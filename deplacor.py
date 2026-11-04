# -*- coding: utf-8 -*-
"""
Deplaçor — Interactions Twitch pour OBS Studio
==============================================

Écoute le chat Twitch en lecture seule (IRC anonyme, sans jeton) et déplace /
redimensionne des sources OBS quand des mots-clés atteignent un seuil.

Organisation du fichier (sections logiques) :
  1. Journalisation
  2. Configuration : normalisation et validation (pur Python)
  3. Moteur de comportements (pur Python, testable hors OBS)
  4. Client IRC Twitch (thread séparé, pur Python, testable hors OBS)
  5. Destinations OBS : capture et application des transformations
  6. Interface (propriétés du script) et persistance
  7. Points d'entrée OBS (script_load, script_update, ...)

Seules les sections 5 à 7 utilisent obspython. Les sections 1 à 4 peuvent être
importées et testées avec un Python classique (voir tests/).

Vocabulaire :
  - destination : une transformation enregistrée (scène, source, position, taille,
    rotation) que l'on peut ré-appliquer plus tard.
  - comportement : une règle « si tel(s) mot(s)-clé(s) apparaissent N fois en
    T secondes, appliquer telle destination ».
"""

import json
import os
import queue
import random
import re
import socket
import ssl
import threading
import time
from collections import deque, namedtuple

# obspython n'existe que dans OBS. Hors OBS (tests unitaires), on met `obs` à None
# et les sections 1 à 4 restent utilisables.
try:
    import obspython as obs
except ImportError:
    obs = None

SCRIPT_VERSION = "0.3.1"
CONFIG_VERSION = 1


# ---------------------------------------------------------------------------
# 1. Journalisation
# ---------------------------------------------------------------------------

def log(level, msg):
    """Écrit dans le journal des scripts OBS (ou sur stdout hors OBS).

    level : 'info', 'warning' ou 'error'.
    """
    if obs is not None:
        lvl = {
            "info": obs.LOG_INFO,
            "warning": obs.LOG_WARNING,
            "error": obs.LOG_ERROR,
        }.get(level, obs.LOG_INFO)
        obs.script_log(lvl, "[Twitch] %s" % msg)
    else:
        print("[%s] %s" % (level.upper(), msg))


# ---------------------------------------------------------------------------
# 2. Configuration : normalisation et validation
# ---------------------------------------------------------------------------
# Toutes les listes de choix sont des couples (valeur_interne, libellé_affiché).
# La valeur interne est celle stockée dans le JSON ; le libellé est pour l'UI.

ROLE_CHOICES = [
    ("everyone", "Tout le monde"),
    ("moderators", "Modérateurs uniquement"),
    ("broadcaster", "Broadcaster uniquement"),
    ("mods_broadcaster", "Modérateurs et broadcaster"),
    ("vip", "VIP uniquement"),
    ("custom", "Liste personnalisée de pseudos"),
]
COUNT_CHOICES = [
    ("combined", "Combiné (un seul compteur)"),
    ("individual", "Individuel (un compteur par mot-clé)"),
]
TRIGGER_CHOICES = [
    ("once", "Unique (réarmement quand le seuil retombe)"),
    ("repeat", "Répétitif (compteur remis à zéro après chaque déclenchement)"),
]
_ROLE_KEYS = {k for k, _ in ROLE_CHOICES}
_COUNT_KEYS = {k for k, _ in COUNT_CHOICES}
_TRIGGER_KEYS = {k for k, _ in TRIGGER_CHOICES}

# Un nom de chaîne Twitch : lettres minuscules, chiffres et _ (1 à 25 caractères).
_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,25}$")


def default_config():
    """Configuration vide, structure de référence du JSON."""
    return {"version": CONFIG_VERSION, "channel": "", "destinations": [], "behaviors": []}


def normalize_channel(value):
    """Nettoie un nom de chaîne ('  #Foo ' -> 'foo'). Retourne '' si invalide."""
    ch = str(value or "").strip().lstrip("#").lower()
    return ch if _CHANNEL_RE.match(ch) else ""


def split_list(value):
    """Convertit une chaîne ou une liste en liste de textes, sans doublons.

    Exemples :
        'a, b\\nc'  -> ['a', 'b', 'c']
        ['a', 'b']  -> ['a', 'b']

    Les séparateurs acceptés sont la virgule, le point-virgule et le retour à la
    ligne. Les doublons (comparés sans tenir compte de la casse) sont supprimés
    en conservant l'ordre et la graphie de la première occurrence.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\n]", value)
    else:
        parts = [str(p) for p in value]

    out, seen = [], set()
    for p in parts:
        p = p.strip()
        # casefold() : comparaison insensible à la casse, y compris les accents.
        if p and p.casefold() not in seen:
            seen.add(p.casefold())
            out.append(p)
    return out


def _num(value, default, cast=float):
    """Convertit `value` avec `cast` (float par défaut) ; `default` si impossible."""
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def normalize_destination(raw):
    """Valide une destination brute (dict). Retourne (destination | None, [erreurs]).

    Champs produits :
      name, scene, source      : identifiants (chaînes non vides)
      x, y, width, height      : position et taille affichée, en pixels
      rotation                 : degrés
      scale_x, scale_y         : échelle réelle de l'élément de scène. 0.0 signifie
                                 « inconnue » (destination enregistrée par une
                                 ancienne version) : on recalculera l'échelle à
                                 partir de width/height au moment de l'application.
      switch_scene             : True pour basculer OBS vers la scène de la
                                 destination quand on l'applique.
    """
    errors = []
    if not isinstance(raw, dict):
        return None, ["destination invalide (objet attendu)"]
    name = str(raw.get("name", "")).strip()
    scene = str(raw.get("scene", "")).strip()
    source = str(raw.get("source", "")).strip()
    if not name:
        errors.append("nom de destination vide")
    if not scene:
        errors.append("scène manquante (%s)" % name)
    if not source:
        errors.append("source manquante (%s)" % name)
    width = _num(raw.get("width"), 0.0)
    height = _num(raw.get("height"), 0.0)
    if width <= 0 or height <= 0:
        errors.append("largeur/hauteur doivent être > 0 (%s)" % name)
    if errors:
        return None, errors
    return {
        "name": name,
        "scene": scene,
        "source": source,
        "x": _num(raw.get("x"), 0.0),
        "y": _num(raw.get("y"), 0.0),
        "width": width,
        "height": height,
        "rotation": _num(raw.get("rotation"), 0.0),
        "scale_x": _num(raw.get("scale_x"), 0.0),
        "scale_y": _num(raw.get("scale_y"), 0.0),
        "switch_scene": bool(raw.get("switch_scene", True)),
    }, []


def normalize_behavior(raw):
    """Valide un comportement brut (dict). Retourne (comportement | None, [erreurs]).

    Champs produits :
      name, enabled
      keywords        : liste de mots ou expressions (mots entiers, insensible à la casse)
      threshold       : nombre d'occurrences à atteindre (>= 1)
      window_seconds  : fenêtre glissante en secondes (> 0)
      count_mode      : 'combined' ou 'individual' (voir COUNT_CHOICES)
      trigger_mode    : 'once' ou 'repeat' (voir TRIGGER_CHOICES)
      roles, users    : qui a le droit de déclencher (users : pseudos en minuscules)
      max_per_message : plafond d'occurrences comptées par message (0 = illimité)
      destination     : nom de la destination à appliquer
    """
    errors = []
    if not isinstance(raw, dict):
        return None, ["comportement invalide (objet attendu)"]
    name = str(raw.get("name", "")).strip()
    if not name:
        errors.append("nom du comportement vide")
    keywords = split_list(raw.get("keywords"))
    if not keywords:
        errors.append("au moins un mot-clé est requis (%s)" % name)
    threshold = _num(raw.get("threshold"), 0, int)
    if threshold < 1:
        errors.append("le nombre de répétitions doit être >= 1 (%s)" % name)
    window = _num(raw.get("window_seconds"), 0.0)
    if window <= 0:
        errors.append("la fenêtre temporelle doit être > 0 s (%s)" % name)
    count_mode = str(raw.get("count_mode", "combined"))
    if count_mode not in _COUNT_KEYS:
        errors.append("mode de comptage inconnu: %s" % count_mode)
    trigger_mode = str(raw.get("trigger_mode", "once"))
    if trigger_mode not in _TRIGGER_KEYS:
        errors.append("mode de déclenchement inconnu: %s" % trigger_mode)
    roles = str(raw.get("roles", "everyone"))
    if roles not in _ROLE_KEYS:
        errors.append("rôle inconnu: %s" % roles)
    # Les pseudos sont stockés en minuscules, sans le @ éventuel, pour la comparaison.
    users = [u.lstrip("@").lower() for u in split_list(raw.get("users"))]
    if roles == "custom" and not users:
        errors.append("liste de pseudos vide alors que le rôle est « personnalisé » (%s)" % name)
    if errors:
        return None, errors
    return {
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "keywords": keywords,
        "threshold": threshold,
        "window_seconds": window,
        "count_mode": count_mode,
        "trigger_mode": trigger_mode,
        "roles": roles,
        "users": users,
        "max_per_message": max(0, _num(raw.get("max_per_message"), 0, int)),
        "destination": str(raw.get("destination", "")).strip(),
    }, []


def normalize_config(raw):
    """Valide une configuration complète. Retourne (config, [erreurs]).

    Les éléments invalides sont ignorés et signalés dans la liste d'erreurs ; les
    doublons de nom (insensibles à la casse) sont ignorés aussi. Aucun jeton
    n'existe dans cette configuration (connexion anonyme).
    """
    cfg = default_config()
    errors = []
    if not isinstance(raw, dict):
        return cfg, ["la configuration doit être un objet JSON"]
    cfg["channel"] = normalize_channel(raw.get("channel", ""))

    # Destinations : validation + dédoublonnage par nom.
    seen = set()
    for d in raw.get("destinations", []) or []:
        dest, errs = normalize_destination(d)
        errors.extend(errs)
        if dest and dest["name"].casefold() not in seen:
            seen.add(dest["name"].casefold())
            cfg["destinations"].append(dest)
        elif dest:
            errors.append("destination en double ignorée: %s" % dest["name"])

    # Comportements : même traitement.
    seen = set()
    for b in raw.get("behaviors", []) or []:
        beh, errs = normalize_behavior(b)
        errors.extend(errs)
        if beh and beh["name"].casefold() not in seen:
            seen.add(beh["name"].casefold())
            cfg["behaviors"].append(beh)
        elif beh:
            errors.append("comportement en double ignoré: %s" % beh["name"])

    # Un comportement qui pointe vers une destination inconnue est conservé mais
    # signalé : il ne pourra rien faire tant que la destination n'existe pas.
    dest_names = {d["name"].casefold() for d in cfg["destinations"]}
    for b in cfg["behaviors"]:
        if b["destination"].casefold() not in dest_names:
            errors.append("destination introuvable pour « %s »: %r" % (b["name"], b["destination"]))
    return cfg, errors


# ---------------------------------------------------------------------------
# 3. Moteur de comportements
# ---------------------------------------------------------------------------

# Un message de chat déjà analysé. `badges` est un frozenset de noms de badges
# ('moderator', 'broadcaster', 'vip', 'subscriber', ...).
ChatMessage = namedtuple("ChatMessage", "user display_name text badges channel")


def is_authorized(behavior, msg):
    """Filtre de rôle, basé uniquement sur les badges/pseudo fournis par Twitch."""
    role = behavior["roles"]
    badges = msg.badges
    if role == "everyone":
        return True
    if role == "moderators":
        return "moderator" in badges
    if role == "broadcaster":
        return "broadcaster" in badges
    if role == "mods_broadcaster":
        return "moderator" in badges or "broadcaster" in badges
    if role == "vip":
        return "vip" in badges
    if role == "custom":
        return msg.user.lower() in behavior["users"]
    return False


class BehaviorEngine:
    """Compteurs à fenêtre glissante, indépendants par comportement.

    Règles :
    - Seuls les messages d'utilisateurs autorisés sont comptés.
    - Mots entiers, insensible à la casse (une expression peut contenir des espaces).
    - Mode « combiné » : un compteur par comportement ; « individuel » : un par mot-clé.
    - Mode « unique » : déclenche quand le seuil est atteint, puis se désarme. Il se
      réarme au premier message correspondant pour lequel, la fenêtre étant purgée,
      le compteur est repassé sous le seuil.
    - Mode « répétitif » : déclenche au seuil puis remet le compteur concerné à zéro ;
      un nouveau déclenchement exige donc un nouveau seuil complet.

    `clock` est injectable pour les tests (horloge simulée).
    """

    def __init__(self, behaviors=None, clock=time.monotonic):
        self._clock = clock
        self._items = []
        self.set_behaviors(behaviors or [])

    def set_behaviors(self, behaviors):
        """Remplace les comportements et remet tous les compteurs à zéro."""
        self._items = []
        for b in behaviors:
            # (?<!\w) ... (?!\w) : le mot-clé ne doit pas être collé à d'autres
            # lettres ('lol' ne matche pas 'lollipop'). re.escape protège les
            # caractères spéciaux éventuels du mot-clé.
            patterns = [
                (kw, re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE))
                for kw in b["keywords"]
            ]
            # État par comportement : horodatages des occurrences (par clé) et
            # armement (par clé). La clé est '*' en mode combiné, le mot-clé sinon.
            self._items.append((b, patterns, {"events": {}, "armed": {}}))

    def process(self, msg, now=None):
        """Traite un message ; retourne la liste des comportements déclenchés."""
        now = self._clock() if now is None else now
        fired = []
        for b, patterns, st in self._items:
            if not b["enabled"] or not is_authorized(b, msg):
                continue

            # 1) Compter les occurrences de chaque mot-clé dans le message.
            cap = b["max_per_message"]
            per_kw = {}
            for kw, pat in patterns:
                n = len(pat.findall(msg.text))
                if n:
                    per_kw[kw] = min(n, cap) if cap else n
            if not per_kw:
                continue

            # 2) Les regrouper selon le mode de comptage.
            if b["count_mode"] == "combined":
                total = sum(per_kw.values())
                increments = {"*": min(total, cap) if cap else total}
            else:
                increments = per_kw

            # 3) Mettre à jour chaque compteur et vérifier le seuil.
            cutoff = now - b["window_seconds"]
            threshold = b["threshold"]
            triggered = False
            for key, n in increments.items():
                events = st["events"].setdefault(key, deque())
                # Purger les occurrences sorties de la fenêtre.
                while events and events[0] <= cutoff:
                    events.popleft()
                # Réarmement (mode unique) : le compteur est repassé sous le seuil.
                armed = st["armed"].get(key, True)
                if not armed and len(events) < threshold:
                    armed = True
                events.extend([now] * n)
                if len(events) >= threshold and armed:
                    triggered = True
                    if b["trigger_mode"] == "once":
                        armed = False
                    else:
                        events.clear()
                st["armed"][key] = armed
            if triggered:
                fired.append(b)
        return fired


# ---------------------------------------------------------------------------
# 4. Client IRC Twitch (lecture seule, anonyme)
# ---------------------------------------------------------------------------

# Séquences d'échappement des valeurs de tags IRCv3 (ex. 'Bob\\sB' -> 'Bob B').
_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape_tag(value):
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), value)


def parse_irc_line(line):
    """Découpe une ligne IRC brute. Retourne (tags, prefix, command, params).

    Exemple : '@mod=1 :bob!bob@host PRIVMSG #chan :hello'
          -> ({'mod': '1'}, 'bob!bob@host', 'PRIVMSG', ['#chan', 'hello'])
    """
    tags, prefix = {}, ""
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for part in raw[1:].split(";"):
            k, _, v = part.partition("=")
            tags[k] = _unescape_tag(v)
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    command, _, rest = line.partition(" ")
    params = []
    while rest:
        # Un paramètre commençant par ':' est le dernier et peut contenir des espaces.
        if rest.startswith(":"):
            params.append(rest[1:])
            break
        p, _, rest = rest.partition(" ")
        params.append(p)
    return tags, prefix, command, params


def message_from_line(line):
    """Convertit une ligne IRC PRIVMSG en ChatMessage (None pour tout autre type)."""
    tags, prefix, command, params = parse_irc_line(line)
    if command != "PRIVMSG" or len(params) < 2:
        return None
    nick = prefix.split("!", 1)[0].lower()
    text = params[1]
    # Les messages '/me' arrivent entourés de \x01ACTION ... \x01.
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]

    # Badges : 'moderator/1,subscriber/6' -> {'moderator', 'subscriber'}.
    badges = set()
    for item in tags.get("badges", "").split(","):
        if item:
            badges.add(item.split("/", 1)[0])
    if tags.get("mod") == "1":
        badges.add("moderator")
    # Le broadcaster est l'utilisateur dont l'id est celui du salon.
    uid, rid = tags.get("user-id"), tags.get("room-id")
    if uid and rid and uid == rid:
        badges.add("broadcaster")
    return ChatMessage(
        user=nick,
        display_name=tags.get("display-name") or nick,
        text=text,
        badges=frozenset(badges),
        channel=params[0].lstrip("#").lower(),
    )


class TwitchChatClient:
    """Lit le chat dans un thread dédié et dépose les messages dans une queue.

    Connexion anonyme (justinfan + mot de passe quelconque) : aucun compte, aucun
    jeton. Le client n'envoie que des commandes de protocole (CAP, PASS, NICK, JOIN,
    PING/PONG), jamais de message de chat. Reconnexion automatique avec attente
    exponentielle (1 s -> 60 s).

    `status` est une chaîne lisible mise à jour par le thread ; elle est lue par
    l'interface OBS depuis le thread principal (lecture d'une référence : sûr).
    """

    def __init__(self, channel, out_queue, host="irc.chat.twitch.tv", port=6697,
                 use_tls=True, initial_backoff=1.0, max_backoff=60.0):
        self.channel = normalize_channel(channel)
        self.out_queue = out_queue
        self.host, self.port, self.use_tls = host, port, use_tls
        self._initial_backoff, self._max_backoff = initial_backoff, max_backoff
        self._backoff = initial_backoff
        self._stop = threading.Event()
        self._thread = None
        self._sock = None
        self.status = "arrêté"

    # -- contrôle ---------------------------------------------------------
    def start(self):
        """Démarre le thread de lecture (sans effet s'il tourne déjà)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status = "connexion…"
        self._thread = threading.Thread(target=self._run, name="TwitchChat", daemon=True)
        self._thread.start()

    def stop(self):
        """Arrête le thread : on ferme la socket pour débloquer recv(), puis on attend."""
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.status = "arrêté"

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # -- boucle principale ------------------------------------------------
    def _run(self):
        """Boucle de reconnexion : une session, puis attente croissante en cas d'échec."""
        self._backoff = self._initial_backoff
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:  # noqa: BLE001 : on journalise et on reconnecte
                if not self._stop.is_set():
                    log("warning", "chat Twitch: %s" % exc)
            if self._stop.is_set():
                break
            self.status = "reconnexion dans %.0f s" % self._backoff
            # wait() retourne True si stop() a été appelé pendant l'attente.
            if self._stop.wait(self._backoff):
                break
            self._backoff = min(self._backoff * 2, self._max_backoff)
        self.status = "arrêté"

    def _send(self, line):
        self._sock.sendall((line + "\r\n").encode("utf-8"))

    def _session(self):
        """Une connexion complète : handshake, puis lecture jusqu'à erreur ou arrêt."""
        self.status = "connexion…"
        sock = socket.create_connection((self.host, self.port), timeout=15)
        if self.use_tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        # Timeout court sur recv() : permet de vérifier régulièrement l'arrêt
        # demandé et d'envoyer un PING si le serveur est silencieux.
        sock.settimeout(10)
        self._sock = sock
        try:
            self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            self._send("PASS SCHMOOPIIE")  # mot de passe factice : connexion anonyme
            self._send("NICK justinfan%d" % random.randint(10000, 99999999))
            self._send("JOIN #%s" % self.channel)
            buf = b""
            last_rx = last_ping = time.monotonic()
            while not self._stop.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    now = time.monotonic()
                    if now - last_rx > 300:
                        raise ConnectionError("aucune donnée depuis 5 minutes")
                    if now - last_rx > 120 and now - last_ping > 120:
                        self._send("PING :keepalive")
                        last_ping = now
                    continue
                except OSError:
                    if self._stop.is_set():
                        return  # socket fermée par stop() : sortie normale
                    raise
                if not data:
                    raise ConnectionError("connexion fermée par le serveur")
                last_rx = time.monotonic()
                # Les lignes peuvent arriver fragmentées : on accumule jusqu'au CRLF.
                buf += data
                while b"\r\n" in buf:
                    raw, buf = buf.split(b"\r\n", 1)
                    self._handle(raw.decode("utf-8", "replace"))
        finally:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, line):
        """Traite une ligne IRC complète."""
        if not line:
            return
        if line.startswith("PING"):
            self._send("PONG :" + (line.partition(":")[2] or "tmi.twitch.tv"))
            return
        tags, prefix, command, params = parse_irc_line(line)
        if command == "001":  # bienvenue : connexion réussie
            self._backoff = self._initial_backoff
            self.status = "connecté à #%s" % self.channel
        elif command == "RECONNECT":
            raise ConnectionError("le serveur demande une reconnexion")
        elif command == "CAP" and len(params) > 1 and params[1] == "NAK":
            log("warning", "capacités tags refusées : les rôles ne seront pas reconnus")
        elif command == "NOTICE" and params and "authentication" in params[-1].lower():
            raise ConnectionError("Twitch exige une authentification : %s" % params[-1])
        elif command == "PRIVMSG":
            msg = message_from_line(line)
            if msg is not None:
                try:
                    self.out_queue.put_nowait(msg)
                except queue.Full:
                    pass  # on préfère perdre un message que bloquer le thread


# ---------------------------------------------------------------------------
# 5. Destinations OBS : capture et application (API obspython)
# ---------------------------------------------------------------------------
# Rappel sur la gestion mémoire d'obspython : tout objet obtenu par une fonction
# « get_..._by_name » ou « enum_... » doit être libéré (obs_source_release,
# sceneitem_list_release, ...). Les blocs try/finally ci-dessous servent à ça.

def _find_item(scene_name, source_name):
    """Retourne (source_de_scène, élément). L'appelant libère source_de_scène."""
    scene_src = obs.obs_get_source_by_name(scene_name)
    if scene_src is None:
        raise ValueError("scène introuvable : %s" % scene_name)
    scene = obs.obs_scene_from_source(scene_src)
    item = None
    if scene is not None:
        # La version récursive (OBS >= 26) cherche aussi dans les groupes.
        finder = getattr(obs, "obs_scene_find_source_recursive", None) or obs.obs_scene_find_source
        item = finder(scene, source_name)
    if item is None:
        obs.obs_source_release(scene_src)
        raise ValueError("source « %s » introuvable dans la scène « %s »" % (source_name, scene_name))
    return scene_src, item


def _base_size(item):
    """Taille de la source après recadrage, avant mise à l'échelle.

    Attention : pour une source dont la taille dépend du contenu (texte, navigateur),
    cette valeur change dans le temps. C'est pourquoi on préfère enregistrer
    l'échelle (scale_x/scale_y) plutôt que de la recalculer à partir de width/height.
    """
    src = obs.obs_sceneitem_get_source(item)
    crop = obs.obs_sceneitem_crop()
    obs.obs_sceneitem_get_crop(item, crop)
    w = obs.obs_source_get_width(src) - crop.left - crop.right
    h = obs.obs_source_get_height(src) - crop.top - crop.bottom
    return float(w), float(h)


def capture_transform(scene_name, source_name):
    """Lit la transformation actuelle d'une source dans une scène.

    Retourne un dict {x, y, width, height, scale_x, scale_y, rotation}.
    """
    scene_src, item = _find_item(scene_name, source_name)
    try:
        pos = obs.vec2()
        obs.obs_sceneitem_get_pos(item, pos)
        scale = obs.vec2()
        obs.obs_sceneitem_get_scale(item, scale)
        # Deux façons pour OBS de définir la taille affichée :
        #  - « bounds » (boîte englobante) si un type de limites est activé ;
        #  - sinon, taille native x échelle.
        if obs.obs_sceneitem_get_bounds_type(item) != obs.OBS_BOUNDS_NONE:
            b = obs.vec2()
            obs.obs_sceneitem_get_bounds(item, b)
            width, height = b.x, b.y
        else:
            bw, bh = _base_size(item)
            width, height = bw * abs(scale.x), bh * abs(scale.y)
        return {
            "x": pos.x, "y": pos.y,
            "width": width, "height": height,
            "scale_x": scale.x, "scale_y": scale.y,
            "rotation": obs.obs_sceneitem_get_rot(item),
        }
    finally:
        obs.obs_source_release(scene_src)


def _apply_transform(item, dest):
    """Applique position, rotation et taille à un élément de scène (sans changer de scène)."""
    # defer_update : OBS n'émet qu'une seule mise à jour pour tout le bloc,
    # ce qui évite un état intermédiaire visible (position changée mais pas l'échelle).
    obs.obs_sceneitem_defer_update_begin(item)
    try:
        pos = obs.vec2()
        obs.vec2_set(pos, dest["x"], dest["y"])
        obs.obs_sceneitem_set_pos(item, pos)
        obs.obs_sceneitem_set_rot(item, dest["rotation"])

        if obs.obs_sceneitem_get_bounds_type(item) != obs.OBS_BOUNDS_NONE:
            bounds = obs.vec2()
            obs.vec2_set(bounds, dest["width"], dest["height"])
            obs.obs_sceneitem_set_bounds(item, bounds)
        else:
            if dest["scale_x"] and dest["scale_y"]:
                # Cas normal : l'échelle a été enregistrée à la capture.
                sx, sy = dest["scale_x"], dest["scale_y"]
            else:
                # Ancienne destination sans échelle : on la déduit de la taille voulue.
                bw, bh = _base_size(item)
                if bw <= 0 or bh <= 0:
                    raise ValueError("la source « %s » n'a pas encore de taille" % dest["source"])
                sx, sy = dest["width"] / bw, dest["height"] / bh
            scale = obs.vec2()
            obs.vec2_set(scale, sx, sy)
            obs.obs_sceneitem_set_scale(item, scale)
    finally:
        obs.obs_sceneitem_defer_update_end(item)


def _is_program_scene(scene_src):
    """True si `scene_src` est déjà la scène diffusée (programme)."""
    current = obs.obs_frontend_get_current_scene()
    if current is None:
        return False
    try:
        return obs.obs_source_get_name(current) == obs.obs_source_get_name(scene_src)
    finally:
        obs.obs_source_release(current)


def _switch_to_scene(scene_src):
    """Bascule OBS vers la scène, en passant par l'aperçu en mode Studio."""
    if obs.obs_frontend_preview_program_mode_active():
        # Mode Studio : on place la scène cible dans l'aperçu, puis on déclenche
        # la transition aperçu -> programme (comme le bouton « Transition »).
        obs.obs_frontend_set_current_preview_scene(scene_src)
        obs.obs_frontend_preview_program_trigger_transition()
    else:
        # Hors mode Studio, OBS applique lui-même la transition courante.
        obs.obs_frontend_set_current_scene(scene_src)


def apply_destination(dest):
    """Applique la destination : transformation, puis changement de scène si demandé.

    Retourne True si une transition de scène a été déclenchée, False sinon.
    La transformation est appliquée AVANT la transition, pour que la scène arrive
    à l'écran déjà dans le bon état.
    """
    scene_src, item = _find_item(dest["scene"], dest["source"])
    try:
        _apply_transform(item, dest)
        if dest.get("switch_scene", True) and not _is_program_scene(scene_src):
            _switch_to_scene(scene_src)
            return True
        return False
    finally:
        obs.obs_source_release(scene_src)


def transition_duration_s():
    """Durée de la transition de scène courante, en secondes (0 si inconnue)."""
    try:
        return obs.obs_frontend_get_transition_duration() / 1000.0
    except Exception:  # noqa: BLE001 : fonction absente sur de vieilles versions
        return 0.0


def list_scene_names():
    scenes = obs.obs_frontend_get_scenes()
    try:
        return [obs.obs_source_get_name(s) for s in scenes]
    finally:
        obs.source_list_release(scenes)


def _collect_items(items, out, depth):
    """Ajoute à `out` les (libellé, nom) des éléments, en descendant dans les groupes."""
    for it in items:
        name = obs.obs_source_get_name(obs.obs_sceneitem_get_source(it))
        out.append(("    " * depth + ("↳ " if depth else "") + name, name))
        if obs.obs_sceneitem_is_group(it):
            sub = obs.obs_sceneitem_group_enum_items(it)
            try:
                _collect_items(sub, out, depth + 1)
            finally:
                obs.sceneitem_list_release(sub)


def list_sources(scene_name):
    """[(libellé, nom)] des sources d'une scène, groupes inclus (libellé indenté)."""
    scene_src = obs.obs_get_source_by_name(scene_name) if scene_name else None
    out = []
    if scene_src is None:
        return out
    try:
        scene = obs.obs_scene_from_source(scene_src)
        if scene is not None:
            items = obs.obs_scene_enum_items(scene)
            try:
                _collect_items(items, out, 0)
            finally:
                obs.sceneitem_list_release(items)
    finally:
        obs.obs_source_release(scene_src)
    return out


# ---------------------------------------------------------------------------
# 6. Interface (propriétés), persistance, import/export
# ---------------------------------------------------------------------------
# Source de vérité : la chaîne JSON « config_json » stockée dans les paramètres du
# script (sauvegardée par OBS entre les redémarrages). Les fichiers JSON ne servent
# qu'à l'export/import explicites. Le canal est un paramètre OBS séparé.
#
# Les clés de paramètres préfixées « ui_ » sont les champs du formulaire ; elles
# ne font pas partie de la configuration, on les recopie depuis/vers _config.

_settings = None            # obs_data_t du script, fourni par OBS
_config = default_config()  # configuration validée en mémoire
_engine = BehaviorEngine()  # moteur alimenté par _config["behaviors"]
_client = None              # TwitchChatClient en cours, ou None
_queue = None               # file de messages remplie par le client
_signature = None           # dernier texte config_json chargé (évite les rechargements)

# File d'attente des déclenchements pendant une transition de scène (voir _pump).
_pending = deque()
_transition_until = 0.0     # instant (monotonic) où la transition en cours sera finie
_clock = time.monotonic     # remplaçable par une horloge simulée dans les tests


def _get_str(key):
    return obs.obs_data_get_string(_settings, key)


def _set_str(key, value):
    obs.obs_data_set_string(_settings, key, value)


def _feedback(msg, level="info"):
    """Journalise et affiche le message dans le champ « Dernier message » de l'UI."""
    log(level, msg)
    if _settings is not None:
        _set_str("feedback", msg)


def _load_config_from_settings():
    """Relit config_json ; ne reconstruit le moteur que si la config a changé."""
    global _config, _signature
    text = _get_str("config_json") or ""
    if text == _signature:
        return
    _signature = text
    raw = {}
    if text.strip():
        try:
            raw = json.loads(text)
        except ValueError as exc:
            log("error", "config_json illisible, configuration vide utilisée : %s" % exc)
    _config, errors = normalize_config(raw)
    for e in errors:
        log("warning", "configuration : %s" % e)
    _engine.set_behaviors(_config["behaviors"])


def _save_config():
    """Persiste _config dans les paramètres OBS et recharge le moteur."""
    global _signature
    _config["version"] = CONFIG_VERSION
    text = json.dumps(_config, ensure_ascii=False, indent=2)
    _set_str("config_json", text)
    _signature = text
    _engine.set_behaviors(_config["behaviors"])


def _find_named(items, name):
    """Index de l'élément dont le nom vaut `name` (insensible à la casse), ou -1."""
    for i, it in enumerate(items):
        if it["name"].casefold() == (name or "").casefold():
            return i
    return -1


# -- client de chat ---------------------------------------------------------

def _start_client():
    """(Re)démarre la lecture du chat avec la chaîne saisie dans l'UI."""
    global _client, _queue
    channel = normalize_channel(_get_str("channel"))
    if not channel:
        _feedback("Canal Twitch invalide (lettres, chiffres et _ uniquement).", "warning")
        return
    _stop_client()
    _queue = queue.Queue(maxsize=5000)
    _client = TwitchChatClient(channel, _queue)
    _client.start()
    log("info", "lecture du chat de #%s démarrée" % channel)


def _stop_client():
    global _client
    if _client is not None:
        _client.stop()
        _client = None


# -- boucle de traitement (thread principal d'OBS) --------------------------

def _pump():
    """Appelée toutes les 50 ms par un timer OBS, dans le thread principal.

    1) Vide la file des déclenchements différés si la transition est terminée.
    2) Traite les messages reçus par le thread du chat.
    Toute manipulation d'OBS (scènes, sources) doit se faire ici, pas dans le
    thread réseau.
    """
    now = _clock()

    # 1) Déclenchements mis en attente pendant une transition.
    if _pending and now >= _transition_until:
        behavior, msg = _pending.popleft()
        _run_trigger(behavior, msg)

    # 2) Messages de chat (au plus 500 par appel pour ne pas bloquer l'interface).
    if _queue is None:
        return
    for _ in range(500):
        try:
            msg = _queue.get_nowait()
        except queue.Empty:
            return
        try:
            for b in _engine.process(msg):
                _run_trigger(b, msg)
        except Exception as exc:  # noqa: BLE001
            log("error", "traitement du message : %r" % exc)


def _run_trigger(behavior, msg):
    """Applique la destination d'un comportement déclenché.

    Si une transition de scène est encore en cours, le déclenchement est mis en
    attente : modifier l'aperçu ou relancer une transition pendant qu'une autre
    joue produit des états incohérents (déformations, scène fantôme).
    """
    global _transition_until
    idx = _find_named(_config["destinations"], behavior["destination"])
    if idx < 0:
        log("warning", "« %s » : destination « %s » introuvable" % (behavior["name"], behavior["destination"]))
        return
    dest = _config["destinations"][idx]

    now = _clock()
    if dest.get("switch_scene", True) and now < _transition_until:
        _pending.append((behavior, msg))
        return

    try:
        switched = apply_destination(dest)
        if switched:
            # On réserve la durée de la transition + une petite marge.
            _transition_until = now + transition_duration_s() + 0.1
        log("info", "« %s » déclenché (dernier message de %s) -> %s" % (behavior["name"], msg.user, dest["name"]))
    except ValueError as exc:
        log("warning", "« %s » : %s" % (behavior["name"], exc))


# -- formulaire « comportement » --------------------------------------------

_UI_DEFAULTS = {
    "ui_name": "", "ui_keywords": "", "ui_users": "",
    "ui_threshold": 5, "ui_window": 10, "ui_max_per_msg": 0,
    "ui_count_mode": "combined", "ui_trigger_mode": "once", "ui_roles": "everyone",
    "ui_dest": "", "ui_enabled": True,
}


def _load_behavior_into_ui(name):
    """Recopie un comportement dans les champs du formulaire (ou les valeurs par défaut)."""
    idx = _find_named(_config["behaviors"], name)
    if idx < 0:
        d = _UI_DEFAULTS
        _set_str("ui_name", d["ui_name"])
        _set_str("ui_keywords", d["ui_keywords"])
        _set_str("ui_users", d["ui_users"])
        obs.obs_data_set_int(_settings, "ui_threshold", d["ui_threshold"])
        obs.obs_data_set_int(_settings, "ui_window", d["ui_window"])
        obs.obs_data_set_int(_settings, "ui_max_per_msg", d["ui_max_per_msg"])
        _set_str("ui_count_mode", d["ui_count_mode"])
        _set_str("ui_trigger_mode", d["ui_trigger_mode"])
        _set_str("ui_roles", d["ui_roles"])
        _set_str("ui_dest", d["ui_dest"])
        obs.obs_data_set_bool(_settings, "ui_enabled", d["ui_enabled"])
        return
    b = _config["behaviors"][idx]
    _set_str("ui_name", b["name"])
    _set_str("ui_keywords", ", ".join(b["keywords"]))
    _set_str("ui_users", ", ".join(b["users"]))
    obs.obs_data_set_int(_settings, "ui_threshold", b["threshold"])
    obs.obs_data_set_int(_settings, "ui_window", int(round(b["window_seconds"])))
    obs.obs_data_set_int(_settings, "ui_max_per_msg", b["max_per_message"])
    _set_str("ui_count_mode", b["count_mode"])
    _set_str("ui_trigger_mode", b["trigger_mode"])
    _set_str("ui_roles", b["roles"])
    _set_str("ui_dest", b["destination"])
    obs.obs_data_set_bool(_settings, "ui_enabled", b["enabled"])


def _refill(props, name, entries, first=None):
    """Remplace le contenu d'une liste déroulante. `first` : entrée (libellé, valeur) en tête."""
    p = obs.obs_properties_get(props, name)  # cherche aussi dans les groupes
    if p is None:
        return
    obs.obs_property_list_clear(p)
    if first:
        obs.obs_property_list_add_string(p, first[0], first[1])
    for label, value in entries:
        obs.obs_property_list_add_string(p, label, value)


def refresh_lists(props):
    """Reconstruit toutes les listes déroulantes à partir de l'état actuel."""
    if _settings is None:
        return
    _refill(props, "ui_scene", [(n, n) for n in list_scene_names()])
    _refill(props, "ui_source", list_sources(_get_str("ui_scene")))
    dests = _config["destinations"]
    _refill(props, "ui_saved_dest",
            [("%s  (%s › %s)" % (d["name"], d["scene"], d["source"]), d["name"]) for d in dests])
    _refill(props, "ui_dest", [(d["name"], d["name"]) for d in dests], first=("(aucune)", ""))
    _refill(props, "sel_behavior",
            [(("" if b["enabled"] else "[désactivé] ") + b["name"], b["name"])
             for b in _config["behaviors"]],
            first=("(nouveau comportement)", ""))
    _set_str("status", _status_text())


def _ui(fn):
    """Décorateur pour les callbacks de boutons/listes : exécute, puis rafraîchit l'UI.

    Retourner True indique à OBS que les propriétés ont changé et qu'il doit
    redessiner le formulaire.
    """
    def wrapper(props, prop, *args):
        fn(props, prop, *args)
        refresh_lists(props)
        return True
    return wrapper


@_ui
def _on_select_behavior(props, prop, settings):
    _load_behavior_into_ui(_get_str("sel_behavior"))


@_ui
def _on_scene_changed(props, prop, settings):
    # Évite de garder sélectionnée une source qui n'existe pas dans la nouvelle scène.
    _set_str("ui_source", "")


@_ui
def _on_save_behavior(props, prop):
    raw = {
        "name": _get_str("ui_name"),
        "enabled": obs.obs_data_get_bool(_settings, "ui_enabled"),
        "keywords": _get_str("ui_keywords"),
        "threshold": obs.obs_data_get_int(_settings, "ui_threshold"),
        "window_seconds": obs.obs_data_get_int(_settings, "ui_window"),
        "max_per_message": obs.obs_data_get_int(_settings, "ui_max_per_msg"),
        "count_mode": _get_str("ui_count_mode"),
        "trigger_mode": _get_str("ui_trigger_mode"),
        "roles": _get_str("ui_roles"),
        "users": _get_str("ui_users"),
        "destination": _get_str("ui_dest"),
    }
    sel = _find_named(_config["behaviors"], _get_str("sel_behavior"))
    beh, errors = normalize_behavior(raw)
    if errors:
        _feedback("Comportement non enregistré : " + " ; ".join(errors), "warning")
        return
    # Interdit de renommer vers un nom déjà pris par un autre comportement.
    dup = _find_named(_config["behaviors"], beh["name"])
    if dup >= 0 and dup != sel:
        _feedback("Un comportement nommé « %s » existe déjà." % beh["name"], "warning")
        return
    if sel >= 0:
        _config["behaviors"][sel] = beh
    else:
        _config["behaviors"].append(beh)
    _set_str("sel_behavior", beh["name"])
    _save_config()
    msg = "Comportement « %s » enregistré." % beh["name"]
    if _find_named(_config["destinations"], beh["destination"]) < 0:
        msg += " Attention : destination introuvable."
    _feedback(msg)


@_ui
def _on_delete_behavior(props, prop):
    sel = _find_named(_config["behaviors"], _get_str("sel_behavior"))
    if sel < 0:
        _feedback("Aucun comportement sélectionné.", "warning")
        return
    name = _config["behaviors"].pop(sel)["name"]
    _set_str("sel_behavior", "")
    _load_behavior_into_ui("")
    _save_config()
    _feedback("Comportement « %s » supprimé." % name)


@_ui
def _on_toggle_behavior(props, prop):
    sel = _find_named(_config["behaviors"], _get_str("sel_behavior"))
    if sel < 0:
        _feedback("Aucun comportement sélectionné.", "warning")
        return
    b = _config["behaviors"][sel]
    b["enabled"] = not b["enabled"]
    obs.obs_data_set_bool(_settings, "ui_enabled", b["enabled"])
    _save_config()
    _feedback("« %s » : %s." % (b["name"], "activé" if b["enabled"] else "désactivé"))


# -- formulaire « destinations » --------------------------------------------

@_ui
def _on_capture_destination(props, prop):
    """Enregistre la transformation actuelle de la source choisie sous un nom."""
    name = _get_str("ui_dest_name").strip()
    scene, source = _get_str("ui_scene"), _get_str("ui_source")
    if not name or not scene or not source:
        _feedback("Choisissez une scène, une source et donnez un nom à la destination.", "warning")
        return
    try:
        t = capture_transform(scene, source)
    except ValueError as exc:
        _feedback(str(exc), "warning")
        return
    raw = dict(t, name=name, scene=scene, source=source,
               switch_scene=obs.obs_data_get_bool(_settings, "ui_switch_scene"))
    dest, errors = normalize_destination(raw)
    if errors:
        _feedback("Destination non enregistrée : " + " ; ".join(errors), "warning")
        return
    idx = _find_named(_config["destinations"], name)
    if idx >= 0:
        _config["destinations"][idx] = dest  # même nom : on écrase
    else:
        _config["destinations"].append(dest)
    _set_str("ui_saved_dest", dest["name"])
    _save_config()
    _feedback("Destination « %s » enregistrée (%.0f, %.0f, %.0f×%.0f)." %
              (dest["name"], dest["x"], dest["y"], dest["width"], dest["height"]))


@_ui
def _on_test_destination(props, prop):
    idx = _find_named(_config["destinations"], _get_str("ui_saved_dest"))
    if idx < 0:
        _feedback("Aucune destination sélectionnée.", "warning")
        return
    try:
        apply_destination(_config["destinations"][idx])
        _feedback("Destination « %s » appliquée." % _config["destinations"][idx]["name"])
    except ValueError as exc:
        _feedback(str(exc), "warning")


@_ui
def _on_delete_destination(props, prop):
    idx = _find_named(_config["destinations"], _get_str("ui_saved_dest"))
    if idx < 0:
        _feedback("Aucune destination sélectionnée.", "warning")
        return
    name = _config["destinations"].pop(idx)["name"]
    _set_str("ui_saved_dest", "")
    _save_config()
    # Prévenir si des comportements pointaient vers cette destination.
    users = [b["name"] for b in _config["behaviors"] if b["destination"].casefold() == name.casefold()]
    msg = "Destination « %s » supprimée." % name
    if users:
        msg += " Utilisée par : " + ", ".join(users)
    _feedback(msg, "warning" if users else "info")


# -- connexion, import/export -----------------------------------------------

@_ui
def _on_start(props, prop):
    _start_client()


@_ui
def _on_stop(props, prop):
    _stop_client()
    log("info", "lecture du chat arrêtée")


@_ui
def _on_refresh(props, prop):
    pass  # le décorateur fait le rafraîchissement


@_ui
def _on_export(props, prop):
    path = _get_str("json_path")
    if not path:
        _feedback("Choisissez un fichier JSON.", "warning")
        return
    data = dict(_config, channel=normalize_channel(_get_str("channel")))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _feedback("Configuration exportée vers %s (sans aucun jeton)." % path)
    except OSError as exc:
        _feedback("Export impossible : %s" % exc, "error")


@_ui
def _on_import(props, prop):
    global _config
    path = _get_str("json_path")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        _feedback("Import impossible : %s" % exc, "error")
        return
    cfg, errors = normalize_config(raw)
    if not cfg["behaviors"] and not cfg["destinations"]:
        _feedback("Import annulé : rien d'exploitable. " + " ; ".join(errors[:3]), "warning")
        return
    _config = cfg
    if cfg["channel"]:
        _set_str("channel", cfg["channel"])
    _set_str("sel_behavior", "")
    _load_behavior_into_ui("")
    _save_config()
    msg = "Import réussi : %d comportement(s), %d destination(s)." % (
        len(cfg["behaviors"]), len(cfg["destinations"]))
    if errors:
        msg += " %d avertissement(s) (voir le journal)." % len(errors)
        for e in errors:
            log("warning", "import : %s" % e)
    _feedback(msg)


def _add_choices(prop, choices):
    """Remplit une liste déroulante à partir d'une liste (valeur, libellé)."""
    for value, label in choices:
        obs.obs_property_list_add_string(prop, label, value)


def _status_text():
    running = _client is not None and _client.running
    state = _client.status if running else "arrêté"
    active = sum(1 for b in _config["behaviors"] if b["enabled"])
    return "Chat : %s — %d comportement(s) actif(s) sur %d" % (state, active, len(_config["behaviors"]))


# ---------------------------------------------------------------------------
# 7. Points d'entrée OBS
# ---------------------------------------------------------------------------
# OBS appelle ces fonctions par leur nom : script_description, script_defaults,
# script_properties, script_load, script_update, script_unload.

REPO_URL = "https://github.com/Ealemun/obs-twitch-interactions"


def script_description():
    """Texte affiché en haut du panneau du script.

    OBS l'affiche avec le moteur de texte riche de Qt : seul un sous-ensemble de
    HTML est reconnu (titres, paragraphes, listes, gras, liens, <small>).
    """
    return (
        "<h3>Deplaçor <small>v%(version)s</small></h3>"
        # Une phrase pour dire ce que fait le script.
        "<p>Vos viewers font bouger vos sources depuis le chat. Quand un mot-clé est "
        "répété assez de fois, une source se déplace, change de taille ou de scène.</p>"
        # Mode d'emploi : dans l'ordre où on configure, pas dans l'ordre des blocs.
        "<ol>"
        "<li><b>Destinations</b> : placez une source comme vous voulez dans OBS, "
        "puis enregistrez sa position sous un nom.</li>"
        "<li><b>Comportements</b> : choisissez les mots-clés, le seuil, qui peut "
        "déclencher et la destination à appliquer.</li>"
        "<li><b>Connexion au chat</b> : entrez le nom de la chaîne et lancez la lecture.</li>"
        "</ol>"
        # Rassurer sur la sécurité, puis renvoyer vers la doc complète.
        "<p><small>Connexion anonyme en lecture seule : aucun compte, aucun jeton, "
        "rien n'est jamais écrit dans le chat. "
        "<a href=\"%(url)s\">Documentation et mises à jour</a></small></p>"
        % {"version": SCRIPT_VERSION, "url": REPO_URL}
    )


def script_defaults(settings):
    """Valeurs par défaut des paramètres (appelé avant script_load)."""
    obs.obs_data_set_default_string(settings, "channel", "")
    obs.obs_data_set_default_bool(settings, "autostart", False)
    obs.obs_data_set_default_bool(settings, "ui_switch_scene", True)
    obs.obs_data_set_default_int(settings, "ui_threshold", _UI_DEFAULTS["ui_threshold"])
    obs.obs_data_set_default_int(settings, "ui_window", _UI_DEFAULTS["ui_window"])
    obs.obs_data_set_default_int(settings, "ui_max_per_msg", _UI_DEFAULTS["ui_max_per_msg"])
    obs.obs_data_set_default_bool(settings, "ui_enabled", True)
    obs.obs_data_set_default_string(settings, "ui_count_mode", "combined")
    obs.obs_data_set_default_string(settings, "ui_trigger_mode", "once")
    obs.obs_data_set_default_string(settings, "ui_roles", "everyone")


def script_properties():
    """Construit le formulaire du script. Les listes sont remplies par refresh_lists()."""
    props = obs.obs_properties_create()

    # Statut et retour d'information
    if _settings is not None:
        _set_str("status", _status_text())
    obs.obs_properties_add_text(props, "status", "État", obs.OBS_TEXT_INFO)
    obs.obs_properties_add_text(props, "feedback", "Dernier message", obs.OBS_TEXT_INFO)

    # Connexion
    g = obs.obs_properties_create()
    obs.obs_properties_add_text(g, "channel", "Chaîne Twitch", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_bool(g, "autostart", "Démarrer automatiquement avec OBS")
    obs.obs_properties_add_button(g, "btn_start", "Démarrer / redémarrer la lecture", _on_start)
    obs.obs_properties_add_button(g, "btn_stop", "Arrêter la lecture", _on_stop)
    obs.obs_properties_add_button(g, "btn_refresh", "Rafraîchir l'état", _on_refresh)
    obs.obs_properties_add_group(props, "grp_conn", "Connexion au chat", obs.OBS_GROUP_NORMAL, g)

    # Destinations
    g = obs.obs_properties_create()
    p = obs.obs_properties_add_list(g, "ui_scene", "Scène", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_set_modified_callback(p, _on_scene_changed)
    obs.obs_properties_add_list(g, "ui_source", "Source", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_properties_add_text(g, "ui_dest_name", "Nom de la destination", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_bool(g, "ui_switch_scene", "Basculer vers la scène quand la destination s'applique")
    obs.obs_properties_add_button(g, "btn_capture", "Enregistrer la transformation actuelle", _on_capture_destination)
    obs.obs_properties_add_list(g, "ui_saved_dest", "Destinations enregistrées",
                                obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_properties_add_button(g, "btn_test_dest", "Appliquer la destination sélectionnée (test)", _on_test_destination)
    obs.obs_properties_add_button(g, "btn_del_dest", "Supprimer la destination sélectionnée", _on_delete_destination)
    obs.obs_properties_add_group(props, "grp_dest", "Destinations", obs.OBS_GROUP_NORMAL, g)

    # Comportements
    g = obs.obs_properties_create()
    p = obs.obs_properties_add_list(g, "sel_behavior", "Comportement à modifier",
                                    obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_set_modified_callback(p, _on_select_behavior)
    obs.obs_properties_add_text(g, "ui_name", "Nom", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_bool(g, "ui_enabled", "Activé")
    obs.obs_properties_add_text(g, "ui_keywords", "Mots-clés (séparés par des virgules)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_int(g, "ui_threshold", "Répétitions requises", 1, 10000, 1)
    obs.obs_properties_add_int(g, "ui_window", "Fenêtre (secondes)", 1, 3600, 1)
    obs.obs_properties_add_int(g, "ui_max_per_msg", "Occurrences max comptées par message (0 = illimité)", 0, 1000, 1)
    p = obs.obs_properties_add_list(g, "ui_count_mode", "Mode de comptage", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    _add_choices(p, COUNT_CHOICES)
    p = obs.obs_properties_add_list(g, "ui_trigger_mode", "Mode de déclenchement", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    _add_choices(p, TRIGGER_CHOICES)
    p = obs.obs_properties_add_list(g, "ui_roles", "Qui peut déclencher", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    _add_choices(p, ROLE_CHOICES)
    obs.obs_properties_add_text(g, "ui_users", "Pseudos autorisés (rôle personnalisé)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_list(g, "ui_dest", "Destination à appliquer", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_properties_add_button(g, "btn_save_beh", "Enregistrer le comportement", _on_save_behavior)
    obs.obs_properties_add_button(g, "btn_toggle_beh", "Activer / désactiver le comportement sélectionné", _on_toggle_behavior)
    obs.obs_properties_add_button(g, "btn_del_beh", "Supprimer le comportement sélectionné", _on_delete_behavior)
    obs.obs_properties_add_group(props, "grp_beh", "Comportements", obs.OBS_GROUP_NORMAL, g)

    # Import / export
    g = obs.obs_properties_create()
    obs.obs_properties_add_path(g, "json_path", "Fichier JSON", obs.OBS_PATH_FILE_SAVE, "JSON (*.json)", "")
    obs.obs_properties_add_button(g, "btn_export", "Exporter la configuration", _on_export)
    obs.obs_properties_add_button(g, "btn_import", "Importer la configuration (remplace l'existante)", _on_import)
    obs.obs_properties_add_group(props, "grp_json", "Import / export JSON", obs.OBS_GROUP_NORMAL, g)

    # Remplissage de toutes les listes déroulantes (scènes, sources, destinations, ...).
    refresh_lists(props)
    return props


def script_load(settings):
    global _settings
    _settings = settings
    _load_config_from_settings()
    obs.timer_add(_pump, 50)  # traitement des messages dans le thread principal
    if obs.obs_data_get_bool(settings, "autostart") and normalize_channel(_get_str("channel")):
        _start_client()


def script_update(settings):
    """Appelé par OBS à chaque modification d'un paramètre."""
    global _settings
    _settings = settings
    _load_config_from_settings()


def script_unload():
    obs.timer_remove(_pump)
    _stop_client()

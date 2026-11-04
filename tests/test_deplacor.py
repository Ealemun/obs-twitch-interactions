# -*- coding: utf-8 -*-
"""Tests hors OBS.

Lancer depuis la racine du projet :
    python -m unittest discover -s tests -v

Ces tests n'ont pas besoin d'OBS : ils couvrent la configuration, le moteur de
comportements, l'analyse IRC, le client réseau (contre un faux serveur local) et
la logique de mise en attente pendant une transition (avec des fonctions OBS
remplacées par des doublures).
"""
import os
import queue
import socket
import sys
import threading
import time
import unittest

# Rendre deplacor.py importable quel que soit le répertoire courant.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deplacor as m  # noqa: E402


class FakeClock:
    """Horloge simulée : on avance le temps à la main avec `clock.t += ...`."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def msg(text, user="viewer", badges=()):
    return m.ChatMessage(user, user, text, frozenset(badges), "chan")


def behavior(**kw):
    """Comportement valide par défaut, surchargeable champ par champ."""
    raw = dict(name="b", keywords=["lol"], threshold=3, window_seconds=10,
               count_mode="combined", trigger_mode="once", roles="everyone",
               destination="d")
    raw.update(kw)
    b, errs = m.normalize_behavior(raw)
    assert not errs, errs
    return b


# ---------------------------------------------------------------------------
# Moteur de comportements
# ---------------------------------------------------------------------------

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def engine(self, *behaviors):
        return m.BehaviorEngine(list(behaviors), clock=self.clock)

    def feed(self, e, text, **kw):
        return e.process(msg(text, **kw))

    def test_threshold_and_case_insensitive(self):
        e = self.engine(behavior())
        self.assertFalse(self.feed(e, "LOL"))
        self.assertFalse(self.feed(e, "lol"))
        self.assertTrue(self.feed(e, "Lol"))

    def test_whole_words_only(self):
        e = self.engine(behavior(threshold=1))
        self.assertFalse(self.feed(e, "lollipop"))
        self.assertFalse(self.feed(e, "mdlol"))
        self.assertTrue(self.feed(e, "haha, lol!"))

    def test_phrase_and_accents(self):
        e = self.engine(behavior(keywords=["c'est parti", "été"], threshold=1, count_mode="individual"))
        self.assertTrue(self.feed(e, "C'EST PARTI les gars"))
        self.assertTrue(self.feed(e, "ÉTÉ"))

    def test_sliding_window(self):
        e = self.engine(behavior())
        self.feed(e, "lol")
        self.clock.t += 6
        self.feed(e, "lol")
        self.clock.t += 6          # le premier message a plus de 10 s
        self.assertFalse(self.feed(e, "lol"))
        self.clock.t += 1
        self.assertTrue(self.feed(e, "lol"))

    def test_combined_vs_individual(self):
        comb = self.engine(behavior(keywords=["a", "b"], threshold=2))
        self.feed(comb, "a")
        self.assertTrue(self.feed(comb, "b"))
        ind = self.engine(behavior(keywords=["a", "b"], threshold=2, count_mode="individual"))
        self.feed(ind, "a")
        self.assertFalse(self.feed(ind, "b"))
        self.assertTrue(self.feed(ind, "a"))

    def test_once_mode_rearm(self):
        e = self.engine(behavior(threshold=2))
        self.feed(e, "lol")
        self.assertTrue(self.feed(e, "lol"))
        for _ in range(5):                       # le chat continue : pas de re-déclenchement
            self.clock.t += 1
            self.assertFalse(self.feed(e, "lol"))
        self.clock.t += 20                       # la fenêtre se vide
        self.assertFalse(self.feed(e, "lol"))    # 1 seul message : réarmé mais sous le seuil
        self.assertTrue(self.feed(e, "lol"))     # nouvelle série complète

    def test_repeat_mode(self):
        e = self.engine(behavior(threshold=2, trigger_mode="repeat"))
        self.feed(e, "lol")
        self.assertTrue(self.feed(e, "lol"))
        self.assertFalse(self.feed(e, "lol"))    # compteur remis à zéro
        self.assertTrue(self.feed(e, "lol"))

    def test_multiple_occurrences_and_cap(self):
        e = self.engine(behavior(threshold=3))
        self.assertTrue(self.feed(e, "lol lol lol"))
        e = self.engine(behavior(threshold=3, max_per_message=1))
        self.assertFalse(self.feed(e, "lol lol lol"))

    def test_cap_applies_to_combined_total(self):
        # Deux mots-clés différents dans un même message : le plafond s'applique
        # au total en mode combiné.
        e = self.engine(behavior(keywords=["a", "b"], threshold=2, max_per_message=1))
        self.assertFalse(self.feed(e, "a b"))
        self.assertTrue(self.feed(e, "a b"))

    def test_independent_behaviors(self):
        e = self.engine(behavior(name="x", keywords=["a"], threshold=2),
                        behavior(name="y", keywords=["b"], threshold=2))
        self.feed(e, "a")
        self.feed(e, "b")
        fired = self.feed(e, "a")
        self.assertEqual([b["name"] for b in fired], ["x"])

    def test_disabled(self):
        e = self.engine(behavior(enabled=False, threshold=1))
        self.assertFalse(self.feed(e, "lol"))

    def test_legacy_roles(self):
        # Anciennes valeurs (chaîne unique) : même comportement qu'avant la v0.4.
        cases = {
            "moderators": ({"moderator"}, {"vip"}),
            "broadcaster": ({"broadcaster"}, {"moderator"}),
            "mods_broadcaster": ({"moderator"}, {"vip", "subscriber"}),
            "vip": ({"vip"}, {"moderator"}),
        }
        for role, (ok, ko) in cases.items():
            e = self.engine(behavior(roles=role, threshold=1))
            self.assertTrue(self.feed(e, "lol", badges=ok), role)
            e = self.engine(behavior(roles=role, threshold=1))
            self.assertFalse(self.feed(e, "lol", badges=ko), role)
        e = self.engine(behavior(roles="custom", users="@Alice, bob", threshold=1))
        self.assertTrue(self.feed(e, "lol", user="alice"))
        self.assertFalse(self.feed(e, "lol", user="carol"))

    def test_combined_roles(self):
        # Broadcaster + modos + VIP ensemble : chacun suffit, un simple viewer non.
        b = behavior(roles=["broadcaster", "moderator", "vip"], threshold=1)
        for badges in ({"broadcaster"}, {"moderator"}, {"vip"}):
            self.assertTrue(self.feed(self.engine(b), "lol", badges=badges), badges)
        self.assertFalse(self.feed(self.engine(b), "lol", badges={"subscriber"}))
        self.assertFalse(self.feed(self.engine(b), "lol"))

    def test_roles_plus_users(self):
        # Les pseudos listés s'ajoutent aux rôles cochés.
        # (Un moteur neuf par cas : en mode « unique », un déclenchement désarme.)
        b = behavior(roles=["vip"], users="alice", threshold=1)
        self.assertTrue(self.feed(self.engine(b), "lol", user="alice"))
        self.assertTrue(self.feed(self.engine(b), "lol", user="zoe", badges={"vip"}))
        self.assertFalse(self.feed(self.engine(b), "lol", user="carol"))

    def test_subscriber_includes_founder(self):
        e = self.engine(behavior(roles=["subscriber"], threshold=1))
        self.assertTrue(self.feed(e, "lol", badges={"founder"}))

    def test_unauthorized_not_counted(self):
        e = self.engine(behavior(roles="moderators", threshold=2))
        self.feed(e, "lol")                                  # spectateur : ignoré
        self.assertFalse(self.feed(e, "lol", badges={"moderator"}))
        self.assertTrue(self.feed(e, "lol", badges={"moderator"}))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_validation(self):
        cfg, errs = m.normalize_config({
            "channel": "#MyChan",
            "destinations": [{"name": "A", "scene": "S", "source": "Logo", "x": 1, "y": 2, "width": 100, "height": 50},
                             {"name": "bad", "scene": "S", "source": "Logo", "width": 0, "height": 1}],
            "behaviors": [{"name": "B", "keywords": "lol, gg", "threshold": 3, "window_seconds": 5, "destination": "A"},
                          {"name": "C", "keywords": [], "threshold": 0, "window_seconds": -1},
                          {"name": "D", "keywords": "x", "threshold": 1, "window_seconds": 1, "destination": "missing"}],
        })
        self.assertEqual(cfg["channel"], "mychan")
        self.assertEqual(len(cfg["destinations"]), 1)
        self.assertEqual([b["name"] for b in cfg["behaviors"]], ["B", "D"])
        self.assertEqual(cfg["behaviors"][0]["keywords"], ["lol", "gg"])
        self.assertTrue(any("introuvable" in e for e in errs))

    def test_destination_defaults(self):
        # switch_scene vaut True par défaut (compatibilité avec les anciens JSON),
        # scale_x/scale_y valent 0.0 quand absents (= « inconnue »).
        d, errs = m.normalize_destination({"name": "A", "scene": "S", "source": "X", "width": 10, "height": 10})
        self.assertFalse(errs)
        self.assertTrue(d["switch_scene"])
        self.assertEqual((d["scale_x"], d["scale_y"]), (0.0, 0.0))
        d, _ = m.normalize_destination({"name": "A", "scene": "S", "source": "X", "width": 10, "height": 10,
                                        "switch_scene": False})
        self.assertFalse(d["switch_scene"])

    def test_normalize_roles(self):
        self.assertEqual(m.normalize_roles("mods_broadcaster"), (["broadcaster", "moderator"], []))
        self.assertEqual(m.normalize_roles(["vip", "broadcaster", "vip"]), (["broadcaster", "vip"], []))
        self.assertEqual(m.normalize_roles("vip, moderator"), (["moderator", "vip"], []))
        self.assertEqual(m.normalize_roles(["everyone", "vip"]), (["everyone"], []))
        self.assertEqual(m.normalize_roles(None), (["everyone"], []))
        roles, errs = m.normalize_roles(["vip", "admin"])
        self.assertEqual(roles, ["vip"])
        self.assertTrue(errs)

    def test_nobody_can_trigger_is_an_error(self):
        _, errs = m.normalize_behavior(dict(name="b", keywords="x", threshold=1, window_seconds=1,
                                            roles=[], users=""))
        self.assertTrue(any("personne" in e for e in errs))

    def test_max_per_message_clamped(self):
        self.assertEqual(behavior(max_per_message=-5)["max_per_message"], 0)
        self.assertEqual(behavior(max_per_message="3")["max_per_message"], 3)

    def test_garbage_input(self):
        cfg, errs = m.normalize_config("pas un objet")
        self.assertEqual(cfg["behaviors"], [])
        self.assertTrue(errs)

    def test_channel(self):
        self.assertEqual(m.normalize_channel("  #Foo_1 "), "foo_1")
        self.assertEqual(m.normalize_channel("a b"), "")


# ---------------------------------------------------------------------------
# Analyse IRC
# ---------------------------------------------------------------------------

class ParserTests(unittest.TestCase):
    LINE = ("@badge-info=subscriber/8;badges=moderator/1,subscriber/6;display-name=Bob\\sB;mod=1;"
            "room-id=42;user-id=7 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hello LOL")

    def test_privmsg(self):
        mm = m.message_from_line(self.LINE)
        self.assertEqual(mm.user, "bob")
        self.assertEqual(mm.display_name, "Bob B")
        self.assertEqual(mm.text, "hello LOL")
        self.assertEqual(mm.badges, frozenset({"moderator", "subscriber"}))
        self.assertEqual(mm.channel, "chan")

    def test_broadcaster_by_ids_and_action(self):
        line = ("@badges=;room-id=42;user-id=42 :streamer!s@s.tmi.twitch.tv PRIVMSG #chan "
                ":\x01ACTION lol\x01")
        mm = m.message_from_line(line)
        self.assertIn("broadcaster", mm.badges)
        self.assertEqual(mm.text, "lol")

    def test_vip_and_ignored_lines(self):
        mm = m.message_from_line("@badges=vip/1 :v!v@v.tmi.twitch.tv PRIVMSG #c :x")
        self.assertIn("vip", mm.badges)
        self.assertIsNone(m.message_from_line(":tmi.twitch.tv 001 justinfan1 :Welcome"))
        self.assertIsNone(m.message_from_line("PING :tmi.twitch.tv"))


# ---------------------------------------------------------------------------
# Client réseau (faux serveur IRC local)
# ---------------------------------------------------------------------------

class FakeIrcServer:
    """Serveur IRC local en clair : 1re connexion = message coupé en deux puis fermeture."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.pong = threading.Event()
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            n = self.connections
            conn.settimeout(5)
            data = b""
            while b"JOIN" not in data:
                data += conn.recv(4096)
            self.received.append(data.decode())
            conn.sendall(b":tmi.twitch.tv 001 justinfan1 :Welcome\r\n")
            line = ("@badges=moderator/1;mod=1 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :msg%d\r\n" % n).encode()
            conn.sendall(line[:20])
            time.sleep(0.05)
            conn.sendall(line[20:])
            if n == 1:
                conn.sendall(b"PING :tmi.twitch.tv\r\n")
                try:
                    if b"PONG" in conn.recv(4096):
                        self.pong.set()
                except socket.timeout:
                    pass
                conn.close()           # coupure -> le client doit se reconnecter
            else:
                time.sleep(2)
                conn.close()


class ClientTests(unittest.TestCase):
    def test_reconnect_pong_and_fragmentation(self):
        srv = FakeIrcServer()
        q = queue.Queue()
        c = m.TwitchChatClient("Chan", q, host="127.0.0.1", port=srv.port, use_tls=False,
                               initial_backoff=0.1, max_backoff=0.2)
        c.start()
        try:
            texts = [q.get(timeout=5).text, q.get(timeout=5).text]
            self.assertEqual(texts, ["msg1", "msg2"])
            self.assertTrue(srv.pong.wait(2))
            first = srv.received[0]
            self.assertIn("CAP REQ :twitch.tv/tags twitch.tv/commands", first)
            self.assertIn("NICK justinfan", first)
            self.assertIn("JOIN #chan", first)
            self.assertNotIn("PRIVMSG", first)      # aucun envoi dans le chat
        finally:
            t0 = time.time()
            c.stop()
            self.assertLess(time.time() - t0, 4)
            self.assertFalse(c.running)


# ---------------------------------------------------------------------------
# Mise en attente pendant une transition de scène
# ---------------------------------------------------------------------------

class TransitionQueueTests(unittest.TestCase):
    """On remplace apply_destination et transition_duration_s par des doublures :
    pas besoin d'OBS pour vérifier la logique d'attente."""

    def setUp(self):
        self.clock = FakeClock()
        self.applied = []
        self._saved = (m._clock, m.apply_destination, m.transition_duration_s,
                       m._config, m._transition_until, m._queue)
        m._clock = self.clock
        m.transition_duration_s = lambda: 0.3
        m._transition_until = 0.0
        m._pending.clear()
        m._queue = None  # _pump ne lira pas de messages, seulement la file d'attente
        dests = [{"name": "d1", "switch_scene": True}, {"name": "d2", "switch_scene": True},
                 {"name": "d3", "switch_scene": False}]
        m._config = {"destinations": dests, "behaviors": []}

        def fake_apply(dest):
            self.applied.append(dest["name"])
            return dest["switch_scene"]
        m.apply_destination = fake_apply

    def tearDown(self):
        (m._clock, m.apply_destination, m.transition_duration_s,
         m._config, m._transition_until, m._queue) = self._saved
        m._pending.clear()

    def trigger(self, dest):
        m._run_trigger(behavior(name="b_" + dest, destination=dest), msg("x"))

    def test_second_switch_waits_for_transition_end(self):
        self.trigger("d1")
        self.trigger("d2")                      # transition en cours : mis en attente
        self.assertEqual(self.applied, ["d1"])
        self.clock.t += 0.2
        m._pump()                               # 0,2 s < 0,3 s + marge : toujours en attente
        self.assertEqual(self.applied, ["d1"])
        self.clock.t += 0.3
        m._pump()                               # transition finie : d2 appliquée
        self.assertEqual(self.applied, ["d1", "d2"])

    def test_move_only_destination_is_never_delayed(self):
        self.trigger("d1")
        self.trigger("d3")                      # pas de changement de scène : immédiat
        self.assertEqual(self.applied, ["d1", "d3"])


if __name__ == "__main__":
    unittest.main()

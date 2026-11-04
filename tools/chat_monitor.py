# -*- coding: utf-8 -*-
"""Moniteur de chat : affiche en direct les messages d'une chaîne Twitch.

Sert à vérifier que la connexion anonyme fonctionne et à voir les badges
que Twitch attache à chaque message (utile pour régler « Qui peut déclencher »).

Usage, depuis la racine du projet :
    python tools/chat_monitor.py nom_de_la_chaine

Ctrl+C pour arrêter. Aucun jeton, aucune écriture dans le chat.
"""
import os
import queue
import sys

# Importer le client depuis deplacor.py sans dupliquer le code.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from deplacor import TwitchChatClient, normalize_channel  # noqa: E402


def main(argv):
    if len(argv) != 2 or not normalize_channel(argv[1]):
        print("usage : python tools/chat_monitor.py <chaine_twitch>")
        return 2
    channel = normalize_channel(argv[1])

    messages = queue.Queue(maxsize=1000)
    client = TwitchChatClient(channel, messages)
    last_status = None
    print("Connexion au chat de #%s… (Ctrl+C pour arrêter)" % channel)
    client.start()
    try:
        while True:
            try:
                # Un message : on l'affiche avec ses badges.
                msg = messages.get(timeout=1)
                badges = ",".join(sorted(msg.badges)) or "-"
                print("%s [%s]: %s" % (msg.display_name, badges, msg.text))
            except queue.Empty:
                # Pas de message depuis 1 s : on affiche le statut s'il a changé.
                if client.status != last_status:
                    print("[statut] %s" % client.status)
                    last_status = client.status
    except KeyboardInterrupt:
        print("\nArrêt…")
    finally:
        client.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# Deplaçor — interactions Twitch pour OBS Studio

Script Python pour OBS Studio qui écoute le chat d'une chaîne Twitch et déplace ou
redimensionne des sources quand des mots-clés sont répétés assez de fois.

- **Aucun jeton, aucun compte** : connexion IRC anonyme en lecture seule.
- **Un seul fichier** à charger dans OBS : `deplacor.py`.
- Configuration entièrement depuis l'interface des scripts d'OBS, exportable en JSON.

## Installation

1. OBS Studio avec le support Python activé (Outils → Scripts → Paramètres Python,
   pointer vers une installation de Python 3 de la version attendue par OBS).
2. Cloner ou télécharger ce dépôt n'importe où (pas besoin d'aller dans le dossier
   d'installation d'OBS).
3. Outils → Scripts → **+** → choisir `deplacor.py`.

## Utilisation

Le panneau du script comporte quatre blocs.

**Connexion au chat** : saisir le nom de la chaîne (sans `#`), puis « Démarrer ».
« Démarrer automatiquement avec OBS » relance la lecture au prochain lancement.

**Destinations** : une destination est une transformation enregistrée.
Placer la source comme voulu dans OBS, choisir la scène et la source, donner un
nom, puis « Enregistrer la transformation actuelle ». La case « Basculer vers la
scène » indique si OBS doit changer de scène quand la destination s'applique ou
seulement déplacer la source. Le bouton « Appliquer (test) » permet de vérifier.

**Comportements** : une règle « si les mots-clés atteignent N occurrences en
T secondes, appliquer telle destination ».

| Champ | Rôle |
|---|---|
| Mots-clés | liste séparée par des virgules ; mots entiers, insensible à la casse, les expressions avec espaces sont acceptées |
| Répétitions requises / Fenêtre | seuil et fenêtre glissante |
| Occurrences max par message | plafond compté pour un même message (0 = illimité), évite qu'un seul `lol lol lol lol lol` déclenche tout |
| Mode de comptage | *combiné* : un compteur pour tous les mots-clés ; *individuel* : un par mot-clé |
| Mode de déclenchement | *unique* : se déclenche une fois puis attend que le compteur retombe sous le seuil ; *répétitif* : remet le compteur à zéro à chaque déclenchement |
| Qui peut déclencher | tout le monde, modérateurs, broadcaster, VIP ou une liste de pseudos |

**Import / export JSON** : sauvegarde ou restauration de toute la configuration.
Un exemple commenté se trouve dans `examples/config.example.json`.

## Changement de scène et transitions

Quand une destination bascule vers une autre scène, le script applique d'abord la
transformation, puis déclenche la transition (via l'aperçu en mode Studio). Si un
second déclenchement arrive pendant que la transition joue encore, il est mis en
attente et appliqué à la fin de la transition : relancer une transition pendant
qu'une autre est en cours produit des déformations ou des scènes fantômes.

En mode Studio, désactiver « Dupliquer la scène » dans les paramètres du mode
Studio est recommandé : avec une scène dupliquée, l'aperçu travaille sur une copie
des éléments et la transformation appliquée par le script peut ne pas être celle
qui arrive à l'écran.

## Développement

Les sections 1 à 4 du script (configuration, moteur, client IRC) sont du Python
pur, sans dépendance à OBS. Elles sont couvertes par des tests :

```
python -m unittest discover -s tests -v
```

Pour voir le chat d'une chaîne en direct (et les badges attachés aux messages) :

```
python tools/chat_monitor.py nom_de_la_chaine
```

## Structure

```
deplacor.py                   script OBS (seul fichier à charger)
tests/test_deplacor.py        tests unitaires, sans OBS
tools/chat_monitor.py         affichage du chat en direct
examples/config.example.json  exemple de configuration exportée
```

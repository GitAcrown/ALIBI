"""Emojis UI ALIBI (format <:name:id>).

Remplis les chaînes une fois les emojis uploadés sur l'application / le serveur.
Tant qu'une valeur est vide, aucun emoji n'est affiché.
"""

from __future__ import annotations


def e(code: str, fallback: str = "") -> str:
    """Renvoie l'emoji custom s'il est renseigné, sinon le fallback (souvent vide)."""
    return code if code else fallback


# --- Tampons / identité ---
CLASSIFIED = ""          # tampon DOSSIER CLASSIFIÉ
CASE_CLOSED = ""         # tampon CASE CLOSED
BLACKOUT = ""            # bloc de censure générique
FILE = ""                # dossier / archive
HOF = ""                 # hall of fame

# --- Actions jeu ---
EVIDENCE = ""            # preuves
SUSPECT = ""             # suspects
INTERROGATE = ""         # interrogatoire
ACCUSE = ""              # accusation
CLOCK = ""               # temps restant / statut
STATUS = ""              # mon statut
REFRESH = ""             # rafraîchir
HISTORY = ""             # historique d'interrogatoire

# --- Badges ---
BADGE_DETECTIVE = "<:BEST_D:1536047965985181756>"
BADGE_FIRST = "<:FAST_D:1536047964630290452>"
BADGE_WRONG = "<:CONF_D:1536047963460075540>"
BADGE_WORST = "<:WORST_D:1536047962323554336>"

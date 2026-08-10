"""Constantes de configuration du cog Enquête."""

import os

MODEL_MAIN = "gpt-5.6-luna"

# Génération / audit — plus de marge de raisonnement, sorties volumineuses.
GENERATION_REASONING_EFFORT = "medium"
GENERATION_MAX_TOKENS = 24000
# reasoning_effort consomme une partie du budget de tokens en raisonnement interne AVANT
# de produire le JSON de sortie : avec un budget trop juste, ça peut renvoyer une
# complétion vide (tout le budget est parti en raisonnement). "high" épuisait même 8000-
# 12000 tokens en pratique → repassé à "medium", qui laisse une marge fiable pour le JSON.
# Les passes de CORRECTION (previous_candidate + issues fournis) reçoivent plus de budget
# de réflexion : corriger des erreurs précises sans tout casser est un exercice plus
# contraint que la création initiale, ça mérite davantage de raisonnement.
GENERATION_REASONING_EFFORT_REPAIR = "high"
AUDIT_REASONING_EFFORT = "medium"
AUDIT_MAX_TOKENS = 12000
# Si la complétion revient vide (budget épuisé par le raisonnement), on retente une fois
# avec ce budget agrandi avant d'abandonner l'audit pour ce candidat.
AUDIT_MAX_TOKENS_RETRY = 20000
# 1 génération initiale + corrections ciblées (pas de retour à zéro) si le
# validateur ou l'auditeur relève un problème précis. Relevé de 6 à 10 : les échecs de
# génération complète (après épuisement des tentatives) sont plus coûteux pour le joueur
# qu'une tentative de correction supplémentaire.
MAX_GENERATION_ATTEMPTS = 10

# Incarnation de suspect — réponses courtes, mais reasoning_effort="none" donnait des
# réponses hors-sujet/hors-personnage (le modèle ne "réfléchit" pas assez à la consigne
# stricte de rester sur le sujet). Passé à "medium" ; budget de tokens élargi en
# conséquence (même leçon que l'auditeur : le raisonnement interne consomme le budget
# AVANT le JSON de sortie, un budget trop juste peut renvoyer une réponse vide).
ACTOR_REASONING_EFFORT = "medium"
ACTOR_MAX_TOKENS = 2000
RESOLUTION_REASONING_EFFORT = "low"
RESOLUTION_MAX_TOKENS = 2000
MAX_ACTOR_ATTEMPTS = 2
# Intervalle mini entre deux edits Discord pendant le streaming de la déposition
# (cooldown message Discord ~5 edits / 5 s — on reste largement en dessous).
INTERROGATION_STREAM_EDIT_INTERVAL_S = 1.25

# Règles de jeu
CASE_DURATION_HOURS = 3
# 12 suspects fixes rendaient le jeu difficile à suivre (trop de monde à interroger/mémoriser).
# Chaque enquête tire désormais un sous-ensemble aléatoire de portraits parmi le pool disponible.
MIN_SUSPECTS = 6
MAX_SUSPECTS = 8
# Planchers ABSOLUS (fallback petit casting) — en pratique min_suspects_with_trait()
# ci-dessous exige davantage dès que le casting grandit, pour une partie de plusieurs
# heures avec assez de fausses pistes pour occuper les joueurs tout du long.
MIN_SUSPECTS_WITH_SECRET = 4
MIN_SUSPECTS_WITH_MOBILE = 4
# Preuves visibles dès le lancement (le reste se révèle en cours de partie).
PUBLIC_EVIDENCE_AT_START = 2


def min_suspects_with_trait(n_suspects: int) -> int:
    """Nombre minimum de suspects devant avoir un secret / mobile apparent.

    Relevé pour durcir la partie : ~2/3 du casting (plancher 4) — plus de fausses
    pistes, moins de lecture « le seul qui a un secret = le coupable »."""
    return max(4, (n_suspects * 2) // 3)


def max_questions_for_case(case) -> int:
    """Quota d'interrogations par joueur : un de moins que le nombre de suspects
    (6→5, 8→7) — force le partage d'infos entre enquêteurs, empêche de tout
    interroger seul."""
    n = len(getattr(case, "suspects", None) or {})
    if n <= 0:
        return MIN_SUSPECTS - 1
    return max(4, n - 1)

# Override global de durée (tests) : CASE_DURATION_MINUTES=5 dans .env
# Si absent / invalide → CASE_DURATION_HOURS * 60.
def default_duration_minutes(bot_config: dict | None = None) -> int:
    raw = ""
    if bot_config:
        raw = str(bot_config.get("CASE_DURATION_MINUTES") or "").strip()
    if not raw:
        raw = (os.getenv("CASE_DURATION_MINUTES") or "").strip()
    if not raw:
        try:
            from dotenv import dotenv_values
            raw = str(dotenv_values(".env").get("CASE_DURATION_MINUTES") or "").strip()
        except Exception:
            raw = ""
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return CASE_DURATION_HOURS * 60

# Détection de reformulation (0-1, ratio de similarité texte normalisé)
DUPLICATE_QUESTION_THRESHOLD = 0.82

# Sélection de facts pertinents par mots-clés
RELEVANT_FACTS_TOP_K = 6

# Mobile deviné à l'accusation (bonus de points, indice de recoupement du mobile réel).
# Seuils de recoupement (indice de Jaccard sur mots-clés normalisés, cf. scoring.py) :
# en dessous de CLOSE, aucun rapport ; entre CLOSE et EXACT, piste proche ; au-dessus, mobile
# identifié. Volontairement permissif (le joueur écrit une phrase courte, pas le texte exact).
MOTIVE_GUESS_CLOSE_THRESHOLD = 0.12
MOTIVE_GUESS_EXACT_THRESHOLD = 0.35
MOTIVE_BONUS_CLOSE = 1
MOTIVE_BONUS_EXACT = 2
# Points pour avoir désigné le bon coupable (le bonus mobile s'ajoute par-dessus).
CORRECT_ACCUSATION_POINTS = 3

# Timer de résolution automatique
RESOLUTION_CHECK_INTERVAL_MINUTES = 1

# Planning automatique — créneaux quotidiens (fuseau pour l'heure affichée / de déclenchement).
SCHEDULE_TIMEZONE = "Europe/Paris"
SCHEDULE_CHECK_INTERVAL_MINUTES = 1

PORTRAITS_DATA_PATH = "assets/portraits_data.json"
PORTRAITS_DIR = "assets/portraits"

# Rôle Discord « Enquêteur » — self-assignable via /notif, mentionné au lancement d'une affaire.
# Si NOTIF_ROLE_ID est défini dans .env, on l'utilise ; sinon le bot cherche (ou crée) un
# rôle nommé ENQUETEUR_ROLE_NAME sur le serveur.
ENQUETEUR_ROLE_NAME = "Enquêteur"


def notif_role_id(bot_config: dict | None = None) -> int | None:
    """ID de rôle notif override (.env NOTIF_ROLE_ID), ou None pour création/recherche auto."""
    raw = ""
    if bot_config:
        raw = str(bot_config.get("NOTIF_ROLE_ID") or "").strip()
    if not raw:
        raw = (os.getenv("NOTIF_ROLE_ID") or "").strip()
    if not raw:
        try:
            from dotenv import dotenv_values
            raw = str(dotenv_values(".env").get("NOTIF_ROLE_ID") or "").strip()
        except Exception:
            raw = ""
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None

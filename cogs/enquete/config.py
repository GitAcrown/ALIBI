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

# Règles de jeu
CASE_DURATION_HOURS = 4
# 12 suspects fixes rendaient le jeu difficile à suivre (trop de monde à interroger/mémoriser).
# Chaque enquête tire désormais un sous-ensemble aléatoire de portraits parmi le pool disponible.
MIN_SUSPECTS = 6
MAX_SUSPECTS = 8
# Planchers ABSOLUS (fallback petit casting) — en pratique min_suspects_with_trait()
# ci-dessous exige davantage dès que le casting grandit, pour une partie de plusieurs
# heures avec assez de fausses pistes pour occuper les joueurs tout du long.
MIN_SUSPECTS_WITH_SECRET = 3
MIN_SUSPECTS_WITH_MOBILE = 3


def min_suspects_with_trait(n_suspects: int) -> int:
    """Nombre minimum de suspects devant avoir un secret / mobile apparent, proportionnel
    au casting (au moins la moitié) plutôt qu'un plancher fixe — un casting à 8 doit rester
    difficile aussi longtemps qu'un casting à 6."""
    return max(3, (n_suspects + 1) // 2)


def max_questions_for_case(case) -> int:
    """Quota d'interrogations par joueur = nombre de suspects de l'affaire (6→6, 8→8…)."""
    n = len(getattr(case, "suspects", None) or {})
    if n <= 0:
        return MIN_SUSPECTS
    return n

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

# Timer de résolution automatique
RESOLUTION_CHECK_INTERVAL_MINUTES = 1

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

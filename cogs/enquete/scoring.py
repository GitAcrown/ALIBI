"""Scoring — classement V1, pensé pour être facilement étendu (registre de badges).

Chaque badge est une fonction pure `(case, accusations) -> Optional[player_id]`. Pour en
ajouter un nouveau : écrire la fonction, l'ajouter à `BADGES`. `compute_results` s'occupe
du reste (attribution, stockage dans `PlayerResult.badges`).
"""

from __future__ import annotations

from typing import Callable, Optional

from . import config
from .facts import normalize_tokens
from .models import Accusation, Case, PlayerResult

BadgeFn = Callable[[Case, list[Accusation]], Optional[int]]


def _correct(case: Case, accusations: list[Accusation]) -> list[Accusation]:
    return [a for a in accusations if a.suspect_id == case.guilty_suspect_id]


def _wrong(case: Case, accusations: list[Accusation]) -> list[Accusation]:
    return [a for a in accusations if a.suspect_id != case.guilty_suspect_id]


def badge_best_detective(case: Case, accusations: list[Accusation]) -> Optional[int]:
    """Bonne accusation dont la décision finale a été prise le plus tôt."""
    correct = _correct(case, accusations)
    if not correct:
        return None
    return min(correct, key=lambda a: a.last_created_at).player_id


def badge_first_to_crack(case: Case, accusations: list[Accusation]) -> Optional[int]:
    """Parmi les gagnants, celui dont la toute première accusation était déjà la bonne."""
    correct = _correct(case, accusations)
    if not correct:
        return None
    return min(correct, key=lambda a: a.first_created_at).player_id


def badge_most_confidently_wrong(case: Case, accusations: list[Accusation]) -> Optional[int]:
    """Mauvaise accusation jamais modifiée, prise le plus tôt — confiance totale, tort total."""
    wrong = _wrong(case, accusations)
    if not wrong:
        return None
    never_changed = [a for a in wrong if a.change_count == 0]
    pool = never_changed or wrong
    return min(pool, key=lambda a: a.first_created_at).player_id


def badge_worst_accusation(case: Case, accusations: list[Accusation]) -> Optional[int]:
    """Mauvaise accusation contre le suspect le moins suspect (le moins d'indices apparents)."""
    wrong = _wrong(case, accusations)
    if not wrong:
        return None

    def suspicion(a: Accusation) -> int:
        suspect = case.suspects.get(a.suspect_id)
        return suspect.suspicion_index if suspect else 0

    return min(wrong, key=lambda a: (suspicion(a), a.first_created_at)).player_id


BADGES: list[tuple[str, BadgeFn]] = [
    ("BEST_DETECTIVE", badge_best_detective),
    ("FIRST_TO_CRACK_THE_CASE", badge_first_to_crack),
    ("MOST_CONFIDENTLY_WRONG", badge_most_confidently_wrong),
    ("WORST_ACCUSATION", badge_worst_accusation),
]


def _motive_target_text(case: Case, accused_suspect_id: str) -> Optional[str]:
    """Texte de référence pour juger le mobile deviné par un joueur.

    Le VRAI coupable a son mobile détaillé dans `case.motive` (texte riche, réservé à la
    résolution). Un suspect innocent n'a qu'un `mobile` apparent, souvent plus court (ou
    aucun) — comparer contre ce champ plutôt que contre le mobile du vrai coupable, qui
    n'a rien à voir avec une personne qu'on accuse à tort."""
    if accused_suspect_id == case.guilty_suspect_id:
        return case.motive
    suspect = case.suspects.get(accused_suspect_id)
    return suspect.mobile if suspect else None


def score_motive_guess(guess: Optional[str], target: Optional[str]) -> int:
    """Recoupement mots-clés (Jaccard, sans LLM) entre le mobile deviné et le mobile réel
    de la personne accusée. Renvoie un bonus de points (0, CLOSE ou EXACT)."""
    if not guess or not target:
        return 0
    g_tokens = normalize_tokens(guess)
    t_tokens = normalize_tokens(target)
    if not g_tokens or not t_tokens:
        return 0
    overlap = len(g_tokens & t_tokens) / len(g_tokens | t_tokens)
    if overlap >= config.MOTIVE_GUESS_EXACT_THRESHOLD:
        return config.MOTIVE_BONUS_EXACT
    if overlap >= config.MOTIVE_GUESS_CLOSE_THRESHOLD:
        return config.MOTIVE_BONUS_CLOSE
    return 0


def compute_results(case: Case, accusations: list[Accusation]) -> list[PlayerResult]:
    badge_winners: dict[str, Optional[int]] = {name: fn(case, accusations) for name, fn in BADGES}

    results: list[PlayerResult] = []
    for acc in accusations:
        correct = acc.suspect_id == case.guilty_suspect_id
        badges = [name for name, winner in badge_winners.items() if winner == acc.player_id]
        target = _motive_target_text(case, acc.suspect_id)
        motive_points = score_motive_guess(acc.motive_guess, target)
        accusation_points = config.CORRECT_ACCUSATION_POINTS if correct else 0
        results.append(
            PlayerResult(
                case_pk=case.case_pk,
                player_id=acc.player_id,
                accused_suspect_id=acc.suspect_id,
                correct=correct,
                badges=badges,
                motive_guess=acc.motive_guess,
                motive_points=motive_points,
                points=accusation_points + motive_points,
            )
        )
    # Classement décroissant : plus de points d'abord, puis bonne accusation avant mauvaise.
    results.sort(key=lambda r: (r.points, int(r.correct)), reverse=True)
    return results

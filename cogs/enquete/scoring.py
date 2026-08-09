"""Scoring — classement V1, pensé pour être facilement étendu (registre de badges).

Chaque badge est une fonction pure `(case, accusations) -> Optional[player_id]`. Pour en
ajouter un nouveau : écrire la fonction, l'ajouter à `BADGES`. `compute_results` s'occupe
du reste (attribution, stockage dans `PlayerResult.badges`).
"""

from __future__ import annotations

from typing import Callable, Optional

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


def compute_results(case: Case, accusations: list[Accusation]) -> list[PlayerResult]:
    badge_winners: dict[str, Optional[int]] = {name: fn(case, accusations) for name, fn in BADGES}

    results: list[PlayerResult] = []
    for acc in accusations:
        correct = acc.suspect_id == case.guilty_suspect_id
        badges = [name for name, winner in badge_winners.items() if winner == acc.player_id]
        results.append(
            PlayerResult(
                case_pk=case.case_pk,
                player_id=acc.player_id,
                accused_suspect_id=acc.suspect_id,
                correct=correct,
                badges=badges,
            )
        )
    return results

"""QuestionAnalyzer — détection de reformulation évidente d'une question déjà posée."""

from __future__ import annotations

from difflib import SequenceMatcher

from .facts import normalize_text
from .models import Interrogation

from . import config


def find_duplicate(question: str, history: list[Interrogation]) -> Interrogation | None:
    """Renvoie l'interrogation précédente la plus proche si la question est une
    reformulation évidente d'une question déjà posée par CE joueur à CE suspect.

    Similarité texte (SequenceMatcher) sur la forme normalisée — pas de LLM : la
    détection doit être déterministe et bon marché (appelée avant tout appel modèle).
    """
    normalized = normalize_text(question)
    if not normalized:
        return None

    best: tuple[float, Interrogation] | None = None
    for past in history:
        ratio = SequenceMatcher(None, normalized, past.question_normalized).ratio()
        if ratio >= config.DUPLICATE_QUESTION_THRESHOLD:
            if best is None or ratio > best[0]:
                best = (ratio, past)
    return best[1] if best else None

"""FactEngine — accessibilité des facts par suspect, sélection déterministe par mots-clés.

Aucun appel LLM ici : la pertinence d'une question par rapport aux facts connus d'un
suspect est calculée par recoupement de mots-clés (code), pas par une IA. C'est ce qui
permet de garantir qu'un suspect ne reçoive jamais un fact hors de son périmètre.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Case, Fact, Suspect

from . import config

_WORD_RE = re.compile(r"[a-z0-9]+")
# "23h30", "23 h 30" et "23:30" doivent matcher les mêmes tokens ("23", "30") —
# sinon une question posée avec un format d'heure différent de celui du fact ne
# matche jamais et le suspect se retrouve sans aucun repère (voir relevant_facts).
_TIME_RE = re.compile(r"(\d{1,2})\s*[hH:]\s*(\d{2})\b")

_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "au", "aux", "et", "ou", "a", "à",
    "est", "es", "etes", "sont", "que", "qui", "quoi", "quand", "comment", "pourquoi",
    "tu", "vous", "il", "elle", "ils", "elles", "je", "tes", "vos", "ton", "votre", "vos",
    "sur", "dans", "avec", "pour", "par", "ce", "cette", "ces", "as", "avez", "avait", "était",
    "vraiment", "pas", "ne", "se", "sa", "son", "ses", "leur", "leurs",
}


def _normalize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _TIME_RE.sub(r"\1 \2", text)
    tokens = _WORD_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def normalize_text(text: str) -> str:
    """Forme normalisée d'une question complète (pour la détection de doublons)."""
    return " ".join(_normalize(text))


class FactEngine:
    def __init__(self, case: Case):
        self.case = case

    def known_facts(self, suspect: Suspect) -> list[Fact]:
        return [self.case.facts[fid] for fid in suspect.known_fact_ids if fid in self.case.facts]

    def relevant_facts(
        self, suspect: Suspect, question: str, *, top_k: int = config.RELEVANT_FACTS_TOP_K
    ) -> list[Fact]:
        """Renvoie TOUS les facts connus du suspect, triés par pertinence pour la question
        (recoupement de mots-clés, déterministe, sans LLM) — les plus pertinents en premier.

        Un suspect ne connaît de toute façon qu'un petit sous-ensemble de facts (quelques
        dizaines maximum) : plutôt que de tronquer arbitrairement à `top_k` et risquer de
        couper LE fait qui répond vraiment à la question (ex. l'affiliation de quelqu'un à
        une organisation, mentionnée nulle part dans le vocabulaire de la question), on donne
        tout ce que le suspect sait et on laisse l'acteur LLM — guidé par des consignes
        strictes de pertinence — choisir quoi utiliser. Le tri par score aide simplement le
        LLM à repérer en priorité les facts les plus probablement utiles."""
        known = self.known_facts(suspect)
        if not known:
            return []

        q_tokens = set(_normalize(question))
        if not q_tokens:
            return known

        scored: list[tuple[int, Fact]] = []
        unscored: list[Fact] = []
        for fact in known:
            kw_tokens = set()
            for kw in fact.keywords:
                kw_tokens.update(_normalize(kw))
            content_tokens = set(_normalize(fact.content))
            score = len(q_tokens & kw_tokens) * 2 + len(q_tokens & content_tokens)
            if score > 0:
                scored.append((score, fact))
            else:
                unscored.append(fact)

        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored] + unscored

    def secrets_for(self, suspect: Suspect) -> list[Fact]:
        return [self.case.facts[fid] for fid in suspect.secret_fact_ids if fid in self.case.facts]

    def lies_relevant(self, suspect: Suspect, relevant: list[Fact]) -> list[dict]:
        """Mensonges du suspect qui portent sur l'un des facts jugés pertinents pour la question."""
        relevant_ids = {f.id for f in relevant}
        return [
            {"fact_id": lie.fact_id, "lie_text": lie.lie_text}
            for lie in suspect.lies
            if lie.fact_id in relevant_ids
        ]

    def facts_by_ids(self, fact_ids: list[str]) -> list[Fact]:
        return [self.case.facts[fid] for fid in fact_ids if fid in self.case.facts]

"""ResponseValidator — anti-hallucination / anti-fuite sur les réponses de suspect (LLM3).

Vérification "best effort" par recoupement de texte et regex — pas une preuve sémantique
formelle, mais un filet de sécurité déterministe avant retry / fallback neutre.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Case
from .suspect_engine import SuspectContext

_CONFESSION_PATTERNS = [
    r"\bje suis (le |la )?(vrai[e]? )?coupable\b",
    r"\bc\'est (bien )?moi qui (l\'|le |la )?ai (tu[ée]|tué[e]?|assassin[ée])",
    r"\bj\'ai (tu[ée]|assassin[ée]|commis (le|ce) crime)\b",
    r"\bj\'avoue\b",
    r"\bje reconnais (avoir tu[ée]|le meurtre|le crime)\b",
    r"\bje l\'ai tu[ée]\b",
]
_CONFESSION_RE = re.compile("|".join(_CONFESSION_PATTERNS), re.IGNORECASE)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.lower()


def validate(response: dict, ctx: SuspectContext, case: Case) -> list[str]:
    issues: list[str] = []
    text = response.get("reponse", "")
    used_ids = set(response.get("fact_ids_utilises", []))

    if not text or not text.strip():
        issues.append("Réponse vide")
        return issues

    # --- Confession explicite interdite ---
    if _CONFESSION_RE.search(_fold(text)):
        issues.append("La réponse contient une confession explicite, interdite pour tous les suspects")

    # --- Facts utilisés doivent appartenir au périmètre autorisé pour cette question ---
    allowed_ids = {f.id for f in ctx.relevant_facts}
    leaked_ids = used_ids - allowed_ids
    if leaked_ids:
        issues.append(f"fact_ids_utilises hors périmètre autorisé : {sorted(leaked_ids)}")

    # --- Recoupement texte : contenu d'un fact NON autorisé recopié dans la réponse ---
    folded_response = _fold(text)
    forbidden_facts = [f for f in case.facts.values() if f.id not in allowed_ids]
    for fact in forbidden_facts:
        content = _fold(fact.content)
        # Ignorer les facts très courts (bruit trop probable) ; comparer par sous-chaîne
        # significative plutôt que l'intégralité (le LLM reformule).
        if len(content) < 12:
            continue
        # Découpe le fact en fragments de ~6 mots et cherche un recouvrement suspect.
        words = content.split()
        for i in range(0, max(len(words) - 5, 1)):
            fragment = " ".join(words[i : i + 6])
            if len(fragment) >= 20 and fragment in folded_response:
                issues.append(f"La réponse semble divulguer un fait non autorisé ({fact.id})")
                break

    return issues


NEUTRAL_FALLBACKS = [
    "Le suspect détourne le regard. « Je n'ai rien à ajouter là-dessus. »",
    "« Écoutez, je préfère ne pas m'étendre sur ce sujet. »",
    "Un silence, puis : « Vous devriez poser la question à quelqu'un d'autre. »",
    "« Je ne vois pas de quoi vous parlez. » répond-il, évasif.",
]


def fallback_response(seed: int = 0) -> dict:
    text = NEUTRAL_FALLBACKS[seed % len(NEUTRAL_FALLBACKS)]
    return {"reponse": text, "fact_ids_utilises": []}

"""SuspectEngine — construit le contexte MINIMAL envoyé au LLM pour incarner un suspect.

Le LLM ne reçoit jamais le dossier complet : uniquement ce que cette fonction assemble.
"""

from __future__ import annotations

from dataclasses import dataclass

from .facts import FactEngine
from .models import Case, Fact, Interrogation, Suspect


@dataclass
class SuspectContext:
    suspect: Suspect
    relevant_facts: list[Fact]
    secret_fact_ids: set[str]
    lies: list[dict]
    history: list[Interrogation]
    victim_name: str
    location: str
    investigation_moment: str


def build_context(
    case: Case,
    suspect: Suspect,
    fact_engine: FactEngine,
    question: str,
    history: list[Interrogation],
) -> SuspectContext:
    relevant = fact_engine.relevant_facts(suspect, question)
    lies = fact_engine.lies_relevant(suspect, relevant)
    return SuspectContext(
        suspect=suspect,
        relevant_facts=relevant,
        secret_fact_ids=set(suspect.secret_fact_ids),
        lies=lies,
        history=history,
        victim_name=case.victim_name,
        location=case.location,
        investigation_moment=case.investigation_moment,
    )

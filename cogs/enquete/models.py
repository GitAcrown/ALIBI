"""Dataclasses du dossier d'enquête — représentation immuable une fois générée/validée.

Ces classes ne portent aucune logique métier lourde : elles servent de structure
typée entre le générateur LLM, le validateur, le stockage SQLite et le reste du cog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

SuspectId = str  # "p01".."p12"

FactType = Literal["timeline", "alibi", "relation", "objet", "lieu", "autre"]
Genre = Literal["masculin", "feminin", "agenre"]


@dataclass
class Fact:
    id: str
    type: FactType
    content: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class Lie:
    fact_id: str
    lie_text: str


@dataclass
class Suspect:
    id: SuspectId
    name: str
    age: int
    gender: Genre
    role: str
    personality: str
    alibi_summary: str
    mobile: Optional[str]
    is_guilty: bool
    known_fact_ids: list[str] = field(default_factory=list)
    secret_fact_ids: list[str] = field(default_factory=list)
    lies: list[Lie] = field(default_factory=list)

    @property
    def suspicion_index(self) -> int:
        """Heuristique simple du nombre d'indices apparents pointant vers ce suspect
        (secrets + mensonges + mobile) — utilisée pour le badge WORST_ACCUSATION."""
        score = len(self.secret_fact_ids) + len(self.lies)
        if self.mobile:
            score += 1
        return score


@dataclass
class TimelineEntry:
    id: str
    time: str
    location: str
    actor_suspect_id: Optional[SuspectId]
    description: str


@dataclass
class Evidence:
    id: str
    description: str
    is_public: bool
    related_fact_ids: list[str] = field(default_factory=list)


@dataclass
class Case:
    """Le dossier complet — immuable une fois stocké."""

    case_id: str
    guild_id: int
    status: Literal["generating", "active", "resolved", "failed"]
    context_prompt: str
    title: str
    summary: str
    victim_name: str
    victim_description: str
    crime_description: str
    method: str
    weapon: str
    location: str
    time_of_death: str
    investigation_moment: str
    timeline: list[TimelineEntry]
    facts: dict[str, Fact]
    evidence: dict[str, Evidence]
    suspects: dict[SuspectId, Suspect]
    guilty_suspect_id: SuspectId
    motive: str
    true_timeline_summary: str
    key_evidence_ids: list[str]
    main_lies_summary: str
    created_at: datetime
    deadline_at: datetime
    channel_id: Optional[int] = None
    announce_message_id: Optional[int] = None
    resolved_at: Optional[datetime] = None
    resolution_monologue: Optional[str] = None
    case_pk: Optional[int] = None

    def public_evidence(self) -> list[Evidence]:
        return [e for e in self.evidence.values() if e.is_public]

    def suspect_by_name(self, name: str) -> Optional[Suspect]:
        name_low = name.strip().lower()
        for s in self.suspects.values():
            if s.name.lower() == name_low:
                return s
        return None


@dataclass
class Interrogation:
    id: int
    case_pk: int
    player_id: int
    suspect_id: SuspectId
    question_raw: str
    question_normalized: str
    fact_ids_used: list[str]
    response_text: str
    is_duplicate: bool
    created_at: datetime


@dataclass
class Accusation:
    case_pk: int
    player_id: int
    suspect_id: SuspectId
    first_created_at: datetime
    last_created_at: datetime
    change_count: int
    motive_guess: Optional[str] = None


@dataclass
class PlayerResult:
    case_pk: int
    player_id: int
    accused_suspect_id: Optional[SuspectId]
    correct: bool
    badges: list[str]
    motive_guess: Optional[str] = None
    # Bonus de points pour un mobile deviné juste/proche (0, config.MOTIVE_BONUS_CLOSE ou
    # config.MOTIVE_BONUS_EXACT) — voir scoring.py.
    motive_points: int = 0
    # Total : points accusation correcte + bonus mobile (voir scoring.compute_results).
    points: int = 0


@dataclass
class Schedule:
    """Créneau quotidien de lancement automatique d'enquête."""

    id: int
    guild_id: int
    channel_id: int
    hour: int
    minute: int
    duration_minutes: int
    enabled: bool
    context_prompt: str
    last_fired_date: Optional[str]  # YYYY-MM-DD (fuseau SCHEDULE_TIMEZONE)
    created_at: datetime

    @property
    def time_label(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def duration_label(self) -> str:
        h, m = divmod(self.duration_minutes, 60)
        if h and m:
            return f"{h}h{m:02d}"
        if h:
            return f"{h}h"
        return f"{m} min"

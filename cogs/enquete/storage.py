"""Persistance SQLite du cog Enquête — une base par serveur (via utils/dataio.py).

Le dossier (`Case`) est stocké de façon dénormalisée (tables `cases`, `facts`,
`suspects`, `evidence`) une fois pour toutes à la validation : plus aucune écriture
sur ces lignes après le passage en statut `active`, hormis les champs de clôture
(`resolved_at`, `resolution_monologue`) à la résolution. C'est le verrou technique
qui matérialise « la vérité ne change jamais ».
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord

from utils.dataio import CogData, TableBuilder, get_instance

DATA_DIR = Path("cogs/enquete/data")

from .models import (
    Accusation,
    Case,
    Evidence,
    Fact,
    Interrogation,
    Lie,
    PlayerResult,
    Suspect,
    TimelineEntry,
)

CASES_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS cases (
        case_pk INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT UNIQUE NOT NULL,
        guild_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        context_prompt TEXT,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        victim_name TEXT NOT NULL,
        victim_description TEXT NOT NULL,
        crime_description TEXT NOT NULL,
        method TEXT NOT NULL,
        weapon TEXT NOT NULL,
        location TEXT NOT NULL,
        time_of_death TEXT NOT NULL,
        investigation_moment TEXT NOT NULL DEFAULT '',
        timeline_json TEXT NOT NULL,
        guilty_suspect_id TEXT NOT NULL,
        motive TEXT NOT NULL,
        true_timeline_summary TEXT NOT NULL,
        key_evidence_ids_json TEXT NOT NULL,
        main_lies_summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deadline_at TEXT NOT NULL,
        channel_id INTEGER,
        announce_message_id INTEGER,
        resolved_at TEXT,
        resolution_monologue TEXT
    )"""
)

FACTS_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS facts (
        case_pk INTEGER NOT NULL,
        fact_id TEXT NOT NULL,
        type TEXT NOT NULL,
        content TEXT NOT NULL,
        keywords_json TEXT NOT NULL,
        PRIMARY KEY (case_pk, fact_id)
    )"""
)

SUSPECTS_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS suspects (
        case_pk INTEGER NOT NULL,
        suspect_id TEXT NOT NULL,
        name TEXT NOT NULL,
        age INTEGER NOT NULL,
        gender TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        personality TEXT NOT NULL,
        alibi_summary TEXT NOT NULL,
        mobile TEXT,
        is_guilty INTEGER NOT NULL,
        known_fact_ids_json TEXT NOT NULL,
        secret_fact_ids_json TEXT NOT NULL,
        lies_json TEXT NOT NULL,
        PRIMARY KEY (case_pk, suspect_id)
    )"""
)

EVIDENCE_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS evidence (
        case_pk INTEGER NOT NULL,
        evidence_id TEXT NOT NULL,
        description TEXT NOT NULL,
        is_public INTEGER NOT NULL,
        related_fact_ids_json TEXT NOT NULL,
        reveal_at TEXT,
        PRIMARY KEY (case_pk, evidence_id)
    )"""
)

INTERROGATIONS_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS interrogations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_pk INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        suspect_id TEXT NOT NULL,
        question_raw TEXT NOT NULL,
        question_normalized TEXT NOT NULL,
        fact_ids_used_json TEXT NOT NULL,
        response_text TEXT NOT NULL,
        is_duplicate INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )"""
)

ACCUSATIONS_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS accusations (
        case_pk INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        suspect_id TEXT NOT NULL,
        first_created_at TEXT NOT NULL,
        last_created_at TEXT NOT NULL,
        change_count INTEGER NOT NULL,
        PRIMARY KEY (case_pk, player_id)
    )"""
)

RESULTS_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS results (
        case_pk INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        accused_suspect_id TEXT,
        correct INTEGER NOT NULL,
        badges_json TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (case_pk, player_id)
    )"""
)

# Historique compact — indépendant des tables détaillées, alimente /historique
# même si les tables ci-dessus venaient à être purgées un jour.
CASE_SUMMARIES_TABLE = TableBuilder(
    """CREATE TABLE IF NOT EXISTS case_summaries (
        case_id TEXT PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        victim_name TEXT NOT NULL,
        guilty_name TEXT NOT NULL,
        winners_json TEXT NOT NULL,
        mvp_json TEXT NOT NULL DEFAULT '[]',
        resolved_at TEXT NOT NULL
    )"""
)

ALL_TABLES = (
    CASES_TABLE,
    FACTS_TABLE,
    SUSPECTS_TABLE,
    EVIDENCE_TABLE,
    INTERROGATIONS_TABLE,
    ACCUSATIONS_TABLE,
    RESULTS_TABLE,
    CASE_SUMMARIES_TABLE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def new_case_id() -> str:
    """ID court, lisible, unique — ex. 7F3A2C1B."""
    return uuid.uuid4().hex[:8].upper()


class EnqueteStorage:
    """Façade de persistance pour un serveur donné."""

    def __init__(self, cog_data: CogData, guild: discord.Guild):
        cog_data.link(discord.Guild, *ALL_TABLES)
        self._db = cog_data.get(guild)
        self.guild_id = guild.id
        self._migrated = False

    async def _ensure_migrated(self) -> None:
        """Ajoute les colonnes manquantes sur une base créée par une version antérieure.

        `CREATE TABLE IF NOT EXISTS` ne modifie jamais une table existante : sans ce
        filet, une base créée avant l'ajout d'une colonne resterait bloquée en erreur SQL."""
        if self._migrated:
            return
        self._migrated = True
        cols = await self._db.fetchall("PRAGMA table_info(suspects)")
        names = {c["name"] for c in cols}
        if names and "role" not in names:
            await self._db.execute("ALTER TABLE suspects ADD COLUMN role TEXT NOT NULL DEFAULT ''")

        cols = await self._db.fetchall("PRAGMA table_info(case_summaries)")
        names = {c["name"] for c in cols}
        if names and "mvp_json" not in names:
            await self._db.execute("ALTER TABLE case_summaries ADD COLUMN mvp_json TEXT NOT NULL DEFAULT '[]'")

        cols = await self._db.fetchall("PRAGMA table_info(cases)")
        names = {c["name"] for c in cols}
        if names and "investigation_moment" not in names:
            await self._db.execute(
                "ALTER TABLE cases ADD COLUMN investigation_moment TEXT NOT NULL DEFAULT ''"
            )

        cols = await self._db.fetchall("PRAGMA table_info(evidence)")
        names = {c["name"] for c in cols}
        if names and "reveal_at" not in names:
            await self._db.execute("ALTER TABLE evidence ADD COLUMN reveal_at TEXT")

    # ------------------------------------------------------------------
    # Cycle de vie du dossier
    # ------------------------------------------------------------------

    async def get_active_case(self) -> Optional[Case]:
        row = await self._db.fetchone(
            "SELECT case_pk FROM cases WHERE guild_id=? AND status IN ('generating','active') "
            "ORDER BY case_pk DESC LIMIT 1",
            self.guild_id,
        )
        if row is None:
            return None
        return await self.get_case(row["case_pk"])

    async def get_case(self, case_pk: int) -> Optional[Case]:
        row = await self._db.fetchone("SELECT * FROM cases WHERE case_pk=?", case_pk)
        if row is None:
            return None
        return await self._hydrate_case(row)

    async def get_case_by_case_id(self, case_id: str) -> Optional[Case]:
        row = await self._db.fetchone("SELECT * FROM cases WHERE case_id=?", case_id)
        if row is None:
            return None
        return await self._hydrate_case(row)

    async def _hydrate_case(self, row) -> Case:
        await self._ensure_migrated()
        case_pk = row["case_pk"]
        fact_rows = await self._db.fetchall("SELECT * FROM facts WHERE case_pk=?", case_pk)
        facts = {
            r["fact_id"]: Fact(
                id=r["fact_id"], type=r["type"], content=r["content"],
                keywords=json.loads(r["keywords_json"]),
            )
            for r in fact_rows
        }

        suspect_rows = await self._db.fetchall("SELECT * FROM suspects WHERE case_pk=?", case_pk)
        suspects = {
            r["suspect_id"]: Suspect(
                id=r["suspect_id"],
                name=r["name"],
                age=r["age"],
                gender=r["gender"],
                role=r["role"] if "role" in r.keys() else "",
                personality=r["personality"],
                alibi_summary=r["alibi_summary"],
                mobile=r["mobile"],
                is_guilty=bool(r["is_guilty"]),
                known_fact_ids=json.loads(r["known_fact_ids_json"]),
                secret_fact_ids=json.loads(r["secret_fact_ids_json"]),
                lies=[Lie(**lie) for lie in json.loads(r["lies_json"])],
            )
            for r in suspect_rows
        }

        evidence_rows = await self._db.fetchall("SELECT * FROM evidence WHERE case_pk=?", case_pk)
        evidence = {
            r["evidence_id"]: Evidence(
                id=r["evidence_id"], description=r["description"],
                is_public=bool(r["is_public"]),
                related_fact_ids=json.loads(r["related_fact_ids_json"]),
            )
            for r in evidence_rows
        }

        timeline = [TimelineEntry(**t) for t in json.loads(row["timeline_json"])]

        return Case(
            case_pk=case_pk,
            case_id=row["case_id"],
            guild_id=row["guild_id"],
            status=row["status"],
            context_prompt=row["context_prompt"] or "",
            title=row["title"],
            summary=row["summary"],
            victim_name=row["victim_name"],
            victim_description=row["victim_description"],
            crime_description=row["crime_description"],
            method=row["method"],
            weapon=row["weapon"],
            location=row["location"],
            time_of_death=row["time_of_death"],
            investigation_moment=row["investigation_moment"] if "investigation_moment" in row.keys() else "",
            timeline=timeline,
            facts=facts,
            evidence=evidence,
            suspects=suspects,
            guilty_suspect_id=row["guilty_suspect_id"],
            motive=row["motive"],
            true_timeline_summary=row["true_timeline_summary"],
            key_evidence_ids=json.loads(row["key_evidence_ids_json"]),
            main_lies_summary=row["main_lies_summary"],
            created_at=_parse_dt(row["created_at"]),
            deadline_at=_parse_dt(row["deadline_at"]),
            channel_id=row["channel_id"],
            announce_message_id=row["announce_message_id"],
            resolved_at=_parse_dt(row["resolved_at"]) if row["resolved_at"] else None,
            resolution_monologue=row["resolution_monologue"],
        )

    async def create_generating_case(self, guild_id: int, context_prompt: str) -> int:
        """Crée une ligne placeholder (status='generating') pour verrouiller le serveur
        pendant la génération, avant même de connaître le contenu du dossier."""
        await self._ensure_migrated()
        now = _now_iso()
        await self._db.execute(
            """INSERT INTO cases (
                case_id, guild_id, status, context_prompt, title, summary, victim_name,
                victim_description, crime_description, method, weapon, location, time_of_death,
                investigation_moment, timeline_json, guilty_suspect_id, motive,
                true_timeline_summary, key_evidence_ids_json, main_lies_summary, created_at,
                deadline_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            new_case_id(), guild_id, "generating", context_prompt, "", "", "", "", "", "", "",
            "", "", "", "[]", "", "", "", "[]", "", now, now,
        )
        row = await self._db.fetchone("SELECT case_pk FROM cases WHERE guild_id=? ORDER BY case_pk DESC LIMIT 1", guild_id)
        return row["case_pk"]

    async def store_generated_case(
        self,
        case_pk: int,
        *,
        case_id: str,
        title: str,
        summary: str,
        victim_name: str,
        victim_description: str,
        crime_description: str,
        method: str,
        weapon: str,
        location: str,
        time_of_death: str,
        investigation_moment: str,
        timeline: list[TimelineEntry],
        facts: dict[str, Fact],
        evidence: dict[str, Evidence],
        suspects: dict[str, Suspect],
        guilty_suspect_id: str,
        motive: str,
        true_timeline_summary: str,
        key_evidence_ids: list[str],
        main_lies_summary: str,
        deadline_at: datetime,
        channel_id: int,
        evidence_reveal_at: Optional[dict[str, datetime]] = None,
    ) -> None:
        """Écrit le dossier validé — appelé une seule fois, transition generating -> active."""
        await self._ensure_migrated()
        await self._db.execute(
            """UPDATE cases SET
                case_id=?, status='active', title=?, summary=?, victim_name=?, victim_description=?,
                crime_description=?, method=?, weapon=?, location=?, time_of_death=?,
                investigation_moment=?, timeline_json=?,
                guilty_suspect_id=?, motive=?, true_timeline_summary=?, key_evidence_ids_json=?,
                main_lies_summary=?, deadline_at=?, channel_id=?
            WHERE case_pk=?""",
            case_id, title, summary, victim_name, victim_description, crime_description, method,
            weapon, location, time_of_death, investigation_moment,
            json.dumps([t.__dict__ for t in timeline], ensure_ascii=False),
            guilty_suspect_id, motive, true_timeline_summary,
            json.dumps(key_evidence_ids, ensure_ascii=False), main_lies_summary,
            deadline_at.isoformat(), channel_id, case_pk,
        )
        for fact in facts.values():
            await self._db.execute(
                "INSERT INTO facts (case_pk, fact_id, type, content, keywords_json) VALUES (?,?,?,?,?)",
                case_pk, fact.id, fact.type, fact.content,
                json.dumps(fact.keywords, ensure_ascii=False),
            )
        for suspect in suspects.values():
            await self._db.execute(
                """INSERT INTO suspects (
                    case_pk, suspect_id, name, age, gender, role, personality, alibi_summary, mobile,
                    is_guilty, known_fact_ids_json, secret_fact_ids_json, lies_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                case_pk, suspect.id, suspect.name, suspect.age, suspect.gender, suspect.role,
                suspect.personality, suspect.alibi_summary, suspect.mobile, int(suspect.is_guilty),
                json.dumps(suspect.known_fact_ids, ensure_ascii=False),
                json.dumps(suspect.secret_fact_ids, ensure_ascii=False),
                json.dumps([lie.__dict__ for lie in suspect.lies], ensure_ascii=False),
            )
        evidence_reveal_at = evidence_reveal_at or {}
        for ev in evidence.values():
            reveal_at = evidence_reveal_at.get(ev.id)
            await self._db.execute(
                "INSERT INTO evidence (case_pk, evidence_id, description, is_public, "
                "related_fact_ids_json, reveal_at) VALUES (?,?,?,?,?,?)",
                case_pk, ev.id, ev.description, int(ev.is_public),
                json.dumps(ev.related_fact_ids, ensure_ascii=False),
                reveal_at.isoformat() if reveal_at else None,
            )

    async def mark_case_failed(self, case_pk: int) -> None:
        await self._db.execute("UPDATE cases SET status='failed' WHERE case_pk=?", case_pk)

    async def fail_generating_cases(self) -> int:
        """Abandonne les enquêtes coincées en `generating` (crash / redémarrage mid-génération).

        Une génération ne survit jamais à un redémarrage du bot : ces lignes bloqueraient
        sinon `/enquete` à jamais via `get_active_case`.
        """
        before = await self._db.fetchall(
            "SELECT case_pk FROM cases WHERE guild_id=? AND status='generating'",
            self.guild_id,
        )
        if not before:
            return 0
        await self._db.execute(
            "UPDATE cases SET status='failed' WHERE guild_id=? AND status='generating'",
            self.guild_id,
        )
        return len(before)

    async def delete_case(self, case_pk: int) -> None:
        for table in ("facts", "suspects", "evidence", "interrogations", "accusations", "results"):
            await self._db.execute(f"DELETE FROM {table} WHERE case_pk=?", case_pk, commit=False)
        await self._db.execute("DELETE FROM cases WHERE case_pk=?", case_pk)

    async def set_announce_message(self, case_pk: int, message_id: int) -> None:
        await self._db.execute("UPDATE cases SET announce_message_id=? WHERE case_pk=?", message_id, case_pk)

    async def pop_due_evidence_reveals(self, case_pk: int) -> list[Evidence]:
        """Bascule is_public=1 les preuves dont l'échéance de révélation est atteinte et les
        renvoie — appelé en boucle pour publier un « bulletin » à mi-partie sans intervention
        du LLM (la preuve existe déjà, seule sa visibilité change : la vérité ne bouge pas)."""
        await self._ensure_migrated()
        now = _now_iso()
        rows = await self._db.fetchall(
            "SELECT * FROM evidence WHERE case_pk=? AND is_public=0 AND reveal_at IS NOT NULL "
            "AND reveal_at<=?",
            case_pk, now,
        )
        if not rows:
            return []
        due = [
            Evidence(
                id=r["evidence_id"], description=r["description"], is_public=True,
                related_fact_ids=json.loads(r["related_fact_ids_json"]),
            )
            for r in rows
        ]
        await self._db.execute(
            "UPDATE evidence SET is_public=1 WHERE case_pk=? AND is_public=0 AND reveal_at IS NOT NULL "
            "AND reveal_at<=?",
            case_pk, now,
        )
        return due

    async def active_case_past_deadline(self) -> Optional[int]:
        """Renvoie le case_pk de CE serveur si son enquête active a dépassé sa deadline.

        La base est isolée par serveur (une base SQLite par `discord.Guild`) : le timer
        global du cog itère donc sur `bot.guilds` et interroge cette méthode pour chacun,
        plutôt que de faire une requête cross-serveurs (impossible avec ce modèle de stockage)."""
        row = await self._db.fetchone(
            "SELECT case_pk FROM cases WHERE guild_id=? AND status='active' AND deadline_at<=?",
            self.guild_id, _now_iso(),
        )
        return row["case_pk"] if row else None

    # ------------------------------------------------------------------
    # Interrogatoires
    # ------------------------------------------------------------------

    async def count_player_questions(self, case_pk: int, player_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) as n FROM interrogations WHERE case_pk=? AND player_id=?",
            case_pk, player_id,
        )
        return row["n"] if row else 0

    async def get_player_suspect_history(
        self, case_pk: int, player_id: int, suspect_id: str
    ) -> list[Interrogation]:
        rows = await self._db.fetchall(
            "SELECT * FROM interrogations WHERE case_pk=? AND player_id=? AND suspect_id=? ORDER BY id ASC",
            case_pk, player_id, suspect_id,
        )
        return [self._hydrate_interrogation(r) for r in rows]

    @staticmethod
    def _hydrate_interrogation(r) -> Interrogation:
        return Interrogation(
            id=r["id"], case_pk=r["case_pk"], player_id=r["player_id"], suspect_id=r["suspect_id"],
            question_raw=r["question_raw"], question_normalized=r["question_normalized"],
            fact_ids_used=json.loads(r["fact_ids_used_json"]), response_text=r["response_text"],
            is_duplicate=bool(r["is_duplicate"]), created_at=_parse_dt(r["created_at"]),
        )

    async def record_interrogation(
        self,
        case_pk: int,
        player_id: int,
        suspect_id: str,
        question_raw: str,
        question_normalized: str,
        fact_ids_used: list[str],
        response_text: str,
        is_duplicate: bool,
    ) -> None:
        await self._db.execute(
            """INSERT INTO interrogations (
                case_pk, player_id, suspect_id, question_raw, question_normalized,
                fact_ids_used_json, response_text, is_duplicate, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            case_pk, player_id, suspect_id, question_raw, question_normalized,
            json.dumps(fact_ids_used, ensure_ascii=False), response_text, int(is_duplicate), _now_iso(),
        )

    # ------------------------------------------------------------------
    # Accusations
    # ------------------------------------------------------------------

    async def upsert_accusation(self, case_pk: int, player_id: int, suspect_id: str) -> Accusation:
        existing = await self._db.fetchone(
            "SELECT * FROM accusations WHERE case_pk=? AND player_id=?", case_pk, player_id
        )
        now = _now_iso()
        if existing is None:
            await self._db.execute(
                """INSERT INTO accusations (case_pk, player_id, suspect_id, first_created_at, last_created_at, change_count)
                VALUES (?,?,?,?,?,0)""",
                case_pk, player_id, suspect_id, now, now,
            )
            return Accusation(case_pk, player_id, suspect_id, _parse_dt(now), _parse_dt(now), 0)
        change_count = existing["change_count"] + (1 if existing["suspect_id"] != suspect_id else 0)
        await self._db.execute(
            "UPDATE accusations SET suspect_id=?, last_created_at=?, change_count=? WHERE case_pk=? AND player_id=?",
            suspect_id, now, change_count, case_pk, player_id,
        )
        return Accusation(
            case_pk, player_id, suspect_id, _parse_dt(existing["first_created_at"]), _parse_dt(now), change_count
        )

    async def get_accusation(self, case_pk: int, player_id: int) -> Optional[Accusation]:
        row = await self._db.fetchone(
            "SELECT * FROM accusations WHERE case_pk=? AND player_id=?", case_pk, player_id
        )
        if row is None:
            return None
        return Accusation(
            case_pk=row["case_pk"], player_id=row["player_id"], suspect_id=row["suspect_id"],
            first_created_at=_parse_dt(row["first_created_at"]),
            last_created_at=_parse_dt(row["last_created_at"]), change_count=row["change_count"],
        )

    async def get_all_accusations(self, case_pk: int) -> list[Accusation]:
        rows = await self._db.fetchall("SELECT * FROM accusations WHERE case_pk=?", case_pk)
        return [
            Accusation(
                case_pk=r["case_pk"], player_id=r["player_id"], suspect_id=r["suspect_id"],
                first_created_at=_parse_dt(r["first_created_at"]),
                last_created_at=_parse_dt(r["last_created_at"]), change_count=r["change_count"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Résultats / résolution / hall of fame
    # ------------------------------------------------------------------

    async def save_resolution(self, case_pk: int, monologue: str) -> None:
        await self._db.execute(
            "UPDATE cases SET status='resolved', resolved_at=?, resolution_monologue=? WHERE case_pk=?",
            _now_iso(), monologue, case_pk,
        )

    async def save_results(self, results: list[PlayerResult]) -> None:
        now = _now_iso()
        for r in results:
            await self._db.execute(
                """INSERT OR REPLACE INTO results (case_pk, player_id, accused_suspect_id, correct, badges_json, computed_at)
                VALUES (?,?,?,?,?,?)""",
                r.case_pk, r.player_id, r.accused_suspect_id, int(r.correct),
                json.dumps(r.badges, ensure_ascii=False), now,
            )

    async def get_results(self, case_pk: int) -> list[PlayerResult]:
        rows = await self._db.fetchall("SELECT * FROM results WHERE case_pk=?", case_pk)
        return [
            PlayerResult(
                case_pk=r["case_pk"], player_id=r["player_id"], accused_suspect_id=r["accused_suspect_id"],
                correct=bool(r["correct"]), badges=json.loads(r["badges_json"]),
            )
            for r in rows
        ]

    async def get_last_resolved_case(self, guild_id: int) -> Optional[Case]:
        row = await self._db.fetchone(
            "SELECT case_pk FROM cases WHERE guild_id=? AND status='resolved' ORDER BY resolved_at DESC LIMIT 1",
            guild_id,
        )
        if row is None:
            return None
        return await self.get_case(row["case_pk"])

    async def add_hall_of_fame_entry(
        self, case_id: str, guild_id: int, title: str, summary: str,
        victim_name: str, guilty_name: str, winners: list[str], mvp: list[str],
    ) -> None:
        await self._ensure_migrated()
        await self._db.execute(
            """INSERT OR REPLACE INTO case_summaries (
                case_id, guild_id, title, summary, victim_name, guilty_name, winners_json,
                mvp_json, resolved_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            case_id, guild_id, title, summary, victim_name, guilty_name,
            json.dumps(winners, ensure_ascii=False), json.dumps(mvp, ensure_ascii=False), _now_iso(),
        )

    async def list_hall_of_fame(self, guild_id: int, limit: int = 10) -> list[dict]:
        await self._ensure_migrated()
        rows = await self._db.fetchall(
            "SELECT * FROM case_summaries WHERE guild_id=? ORDER BY resolved_at DESC LIMIT ?",
            guild_id, limit,
        )
        return [dict(r) for r in rows]


def guild_db_path(guild_id: int) -> Path:
    """Chemin de la base d'un serveur — créé uniquement au premier vrai usage."""
    return DATA_DIR / f"guild_{guild_id}.db"


def has_guild_data(guild_id: int) -> bool:
    return guild_db_path(guild_id).exists()


def iter_guild_ids_with_data() -> list[int]:
    """IDs des serveurs qui ont déjà une base (pas de création)."""
    if not DATA_DIR.is_dir():
        return []
    ids: list[int] = []
    for path in DATA_DIR.glob("guild_*.db"):
        m = re.fullmatch(r"guild_(\d+)\.db", path.name)
        if m:
            ids.append(int(m.group(1)))
    return ids


def fail_all_generating_cases_sync() -> int:
    """Nettoyage synchrone au démarrage — sans objet Guild Discord requis.

    Marque `failed` toutes les enquêtes encore en `generating` dans toutes les bases
    locales. Une génération ne peut pas survivre à un redémarrage du process.
    """
    import sqlite3

    total = 0
    if not DATA_DIR.is_dir():
        return 0
    for path in DATA_DIR.glob("guild_*.db"):
        try:
            conn = sqlite3.connect(path)
            try:
                cur = conn.execute(
                    "UPDATE cases SET status='failed' WHERE status='generating'"
                )
                total += cur.rowcount
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return total


def get_storage(bot_cog, guild: discord.Guild) -> EnqueteStorage:
    """Ouvre (et crée si besoin) le stockage du serveur.

    À réserver aux écritures / lancement d'enquête. Pour les lectures et les
    boucles globales, préférer `get_storage_if_exists`.
    """
    cog_data = get_instance("enquete")
    return EnqueteStorage(cog_data, guild)


def get_storage_if_exists(bot_cog, guild: discord.Guild) -> Optional[EnqueteStorage]:
    """Renvoie le stockage seulement si une base existe déjà pour ce serveur."""
    if not has_guild_data(guild.id):
        return None
    return get_storage(bot_cog, guild)

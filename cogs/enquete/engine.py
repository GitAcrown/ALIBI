"""CaseEngine — cycle de vie d'une enquête : génération, verrouillage, résolution.

Orchestration uniquement : la génération/validation/audit vivent dans generator.py /
validator.py / auditor.py, la résolution narrative dans actor.py, le scoring dans
scoring.py. Ce module colle les morceaux ensemble et parle à `storage.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import discord

from common.llm.client import LLMClient

from . import config, scoring
from .actor import LLMActor, strip_slot_ids
from .auditor import ScenarioAuditor
from .generator import ScenarioGenerator
from .models import Case, Evidence, Fact, Lie, PlayerResult, Suspect, TimelineEntry
from .storage import EnqueteStorage, get_storage, get_storage_if_exists, new_case_id
from . import validator as scenario_validator

logger = logging.getLogger("enquete.engine")


class GenerationFailed(Exception):
    def __init__(self, attempts_log: list[str]):
        self.attempts_log = attempts_log
        super().__init__("Échec de génération du dossier après plusieurs tentatives.")


class CaseAlreadyActive(Exception):
    pass


def _schedule_evidence_reveals(
    evidence: dict[str, Evidence], now: datetime, deadline: datetime
) -> dict[str, datetime]:
    """Programme la révélation progressive de quelques preuves gardées privées à la
    génération — pour que des « bulletins » ponctuent la partie sur plusieurs heures au
    lieu que tout le contenu jouable soit disponible (ou invisible) dès la première minute.

    La preuve elle-même ne change jamais (la vérité est fixe) : seule sa visibilité
    (`is_public`) bascule automatiquement à l'heure programmée. On laisse toujours au moins
    la moitié des preuves privées intactes pour la révélation finale, afin de ne pas vider
    le dossier de tout suspense avant la résolution."""
    hidden = sorted(
        (e for e in evidence.values() if not e.is_public), key=lambda e: e.id
    )
    if len(hidden) < 2:
        return {}
    count = min(2, len(hidden) // 2)
    if count <= 0:
        return {}
    total = (deadline - now).total_seconds()
    if total <= 0:
        return {}
    schedule: dict[str, datetime] = {}
    for i in range(count):
        fraction = (i + 1) / (count + 1)
        schedule[hidden[i].id] = now + timedelta(seconds=total * fraction)
    return schedule


def _raw_to_pieces(raw: dict) -> dict:
    """Convertit le JSON brut de LLM1 (déjà validé) en structures typées pour le stockage."""
    facts = {
        f["id"]: Fact(id=f["id"], type=f["type"], content=f["content"], keywords=f["keywords"])
        for f in raw["facts"]
    }
    evidence = {
        e["id"]: Evidence(
            id=e["id"], description=e["description"], is_public=e["is_public"],
            related_fact_ids=e["related_fact_ids"],
        )
        for e in raw["evidence"]
    }
    suspects = {
        sid: Suspect(
            id=sid,
            name=s["name"],
            age=s["age"],
            gender=s["gender"],
            role=s["role"],
            personality=s["personality"],
            alibi_summary=s["alibi_summary"],
            mobile=s.get("mobile"),
            is_guilty=s["is_guilty"],
            known_fact_ids=s["known_fact_ids"],
            secret_fact_ids=s["secret_fact_ids"],
            lies=[Lie(fact_id=l["fact_id"], lie_text=l["lie_text"]) for l in s["lies"]],
        )
        for sid, s in raw["suspects"].items()
    }
    timeline = [
        TimelineEntry(
            id=t["id"], time=t["time"], location=t["location"],
            actor_suspect_id=t.get("actor_suspect_id"), description=t["description"],
        )
        for t in raw["timeline"]
    ]
    return {
        "title": raw["title"],
        "summary": raw["summary"],
        "victim_name": raw["victim_name"],
        "victim_description": raw["victim_description"],
        "crime_description": raw["crime_description"],
        "method": raw["method"],
        "weapon": raw["weapon"],
        "location": raw["location"],
        "time_of_death": raw["time_of_death"],
        "investigation_moment": raw["investigation_moment"],
        "timeline": timeline,
        "facts": facts,
        "evidence": evidence,
        "suspects": suspects,
        "guilty_suspect_id": raw["guilty_suspect_id"],
        "motive": raw["motive"],
        "true_timeline_summary": raw["true_timeline_summary"],
        "key_evidence_ids": raw["key_evidence_ids"],
        "main_lies_summary": raw["main_lies_summary"],
    }


class CaseEngine:
    def __init__(self, client: LLMClient):
        self.client = client
        self.generator = ScenarioGenerator(client)
        self.auditor = ScenarioAuditor(client)
        self.actor = LLMActor(client)

    def storage(self, guild: discord.Guild) -> EnqueteStorage:
        """Crée la base si absente — pour lancement / écriture uniquement."""
        return get_storage(self, guild)

    def storage_if_exists(self, guild: discord.Guild) -> Optional[EnqueteStorage]:
        """Lecture seule : None si ce serveur n'a encore jamais joué."""
        return get_storage_if_exists(self, guild)

    # ------------------------------------------------------------------
    # Génération
    # ------------------------------------------------------------------

    async def start_case(
        self,
        guild: discord.Guild,
        channel_id: int,
        context_prompt: str,
        portraits_meta: dict,
        *,
        duration_minutes: Optional[int] = None,
        progress_cb: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    ) -> Case:
        existing = self.storage_if_exists(guild)
        if existing is not None:
            # Génération interrompue (redémarrage, crash) → ne doit pas bloquer le serveur.
            n = await existing.fail_generating_cases()
            if n:
                logger.warning(
                    "Abandon de %d enquête(s) coincée(s) en génération sur %s", n, guild.id
                )
            active = await existing.get_active_case()
            if active is not None and active.status == "active":
                raise CaseAlreadyActive()

        # Première écriture pour ce serveur : crée la base maintenant seulement.
        store = self.storage(guild)
        case_pk = await store.create_generating_case(guild.id, context_prompt)

        async def _notify(attempt: int, note: str) -> None:
            if progress_cb is not None:
                try:
                    await progress_cb(attempt, config.MAX_GENERATION_ATTEMPTS, note)
                except Exception:
                    logger.exception("progress_cb a échoué (ignoré)")

        all_issues_log: list[str] = []
        current_issues: Optional[list[str]] = None
        last_candidate: Optional[dict] = None
        raw: Optional[dict] = None

        try:
            for attempt in range(1, config.MAX_GENERATION_ATTEMPTS + 1):
                if last_candidate is None:
                    await _notify(attempt, "Rédaction du rapport initial…")
                else:
                    await _notify(attempt, "Réécriture des pages incohérentes…")

                try:
                    candidate = await self.generator.generate(
                        context_prompt,
                        portraits_meta,
                        previous_candidate=last_candidate,
                        issues=current_issues,
                    )
                except Exception as e:
                    logger.error("Génération LLM1 échouée (tentative %d) : %s", attempt, e)
                    all_issues_log.append(f"Tentative {attempt} — erreur technique : {e}")
                    continue

                last_candidate = candidate

                # Code pur (ms) — ce n'est PAS un LLM. L'étape lente ensuite = ScenarioAuditor.
                await _notify(attempt, "Contrôle des scellés…")
                issues = scenario_validator.validate(candidate)
                if issues:
                    logger.warning(
                        "Dossier rejeté par ScenarioValidator/code (tentative %d) : %s",
                        attempt, issues,
                    )
                    all_issues_log.extend(issues)
                    current_issues = issues
                    continue

                await _notify(attempt, "Contre-expertise du bureau…")
                audit_valid, audit_issues = await self.auditor.audit(candidate)
                if not audit_valid:
                    logger.warning(
                        "Dossier rejeté par ScenarioAuditor/LLM (tentative %d) : %s",
                        attempt, audit_issues,
                    )
                    all_issues_log.extend(audit_issues)
                    current_issues = audit_issues
                    continue

                raw = candidate
                break
        except Exception:
            await store.mark_case_failed(case_pk)
            raise

        if raw is None:
            await store.mark_case_failed(case_pk)
            raise GenerationFailed(all_issues_log)

        minutes = (
            duration_minutes
            if duration_minutes and duration_minutes > 0
            else config.default_duration_minutes()
        )
        pieces = _raw_to_pieces(raw)
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(minutes=minutes)
        case_id = new_case_id()
        evidence_reveal_at = _schedule_evidence_reveals(pieces["evidence"], now, deadline)
        await store.store_generated_case(
            case_pk, case_id=case_id, deadline_at=deadline, channel_id=channel_id,
            evidence_reveal_at=evidence_reveal_at, **pieces
        )
        case = await store.get_case(case_pk)
        logger.info(
            "Enquête %s active sur le serveur %s (coupable=%s, durée=%d min)",
            case_id, guild.id, case.guilty_suspect_id, minutes,
        )
        return case

    async def check_evidence_reveals(self, guild: discord.Guild, case: Case) -> list[Evidence]:
        """Renvoie les preuves dont l'échéance de révélation programmée vient d'être
        atteinte (et bascule leur visibilité en base) — pour publier un bulletin en jeu."""
        store = self.storage_if_exists(guild)
        if store is None:
            return []
        return await store.pop_due_evidence_reveals(case.case_pk)

    # ------------------------------------------------------------------
    # Résolution
    # ------------------------------------------------------------------

    async def resolve_case(self, guild: discord.Guild, case: Case) -> tuple[Case, list[PlayerResult]]:
        store = self.storage(guild)
        try:
            monologue = await self.actor.resoudre(case)
        except Exception as e:
            logger.error("Échec génération du monologue de résolution : %s", e)
            monologue = self._fallback_monologue(case)
        else:
            monologue = self._sanitize_monologue(monologue, case)

        await store.save_resolution(case.case_pk, monologue)

        accusations = await store.get_all_accusations(case.case_pk)
        results = scoring.compute_results(case, accusations)
        await store.save_results(results)

        winners = [
            str(r.player_id) for r in results if r.correct
        ]
        # MVP de la partie : détective(s) portant le badge BEST_DETECTIVE (bonne accusation
        # prise le plus tôt). Liste plutôt qu'un seul id : garde la porte ouverte à des
        # égalités gérées plus tard sans changer le format de stockage.
        mvp = [str(r.player_id) for r in results if "BEST_DETECTIVE" in r.badges]
        await store.add_hall_of_fame_entry(
            case_id=case.case_id,
            guild_id=guild.id,
            title=case.title,
            summary=case.summary,
            victim_name=case.victim_name,
            guilty_name=case.suspects[case.guilty_suspect_id].name,
            winners=winners,
            mvp=mvp,
        )

        case.resolved_at = datetime.now(timezone.utc)
        case.resolution_monologue = monologue
        case.status = "resolved"
        return case, results

    @staticmethod
    def _fallback_monologue(case: Case) -> str:
        guilty = case.suspects[case.guilty_suspect_id]
        text = (
            f"CASE CLOSED.\n\n{case.title}.\n\n"
            f"{case.victim_name} a été retrouvé(e) sans vie à {case.location}, {case.time_of_death}.\n"
            f"{case.investigation_moment}\n"
            f"Le coupable : {guilty.name}.\n"
            f"Mobile : {case.motive}.\n"
            f"Méthode : {case.method} ({case.weapon}).\n\n"
            f"{case.true_timeline_summary}\n\n"
            f"Mensonges révélés : {case.main_lies_summary}"
        )
        # Filet de sécurité : true_timeline_summary/main_lies_summary viennent tels quels
        # de LLM1 et pourraient contenir un identifiant de slot (p01..) résiduel.
        return strip_slot_ids(text, case)

    @staticmethod
    def _sanitize_monologue(monologue: str, case: Case) -> str:
        """Garde-fou léger : si le monologue ne mentionne même pas le coupable, on ne
        fait pas confiance à la version générée et on retombe sur le gabarit garanti fidèle."""
        guilty_name = case.suspects[case.guilty_suspect_id].name
        if guilty_name.split()[0].lower() not in monologue.lower():
            logger.warning("Monologue de résolution ne mentionne pas le coupable — fallback gabarit")
            return CaseEngine._fallback_monologue(case)
        return monologue

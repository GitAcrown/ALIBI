"""Cog Enquête — ALIBI, whodunit multijoueur généré par IA."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.fuzzy import find as fuzzy_find, finder as fuzzy_finder

from common.llm.client import LLMClient

from . import config, response_validator, views
from .engine import CaseAlreadyActive, CaseEngine, GenerationFailed
from .facts import FactEngine, normalize_text
from .models import Case, Suspect
from .portraits import load_portraits_data, setup_portrait_emojis
from .question_analyzer import find_duplicate
from .storage import fail_all_generating_cases_sync, iter_guild_ids_with_data
from .suspect_engine import build_context

logger = logging.getLogger("enquete.cog")


def _resolve_suspect(case: Case, value: str) -> Optional[Suspect]:
    value = (value or "").strip()
    if value in case.suspects:
        return case.suspects[value]
    return fuzzy_find(value, case.suspects.values(), key=lambda s: s.name)


class EnqueteCog(commands.Cog, name="Enquete"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key = bot.config.get("OPENAI_API_KEY")  # type: ignore[attr-defined]
        if not api_key:
            logger.critical(
                "OPENAI_API_KEY manquante dans .env — le cog Enquête ne pourra pas générer de dossier."
            )
        self.client = LLMClient(api_key or "")
        self.engine = CaseEngine(self.client)
        self.resolution_loop.start()

    async def cog_load(self) -> None:
        # Génération interrompue par un redémarrage → libère le verrou serveur.
        n = fail_all_generating_cases_sync()
        if n:
            logger.warning("Abandon de %d enquête(s) coincée(s) en génération au démarrage", n)
        # DynamicItem : une inscription suffit pour TOUTES les enquêtes (durée + restart).
        self.bot.add_dynamic_items(*views.DOSSIER_DYNAMIC_ITEMS)
        logger.info("DynamicItems dossier enregistrés (%d)", len(views.DOSSIER_DYNAMIC_ITEMS))

    async def cog_unload(self) -> None:
        self.resolution_loop.cancel()
        try:
            self.bot.remove_dynamic_items(*views.DOSSIER_DYNAMIC_ITEMS)
        except Exception:
            pass
        await self.client.close()

    def _member_name_getter(self, guild: discord.Guild):
        def _get(player_id: int) -> str:
            member = guild.get_member(player_id)
            return member.display_name if member else f"Joueur {player_id}"
        return _get

    # ------------------------------------------------------------------
    # Handlers partagés (slash + boutons / modals)
    # ------------------------------------------------------------------

    async def handle_interrogation(
        self,
        interaction: discord.Interaction,
        *,
        suspect_id: str,
        question: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        # Modale ouverte depuis le sélecteur (bouton "Interroger…") : Discord permet
        # d'éditer EN PLACE le message d'origine (deferred_message_update), au lieu
        # d'empiler un nouveau message ephémère à chaque question posée.
        is_modal_flow = interaction.type == discord.InteractionType.modal_submit

        async def _error(msg: str) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

        store = self.engine.storage_if_exists(guild)
        if store is None:
            return await _error("Aucun dossier ouvert ici.")
        case = await store.get_active_case()
        if case is None or case.status != "active":
            return await _error("Aucun dossier ouvert ici.")

        target = case.suspects.get(suspect_id) or _resolve_suspect(case, suspect_id)
        if target is None:
            return await _error("Ce nom ne figure pas au dossier.")

        player_id = interaction.user.id
        asked = await store.count_player_questions(case.case_pk, player_id)
        qmax = config.max_questions_for_case(case)
        if asked >= qmax:
            return await _error(
                f"Tes crédits d'interrogatoire sont épuisés ({qmax})."
            )

        if not interaction.response.is_done():
            try:
                if is_modal_flow:
                    await interaction.response.defer()
                else:
                    await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                is_modal_flow = False
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=True)

        history = await store.get_player_suspect_history(case.case_pk, player_id, target.id)
        duplicate = find_duplicate(question, history)

        if duplicate is not None:
            response_text = duplicate.response_text
            fact_ids = duplicate.fact_ids_used
            is_duplicate = True
        else:
            fact_engine = FactEngine(case)
            ctx = build_context(case, target, fact_engine, question, history)
            result = None
            for attempt in range(config.MAX_ACTOR_ATTEMPTS):
                try:
                    candidate = await self.engine.actor.interroger(ctx, question)
                except Exception as e:
                    logger.error("Échec appel LLMActor.interroger : %s", e)
                    continue
                issues = response_validator.validate(candidate, ctx, case)
                if not issues:
                    result = candidate
                    break
                logger.warning("Réponse de suspect rejetée (tentative %d) : %s", attempt + 1, issues)
            if result is None:
                result = response_validator.fallback_response(seed=player_id + asked)
            response_text = result["reponse"]
            fact_ids = result.get("fact_ids_utilises", [])
            is_duplicate = False

        await store.record_interrogation(
            case.case_pk, player_id, target.id, question, normalize_text(question),
            fact_ids, response_text, is_duplicate,
        )

        remaining = qmax - (asked + 1)
        portraits = load_portraits_data()
        view = views.InterrogationResultView(
            suspect_id=target.id,
            suspect_name=target.name,
            question=question,
            response=response_text,
            questions_left=remaining,
            is_duplicate=is_duplicate,
            case=case,
            portraits_meta=portraits,
            suspect_age=target.age,
            suspect_role=target.role,
        )
        files = view.portrait_files

        if is_modal_flow:
            try:
                await interaction.edit_original_response(
                    view=view, attachments=files or [],
                )
                return
            except discord.HTTPException:
                logger.warning("edit_original_response a échoué pour l'interrogatoire — nouveau message")
                # Le flux du File a pu être consommé : reconstruire la vue + fichier.
                view = views.InterrogationResultView(
                    suspect_id=target.id,
                    suspect_name=target.name,
                    question=question,
                    response=response_text,
                    questions_left=remaining,
                    is_duplicate=is_duplicate,
                    case=case,
                    portraits_meta=portraits,
                    suspect_age=target.age,
                    suspect_role=target.role,
                )
                files = view.portrait_files

        if interaction.response.is_done():
            await interaction.followup.send(view=view, files=files, ephemeral=True)
        else:
            await interaction.response.send_message(view=view, files=files, ephemeral=True)

    async def handle_accusation(
        self,
        interaction: discord.Interaction,
        *,
        suspect_id: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        # Depuis le sélecteur (bouton "Accuser…") : édite EN PLACE le même message
        # ephémère au lieu d'en envoyer un nouveau à chaque changement d'accusation.
        is_component_flow = (
            interaction.type == discord.InteractionType.component
            and not interaction.response.is_done()
        )

        async def _error(msg: str) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

        store = self.engine.storage_if_exists(guild)
        if store is None:
            return await _error("Aucun dossier ouvert ici.")
        case = await store.get_active_case()
        if case is None or case.status != "active":
            return await _error("Aucun dossier ouvert ici.")

        target = case.suspects.get(suspect_id) or _resolve_suspect(case, suspect_id)
        if target is None:
            return await _error("Ce nom ne figure pas au dossier.")

        await store.upsert_accusation(case.case_pk, interaction.user.id, target.id)
        portraits = load_portraits_data()
        emoji = (portraits.get(target.id) or {}).get("emoji") or ""
        view = views.AccusationResultView(
            target.name, emoji, suspect_age=target.age, suspect_role=target.role
        )

        if is_component_flow:
            try:
                await interaction.response.edit_message(view=view)
                return
            except discord.HTTPException:
                logger.warning("edit_message a échoué pour l'accusation — nouveau message")

        if interaction.response.is_done():
            await interaction.followup.send(view=view, ephemeral=True)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)

    # ------------------------------------------------------------------
    # Timer de résolution automatique
    # ------------------------------------------------------------------

    @tasks.loop(minutes=config.RESOLUTION_CHECK_INTERVAL_MINUTES)
    async def resolution_loop(self) -> None:
        # Uniquement les serveurs qui ont déjà une base — pas de création de .db ici.
        for guild_id in iter_guild_ids_with_data():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                store = self.engine.storage_if_exists(guild)
                if store is None:
                    continue

                active = await store.get_active_case()
                if active is not None and active.status == "active":
                    await self._announce_due_evidence(guild, active)

                case_pk = await store.active_case_past_deadline()
                if case_pk is None:
                    continue
                case = await store.get_case(case_pk)
                if case is None:
                    continue
                logger.info("Résolution automatique de l'enquête %s (serveur %s)", case.case_id, guild.id)
                await self._resolve_and_announce(guild, case)
            except Exception:
                logger.exception("Erreur dans la boucle de résolution auto pour le serveur %s", guild_id)

    async def _announce_due_evidence(self, guild: discord.Guild, case: Case) -> None:
        """Publie un bulletin public si une preuve programmée vient d'être révélée."""
        if not case.channel_id:
            return
        try:
            due = await self.engine.check_evidence_reveals(guild, case)
        except Exception:
            logger.exception("Échec de la vérification des révélations programmées (%s)", case.case_id)
            return
        if not due:
            return
        channel = guild.get_channel(case.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(case.channel_id)
            except Exception:
                return
        try:
            await channel.send(view=views.EvidenceBulletinView(case, due))
        except Exception:
            logger.exception("Échec de l'envoi du bulletin d'enquête (%s)", case.case_id)

    @resolution_loop.before_loop
    async def _before_resolution_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _publish_dossier_menu(
        self,
        channel: discord.abc.Messageable,
        case: Case,
        portraits_meta: dict,
    ) -> Optional[discord.Message]:
        """Publie le menu principal en NOUVEAU message (épinglé), distinct du suivi de génération."""
        view = views.DossierView(case, portraits_meta)
        try:
            message = await channel.send(view=view)
        except discord.HTTPException:
            logger.exception("Impossible d'envoyer le menu dossier pour %s", case.case_id)
            return None

        try:
            await message.pin(reason=f"ALIBI · dossier {case.case_id}")
        except discord.Forbidden:
            logger.warning(
                "Pas la permission d'épingler le menu dossier (salon %s) — "
                "il faut « Gérer les messages ».",
                getattr(channel, "id", "?"),
            )
        except discord.HTTPException as e:
            logger.warning("Épinglage du menu dossier échoué : %s", e)

        try:
            guild = message.guild
            if guild is not None:
                store = self.engine.storage(guild)
                await store.set_announce_message(case.case_pk, message.id)
                case.announce_message_id = message.id
        except Exception:
            logger.exception("Impossible d'enregistrer announce_message_id pour %s", case.case_id)
        return message

    async def _unpin_dossier_menu(self, guild: discord.Guild, case: Case) -> None:
        """Désépingle le menu principal de l'affaire (après /classer ou résolution auto)."""
        if not case.channel_id or not case.announce_message_id:
            return
        channel = guild.get_channel(case.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(case.channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            pinned = await channel.fetch_message(case.announce_message_id)
            if pinned.pinned:
                await pinned.unpin(reason=f"ALIBI · affaire {case.case_id} classée")
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning("Désépinglage du menu dossier échoué : %s", e)

    async def _resolve_and_announce(self, guild: discord.Guild, case: Case) -> None:
        resolved_case, results = await self.engine.resolve_case(guild, case)
        portraits = load_portraits_data()
        view = views.ResolutionView(
            resolved_case, results, self._member_name_getter(guild), portraits
        )
        channel = guild.get_channel(case.channel_id) if case.channel_id else None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(view=view)
            except discord.HTTPException:
                logger.warning("Impossible d'annoncer la résolution dans le salon %s", case.channel_id)
        await self._unpin_dossier_menu(guild, case)

    # ------------------------------------------------------------------
    # Autocomplete
    # ------------------------------------------------------------------

    async def suspect_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        store = self.engine.storage_if_exists(interaction.guild)
        if store is None:
            return []
        case = await store.get_active_case()
        if case is None or case.status != "active":
            return []
        if current:
            matches = fuzzy_finder(current, case.suspects.values(), key=lambda s: s.name)
        else:
            matches = list(case.suspects.values())
        return [app_commands.Choice(name=s.name, value=s.name) for s in matches[:25]]

    # ------------------------------------------------------------------
    # /enquete
    # ------------------------------------------------------------------

    @app_commands.command(name="enquete", description="Ouvre un nouveau dossier (admin).")
    @app_commands.describe(
        contexte="Contexte optionnel pour orienter l'enquête générée.",
        duree_minutes="Durée de l'enquête en minutes (tests / fast-forward). Défaut : 4h.",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enquete(
        self,
        interaction: discord.Interaction,
        contexte: Optional[str] = None,
        duree_minutes: Optional[app_commands.Range[int, 1, 1440]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        assert guild is not None

        progress = await interaction.followup.send(view=views.GeneratingView(), wait=True)
        portraits_meta = load_portraits_data()

        async def _on_progress(attempt: int, total: int, note: str) -> None:
            try:
                await progress.edit(view=views.GeneratingView(attempt=attempt, total=total, note=note))
            except discord.HTTPException:
                pass

        try:
            case = await self.engine.start_case(
                guild,
                interaction.channel_id,
                contexte or "",
                portraits_meta,
                duration_minutes=duree_minutes,
                progress_cb=_on_progress,
            )
        except CaseAlreadyActive:
            await progress.edit(
                view=views.ErrorView(
                    "Un dossier est déjà ouvert ici. Classe-le avec `/classer` avant d'en ouvrir un autre."
                )
            )
            return
        except GenerationFailed as e:
            detail = "\n".join(f"- {issue}" for issue in e.attempts_log[-8:]) or "(aucune note)"
            await progress.edit(
                view=views.ErrorView(
                    "Le bureau a rejeté le dossier après plusieurs relectures.\n"
                    f"**Anomalies relevées :**\n{detail}"
                )
            )
            return
        except Exception as e:
            logger.exception("Erreur inattendue lors de la génération de l'enquête")
            # Libère le verrou generating si le moteur n'a pas pu le faire.
            try:
                store = self.engine.storage_if_exists(guild)
                if store is not None:
                    await store.fail_generating_cases()
            except Exception:
                pass
            await progress.edit(
                view=views.ErrorView(
                    f"Incident au bureau pendant la constitution du dossier.\n`{type(e).__name__}: {e}`"
                )
            )
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await progress.edit(
                view=views.ErrorView("Ce local ne permet pas d'afficher le dossier.")
            )
            return

        await self._publish_dossier_menu(channel, case, portraits_meta)
        try:
            await progress.delete()
        except discord.HTTPException:
            # Si la suppression échoue, on laisse le suivi de génération tel quel
            # (le menu principal est déjà un message distinct).
            pass

    # ------------------------------------------------------------------
    # /interroger
    # ------------------------------------------------------------------

    @app_commands.command(
        name="interroger",
        description="Convoque un témoin (autant d'interrogatoires que de suspects).",
    )
    @app_commands.describe(suspect="Nom du suspect à interroger.", question="Ta question.")
    @app_commands.autocomplete(suspect=suspect_autocomplete)
    @app_commands.guild_only()
    async def interroger(self, interaction: discord.Interaction, suspect: str, question: str) -> None:
        guild = interaction.guild
        assert guild is not None
        store = self.engine.storage_if_exists(guild)
        if store is None:
            await interaction.response.send_message(
                view=views.ErrorView("Aucun dossier ouvert ici."), ephemeral=True
            )
            return
        case = await store.get_active_case()
        if case is None or case.status != "active":
            await interaction.response.send_message(
                view=views.ErrorView("Aucun dossier ouvert ici."), ephemeral=True
            )
            return
        target = _resolve_suspect(case, suspect)
        if target is None:
            await interaction.response.send_message(
                view=views.ErrorView(f"`{suspect}` ne figure pas au dossier."), ephemeral=True
            )
            return
        await self.handle_interrogation(interaction, suspect_id=target.id, question=question)

    # ------------------------------------------------------------------
    # /accuser
    # ------------------------------------------------------------------

    @app_commands.command(name="accuser", description="Pointe un coupable sous scellés (modifiable).")
    @app_commands.describe(suspect="Le suspect que tu accuses.")
    @app_commands.autocomplete(suspect=suspect_autocomplete)
    @app_commands.guild_only()
    async def accuser(self, interaction: discord.Interaction, suspect: str) -> None:
        guild = interaction.guild
        assert guild is not None
        store = self.engine.storage_if_exists(guild)
        if store is None:
            await interaction.response.send_message(
                view=views.ErrorView("Aucun dossier ouvert ici."), ephemeral=True
            )
            return
        case = await store.get_active_case()
        if case is None or case.status != "active":
            await interaction.response.send_message(
                view=views.ErrorView("Aucun dossier ouvert ici."), ephemeral=True
            )
            return
        target = _resolve_suspect(case, suspect)
        if target is None:
            await interaction.response.send_message(
                view=views.ErrorView(f"`{suspect}` ne figure pas au dossier."), ephemeral=True
            )
            return
        await self.handle_accusation(interaction, suspect_id=target.id)

    # ------------------------------------------------------------------
    # /scelles — équivalent slash du bouton « Scellés »
    # ------------------------------------------------------------------

    @app_commands.command(name="scelles", description="Consulte les scellés publics et les personnes d'intérêt.")
    @app_commands.guild_only()
    async def scelles(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        store = self.engine.storage_if_exists(guild)
        case = await store.get_active_case() if store else None
        if case is None:
            await interaction.response.send_message(
                view=views.ErrorView("Aucun dossier ouvert ici."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            view=views.EvidenceView(case, load_portraits_data()), ephemeral=True
        )

    # ------------------------------------------------------------------
    # /badge — équivalent slash du bouton « Mon badge »
    # ------------------------------------------------------------------

    @app_commands.command(name="badge", description="Consulte ton badge enquêteur.")
    @app_commands.guild_only()
    async def badge(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        store = self.engine.storage_if_exists(guild)
        case = await store.get_active_case() if store else None
        if case is None:
            await interaction.response.send_message(
                view=views.ErrorView("Aucun dossier ouvert ici."), ephemeral=True
            )
            return
        view = await views.build_status_view(self, case, interaction.user.id)
        await interaction.response.send_message(view=view, ephemeral=True)

    # ------------------------------------------------------------------
    # /classer — clôture l'affaire (ton "dossier classé" de l'archive)
    # ------------------------------------------------------------------

    @app_commands.command(name="classer", description="Classe l'affaire immédiatement (admin).")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def classer(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        store = self.engine.storage_if_exists(guild)
        case = await store.get_active_case() if store else None
        if case is None or case.status != "active":
            await interaction.response.send_message(
                view=views.ErrorView("Aucun dossier ouvert ici."), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        resolved_case, results = await self.engine.resolve_case(guild, case)
        await self._unpin_dossier_menu(guild, case)
        view = views.ResolutionView(
            resolved_case, results, self._member_name_getter(guild), load_portraits_data()
        )
        await interaction.followup.send(view=view)

    # ------------------------------------------------------------------
    # /palmares
    # ------------------------------------------------------------------

    @app_commands.command(name="palmares", description="Bilan de la dernière affaire classée.")
    @app_commands.guild_only()
    async def palmares(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        store = self.engine.storage_if_exists(guild)
        if store is None:
            await interaction.response.send_message(
                view=views.ErrorView("Les archives sont vides."), ephemeral=True
            )
            return
        case = await store.get_last_resolved_case(guild.id)
        if case is None:
            await interaction.response.send_message(
                view=views.ErrorView("Les archives sont vides."), ephemeral=True
            )
            return
        results = await store.get_results(case.case_pk)
        view = views.ResolutionView(
            case, results, self._member_name_getter(guild), load_portraits_data()
        )
        await interaction.response.send_message(view=view)

    # ------------------------------------------------------------------
    # /historique
    # ------------------------------------------------------------------

    @app_commands.command(name="historique", description="Parcourt les archives des affaires classées.")
    @app_commands.guild_only()
    async def historique(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        store = self.engine.storage_if_exists(guild)
        entries = await store.list_hall_of_fame(guild.id) if store else []
        await interaction.response.send_message(
            view=views.HistoriqueView(entries)
        )

    # ------------------------------------------------------------------
    # /portraits_setup
    # ------------------------------------------------------------------

    @app_commands.command(name="portraits_setup", description="Upload les 12 portraits comme emojis d'application (admin).")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def portraits_setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        successes, errors = await setup_portrait_emojis(self.bot)
        lines = []
        if successes:
            lines.append("**Réussites :**\n" + "\n".join(successes))
        if errors:
            lines.append("**Erreurs :**\n" + "\n".join(errors))
        await interaction.followup.send("\n\n".join(lines) or "Rien à faire.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EnqueteCog(bot))

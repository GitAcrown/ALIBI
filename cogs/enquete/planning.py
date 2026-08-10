"""Planning automatique — UI modo + helpers de créneaux quotidiens."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import discord

from common import emojis as E
from common.discord_ui import append_controls, make_container

from . import config
from .models import Schedule

if TYPE_CHECKING:
    from .enquete import EnqueteCog

logger = logging.getLogger("enquete.planning")

_TIME_RE = re.compile(r"^(\d{1,2})\s*[hH:]\s*(\d{2})$")


def schedule_tz() -> ZoneInfo:
    return ZoneInfo(config.SCHEDULE_TIMEZONE)


def local_now() -> datetime:
    return datetime.now(schedule_tz())


def parse_time(raw: str) -> Optional[tuple[int, int]]:
    """Parse '12:00', '12h00', '20h' → (hour, minute)."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    m = _TIME_RE.match(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
        return None
    # "20h" ou "20"
    m2 = re.match(r"^(\d{1,2})\s*h?$", text)
    if m2:
        h = int(m2.group(1))
        if 0 <= h <= 23:
            return h, 0
    return None


def parse_duration_hours(raw: str) -> Optional[int]:
    """Parse '4', '4h', '3h30' → minutes."""
    text = (raw or "").strip().lower().replace(" ", "")
    if not text:
        return None
    m = re.match(r"^(\d{1,2})h(\d{1,2})$", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 24 and mi < 60:
            total = h * 60 + mi
            return total if 1 <= total <= 1440 else None
    m2 = re.match(r"^(\d{1,2})h?$", text)
    if m2:
        h = int(m2.group(1))
        if 1 <= h <= 24:
            return h * 60
    if text.isdigit():
        h = int(text)
        if 1 <= h <= 24:
            return h * 60
    return None


def _heading(code: str, text: str, level: str = "##") -> str:
    prefix = f"{code} " if code else ""
    return f"{level} {prefix}{text}"


def _channel_label(guild: discord.Guild, channel_id: int) -> str:
    ch = guild.get_channel(channel_id)
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        return ch.mention
    return f"`#{channel_id}`"


def _schedule_line(guild: discord.Guild, s: Schedule) -> str:
    status = "ON" if s.enabled else "OFF"
    ctx = f" · _{s.context_prompt[:40]}…_" if len(s.context_prompt) > 40 else (
        f" · _{s.context_prompt}_" if s.context_prompt else ""
    )
    return (
        f"**{s.time_label}** · {s.duration_label} · {_channel_label(guild, s.channel_id)} "
        f"· `{status}`{ctx}"
    )


def is_due(schedule: Schedule, now: Optional[datetime] = None) -> bool:
    """Vrai si le créneau doit démarrer maintenant (même heure:minute locale, pas encore tiré aujourd'hui)."""
    now = now or local_now()
    if not schedule.enabled:
        return False
    today = now.date().isoformat()
    if schedule.last_fired_date == today:
        return False
    return now.hour == schedule.hour and now.minute == schedule.minute


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class PlanningView(discord.ui.LayoutView):
    """Panneau modo — créneaux quotidiens d'ouverture d'enquête."""

    def __init__(
        self,
        guild: discord.Guild,
        schedules: list[Schedule],
        *,
        default_channel_id: Optional[int] = None,
        note: str = "",
    ):
        super().__init__(timeout=300)
        self.guild = guild
        self.schedules = schedules
        self.default_channel_id = default_channel_id or (
            schedules[0].channel_id if schedules else None
        )

        clock = E.e(E.CLOCK)
        clock_prefix = f"{clock} " if clock else ""
        tz = config.SCHEDULE_TIMEZONE

        if schedules:
            body = "\n".join(_schedule_line(guild, s) for s in schedules)
        else:
            body = "-# Aucun créneau — ajoute le premier ci-dessous."

        children: list = [
            discord.ui.TextDisplay(_heading(E.CLOCK or E.CLASSIFIED, "PLANNING AUTOMATIQUE")),
            discord.ui.TextDisplay(
                f"-# Lancement quotidien · fuseau **{tz}** · réservé aux modos"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Créneaux"),
            discord.ui.TextDisplay(body),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"{clock_prefix}Salon cible des **nouveaux** créneaux"
                + (
                    f" · {_channel_label(guild, self.default_channel_id)}"
                    if self.default_channel_id
                    else ""
                )
                + " :"
            ),
            discord.ui.ActionRow(
                _PlanningChannelSelect(self.default_channel_id),
            ),
        ]

        buttons = [
            _AddScheduleButton(),
            _RefreshPlanningButton(),
        ]
        append_controls(
            children,
            note=note or (
                "Ex. tous les jours 12h → 4h, 20h → 3h. "
                "Si une affaire est déjà ouverte à l'heure dite, le créneau est sauté."
            ),
            button_row=discord.ui.ActionRow(*buttons),
        )

        if schedules:
            options = [
                discord.SelectOption(
                    label=f"{s.time_label} · {s.duration_label}",
                    value=str(s.id),
                    description=(
                        f"{'ON' if s.enabled else 'OFF'} · "
                        f"salon {s.channel_id}"
                    )[:100],
                )
                for s in schedules[:25]
            ]
            children += [
                discord.ui.Separator(),
                discord.ui.TextDisplay("### Gérer un créneau"),
                discord.ui.ActionRow(_ScheduleManageSelect(options)),
            ]

        self.add_item(make_container(*children))


class _PlanningChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, default_channel_id: Optional[int]):
        # Pas de default_values : selon les versions discord.py / le cache, un
        # Object nu peut faire échouer le send. On réaffiche le salon choisi via note.
        super().__init__(
            placeholder="Salon où lancer les enquêtes…",
            channel_types=[discord.ChannelType.text, discord.ChannelType.public_thread],
            min_values=1,
            max_values=1,
        )
        self._hint_channel_id = default_channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            return await interaction.response.defer()
        ch = self.values[0]
        view = await build_planning_view(
            interaction,
            default_channel_id=ch.id,
            note=f"Salon cible : {ch.mention}",
        )
        if view is None:
            return
        await interaction.response.edit_message(view=view)


class _AddScheduleButton(discord.ui.Button):
    def __init__(self):
        kwargs = {"style": discord.ButtonStyle.primary, "label": "Ajouter un créneau"}
        clock = E.e(E.CLOCK)
        if clock:
            kwargs["emoji"] = clock
        super().__init__(**kwargs)

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        channel_id = None
        if isinstance(parent, PlanningView):
            channel_id = parent.default_channel_id
        if channel_id is None and interaction.channel_id:
            channel_id = interaction.channel_id
        await interaction.response.send_modal(AddScheduleModal(channel_id))


class _RefreshPlanningButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Actualiser",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = await build_planning_view(interaction)
        if view is None:
            return
        await interaction.response.edit_message(view=view)


class _ScheduleManageSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Choisir un créneau à gérer…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        schedule_id = int(self.values[0])
        await interaction.response.edit_message(
            view=ScheduleDetailView(schedule_id),
        )


class ScheduleDetailView(discord.ui.LayoutView):
    """Actions sur un créneau précis (ON/OFF, supprimer, retour)."""

    def __init__(self, schedule_id: int, *, note: str = ""):
        super().__init__(timeout=180)
        self.schedule_id = schedule_id
        children = [
            discord.ui.TextDisplay(_heading(E.CLOCK or E.CLASSIFIED, "GÉRER LE CRÉNEAU")),
            discord.ui.TextDisplay(f"-# Créneau `#{schedule_id}`"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                note or "Active, coupe ou supprime ce créneau — retour au planning ensuite."
            ),
        ]
        append_controls(
            children,
            button_row=discord.ui.ActionRow(
                _ToggleScheduleButton(schedule_id, enable=True),
                _ToggleScheduleButton(schedule_id, enable=False),
                _DeleteScheduleButton(schedule_id),
                _BackToPlanningButton(),
            ),
        )
        self.add_item(make_container(*children))


class _ToggleScheduleButton(discord.ui.Button):
    def __init__(self, schedule_id: int, *, enable: bool):
        super().__init__(
            style=discord.ButtonStyle.success if enable else discord.ButtonStyle.secondary,
            label="Activer" if enable else "Désactiver",
        )
        self.schedule_id = schedule_id
        self.enable = enable

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            return await interaction.response.send_message("Bureau hors ligne.", ephemeral=True)
        store = cog.engine.storage(interaction.guild)
        s = await store.set_schedule_enabled(self.schedule_id, self.enable)
        if s is None:
            return await interaction.response.send_message("Créneau introuvable.", ephemeral=True)
        view = await build_planning_view(
            interaction,
            note=f"Créneau **{s.time_label}** {'activé' if self.enable else 'désactivé'}.",
        )
        if view is None:
            return
        await interaction.response.edit_message(view=view)


class _DeleteScheduleButton(discord.ui.Button):
    def __init__(self, schedule_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Supprimer",
        )
        self.schedule_id = schedule_id

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            return await interaction.response.send_message("Bureau hors ligne.", ephemeral=True)
        store = cog.engine.storage(interaction.guild)
        ok = await store.delete_schedule(self.schedule_id)
        view = await build_planning_view(
            interaction,
            note="Créneau supprimé." if ok else "Créneau déjà absent.",
        )
        if view is None:
            return
        await interaction.response.edit_message(view=view)


class _BackToPlanningButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.primary, label="Retour")

    async def callback(self, interaction: discord.Interaction) -> None:
        view = await build_planning_view(interaction)
        if view is None:
            return
        await interaction.response.edit_message(view=view)


class AddScheduleModal(discord.ui.Modal, title="Nouveau créneau"):
    def __init__(self, channel_id: Optional[int]):
        super().__init__()
        self.channel_id = channel_id
        self.heure = discord.ui.TextInput(
            label="Heure quotidienne",
            placeholder="12:00 ou 20h",
            max_length=8,
            required=True,
        )
        self.duree = discord.ui.TextInput(
            label="Durée de la partie",
            placeholder="4 ou 4h ou 3h30",
            max_length=8,
            required=True,
        )
        self.contexte = discord.ui.TextInput(
            label="Contexte (optionnel)",
            placeholder="Ex. polar d'entreprise, huis clos…",
            style=discord.TextStyle.paragraph,
            max_length=400,
            required=False,
        )
        self.add_item(self.heure)
        self.add_item(self.duree)
        self.add_item(self.contexte)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if cog is None or interaction.guild is None:
            return await interaction.response.send_message("Bureau hors ligne.", ephemeral=True)

        parsed_time = parse_time(self.heure.value)
        if parsed_time is None:
            return await interaction.response.send_message(
                "Heure invalide — utilise `12:00` ou `20h`.", ephemeral=True
            )
        duration = parse_duration_hours(self.duree.value)
        if duration is None:
            return await interaction.response.send_message(
                "Durée invalide — utilise `4`, `4h` ou `3h30` (max 24h).", ephemeral=True
            )

        channel_id = self.channel_id or interaction.channel_id
        if not channel_id:
            return await interaction.response.send_message(
                "Choisis d'abord un salon cible dans le planning.", ephemeral=True
            )

        hour, minute = parsed_time
        store = cog.engine.storage(interaction.guild)
        # Évite les doublons exacts même heure + même salon.
        existing = await store.list_schedules()
        if any(
            s.hour == hour and s.minute == minute and s.channel_id == channel_id
            for s in existing
        ):
            return await interaction.response.send_message(
                "Un créneau existe déjà à cette heure pour ce salon.", ephemeral=True
            )

        s = await store.add_schedule(
            channel_id=channel_id,
            hour=hour,
            minute=minute,
            duration_minutes=duration,
            context_prompt=(self.contexte.value or "").strip(),
        )
        view = await build_planning_view(
            interaction,
            note=f"Créneau **{s.time_label}** · {s.duration_label} ajouté.",
            default_channel_id=channel_id,
        )
        if view is None:
            return
        # Le modal peut venir d'un message planning → on tente l'edit, sinon nouveau message.
        if interaction.message is not None:
            await interaction.response.edit_message(view=view)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)


def _get_cog(interaction: discord.Interaction) -> Optional["EnqueteCog"]:
    return interaction.client.get_cog("Enquete")  # type: ignore[return-value]


async def build_planning_view(
    interaction: discord.Interaction,
    *,
    note: str = "",
    default_channel_id: Optional[int] = None,
) -> Optional[PlanningView]:
    cog = _get_cog(interaction)
    guild = interaction.guild
    if cog is None or guild is None:
        if not interaction.response.is_done():
            await interaction.response.send_message("Bureau hors ligne.", ephemeral=True)
        return None
    store = cog.engine.storage(guild)
    schedules = await store.list_schedules()
    channel_id = default_channel_id
    if channel_id is None and isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        channel_id = interaction.channel.id
    return PlanningView(
        guild, schedules, default_channel_id=channel_id, note=note,
    )

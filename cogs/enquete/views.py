"""UI Components v2 — LayoutViews dossier classifié / film noir.

Panneau public persistant (boutons) + panneaux éphémères (selects, modals).
Les slash commands restent disponibles ; les boutons offrent le chemin principal.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional

import discord

from common import emojis as E
from common.discord_ui import append_controls, make_container

from . import config
from .models import Case, Interrogation, PlayerResult, Suspect
from .portraits import load_portraits_data, portrait_thumbnail_media

if TYPE_CHECKING:
    from .enquete import EnqueteCog

logger = logging.getLogger("enquete.views")

BADGE_LABELS = {
    "BEST_DETECTIVE": "Meilleur limier",
    "FIRST_TO_CRACK_THE_CASE": "Premier à craquer l'affaire",
    "MOST_CONFIDENTLY_WRONG": "Confiance aveugle",
    "WORST_ACCUSATION": "Pire piste",
}

# Messages joueur — ton dossier / archives (pas de jargon technique).
MSG_NO_ACTIVE = "Aucun dossier ouvert ici."
MSG_NO_RESOLVED = "Les archives sont vides."
MSG_SUSPECT_MISSING = "Ce nom ne figure pas au dossier."

BADGE_EMOJI = {
    "BEST_DETECTIVE": E.e(E.BADGE_DETECTIVE),
    "FIRST_TO_CRACK_THE_CASE": E.e(E.BADGE_FIRST),
    "MOST_CONFIDENTLY_WRONG": E.e(E.BADGE_WRONG),
    "WORST_ACCUSATION": E.e(E.BADGE_WORST),
}


# ---------------------------------------------------------------------------
# Helpers affichage
# ---------------------------------------------------------------------------

def _heading(code: str, text: str, level: str = "##") -> str:
    """Titre avec emoji custom optionnel en préfixe (aucun fallback unicode)."""
    prefix = f"{code} " if code else ""
    return f"{level} {prefix}{text}"


def _time_left(case: Case) -> str:
    remaining = case.deadline_at - datetime.now(timezone.utc)
    secs = int(remaining.total_seconds())
    if secs <= 0:
        return "délai écoulé"
    hours, rem = divmod(secs, 3600)
    minutes = rem // 60
    if hours > 0:
        return f"{hours}h{minutes:02d}"
    return f"{minutes} min"


def _portrait(slot: str, portraits_meta: dict) -> str:
    """Emoji custom du portrait, ou chaîne vide si aucun n'est configuré."""
    return (portraits_meta.get(slot) or {}).get("emoji") or ""


def _suspect_tag(s) -> str:
    """Étiquette texte 'nom, âge, rôle' — les portraits sont symboliques (pas de ressemblance
    fiable), donc c'est le principal repère d'identification des suspects pour les joueurs."""
    role = getattr(s, "role", "") or ""
    if role:
        return f"{s.name} · {s.age} ans · {role}"
    return f"{s.name} · {s.age} ans"


def _roster_lines(case: Case, portraits_meta: dict) -> str:
    lines = []
    for sid in sorted(case.suspects):
        s = case.suspects[sid]
        portrait = _portrait(sid, portraits_meta)
        prefix = f"{portrait} " if portrait else ""
        lines.append(f"{prefix}**{_suspect_tag(s)}**")
    return "\n".join(lines)


def _evidence_lines(case: Case) -> str:
    public = case.public_evidence()
    if not public:
        return "-# Aucune preuve publique pour l'instant."
    return "\n".join(f"**{e.id}** · {e.description}" for e in public)


def _btn_emoji(code: str) -> Optional[str]:
    """Emoji custom pour un bouton, ou None (aucune icône) si pas configuré."""
    return code or None


def _get_cog(interaction: discord.Interaction) -> Optional["EnqueteCog"]:
    return interaction.client.get_cog("Enquete")  # type: ignore[return-value]


async def _require_active_case(interaction: discord.Interaction):
    cog = _get_cog(interaction)
    if cog is None or interaction.guild is None:
        await interaction.response.send_message("Bureau hors ligne.", ephemeral=True)
        return None, None
    store = cog.engine.storage_if_exists(interaction.guild)
    case = await store.get_active_case() if store else None
    if case is None or case.status != "active":
        if interaction.response.is_done():
            await interaction.followup.send(MSG_NO_ACTIVE, ephemeral=True)
        else:
            await interaction.response.send_message(MSG_NO_ACTIVE, ephemeral=True)
        return None, None
    return cog, case


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

def _modal_case_reminder(
    victim_name: str,
    time_of_death: str,
    location: str = "",
    *,
    max_len: int = 45,
) -> str:
    """Rappel compact pour un TextInput Discord (label ≤ 45, placeholder ≤ 100)."""
    victim = (victim_name or "").strip()
    time = (time_of_death or "").strip()
    loc = (location or "").strip()
    candidates = []
    if victim and time and loc:
        candidates.append(f"{victim} · ~{time} · {loc}")
    if victim and time:
        candidates.append(f"{victim} · ~{time}")
    if victim:
        candidates.append(victim)
    if time:
        candidates.append(f"~{time}")
    for text in candidates:
        if len(text) <= max_len:
            return text
    # Dernier recours : tronquer le candidat le plus informatif.
    base = candidates[0] if candidates else "Affaire en cours"
    if len(base) <= max_len:
        return base
    return base[: max_len - 1] + "…"


class QuestionModal(discord.ui.Modal, title="Salle d'interrogatoire"):
    def __init__(
        self,
        case_pk: int,
        suspect_id: str,
        suspect_name: str,
        *,
        victim_name: str = "",
        time_of_death: str = "",
        location: str = "",
    ):
        super().__init__()
        self.case_pk = case_pk
        self.suspect_id = suspect_id
        reminder = _modal_case_reminder(victim_name, time_of_death, location, max_len=100)
        self.question = discord.ui.TextInput(
            label=f"Question à {suspect_name}",
            placeholder=reminder or "Où étiez-vous à cette heure ? / Que savez-vous de la victime ?",
            style=discord.TextStyle.paragraph,
            max_length=400,
            required=True,
        )
        self.add_item(self.question)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if cog is None:
            return await interaction.response.send_message("Bureau hors ligne.", ephemeral=True)
        await cog.handle_interrogation(
            interaction,
            suspect_id=self.suspect_id,
            question=self.question.value.strip(),
        )


# ---------------------------------------------------------------------------
# Selects
# ---------------------------------------------------------------------------

def _suspect_options(case: Case, portraits_meta: dict) -> list[discord.SelectOption]:
    options = []
    for sid in sorted(case.suspects):
        s = case.suspects[sid]
        emoji_str = _portrait(sid, portraits_meta)
        kwargs = {
            "label": s.name[:100],
            "value": sid,
            # Rôle plutôt que genre : le portrait est symbolique, c'est ce repère
            # (âge + rôle) qui permet vraiment de reconnaître le suspect.
            "description": f"{s.age} ans · {s.role}"[:100] if s.role else f"{s.age} ans"[:100],
        }
        # PartialEmoji depuis <:name:id> si possible
        if emoji_str.startswith("<") and emoji_str.endswith(">"):
            try:
                kwargs["emoji"] = discord.PartialEmoji.from_str(emoji_str)
            except Exception:
                pass
        options.append(discord.SelectOption(**kwargs))
    return options[:25]


_PLACEHOLDERS = {
    "interrogate": "Convoquer un témoin…",
    "accuse": "Pointer un coupable…",
    "history": "Rouvrir un procès-verbal…",
}


class _SuspectSelect(discord.ui.Select):
    """Select générique — mode 'interrogate' ouvre un modal, 'accuse' enregistre,
    'history' revoit les échanges déjà eus avec un suspect (résultat privé)."""

    def __init__(self, case: Case, portraits_meta: dict, *, mode: str):
        self.mode = mode
        self.case_pk = case.case_pk
        super().__init__(
            placeholder=_PLACEHOLDERS.get(mode, "Choisir un suspect…"),
            min_values=1,
            max_values=1,
            options=_suspect_options(case, portraits_meta),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        suspect_id = self.values[0]
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        if case.case_pk != self.case_pk:
            return await interaction.response.send_message(
                "Ce panneau appartient à une affaire classée.", ephemeral=True
            )
        suspect = case.suspects.get(suspect_id)
        if suspect is None:
            return await interaction.response.send_message(MSG_SUSPECT_MISSING, ephemeral=True)

        if self.mode == "interrogate":
            store = cog.engine.storage_if_exists(interaction.guild)
            if store is None:
                return await interaction.response.send_message(
                    MSG_NO_ACTIVE, ephemeral=True
                )
            asked = await store.count_player_questions(
                case.case_pk, interaction.user.id
            )
            qmax = config.max_questions_for_case(case)
            if asked >= qmax:
                return await interaction.response.send_message(
                    f"Tes crédits d'interrogatoire sont épuisés ({qmax}).",
                    ephemeral=True,
                )
            await interaction.response.send_modal(
                QuestionModal(
                    case.case_pk,
                    suspect_id,
                    suspect.name,
                    victim_name=case.victim_name,
                    time_of_death=case.time_of_death,
                    location=case.location,
                )
            )
            return

        if self.mode == "history":
            await _send_history(interaction, cog, case, suspect_id)
            return

        # mode accuse
        await cog.handle_accusation(interaction, suspect_id=suspect_id)


async def _send_history(
    interaction: discord.Interaction, cog: "EnqueteCog", case: Case, suspect_id: str
) -> None:
    """Renvoie TOUJOURS un nouveau message éphémère (jamais un edit_message) : ce
    sélecteur peut vivre dans un message PUBLIC (`/scelles`) — l'historique d'un
    joueur ne doit jamais fuiter dans le message partagé."""
    suspect = case.suspects.get(suspect_id)
    if suspect is None:
        return await interaction.response.send_message(MSG_SUSPECT_MISSING, ephemeral=True)
    store = cog.engine.storage_if_exists(interaction.guild)
    history = (
        await store.get_player_suspect_history(case.case_pk, interaction.user.id, suspect_id)
        if store is not None
        else []
    )
    portraits = load_portraits_data()
    view = HistoryView(suspect, history, portraits_meta=portraits)
    await interaction.response.send_message(
        view=view, files=view.portrait_files, ephemeral=True
    )


# ---------------------------------------------------------------------------
# Boutons du dossier public — DynamicItem (persistance auto après restart)
# ---------------------------------------------------------------------------
# Discord.py route tout custom_id qui matche le `template` regex, sans avoir
# à rappeler add_view(case_pk) au démarrage. Une seule inscription des classes
# via bot.add_dynamic_items(...) suffit pour toutes les enquêtes.

class StatusButton(discord.ui.DynamicItem[discord.ui.Button], template=r"alibi:status:(?P<case_pk>[0-9]+)"):
    def __init__(self, case_pk: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Mon badge",
                emoji=_btn_emoji(E.STATUS),
                custom_id=f"alibi:status:{case_pk}",
            )
        )
        self.case_pk = case_pk

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ):
        return cls(int(match["case_pk"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        view = await build_status_view(cog, case, interaction.user.id)
        await interaction.response.send_message(view=view, ephemeral=True)


class InterrogateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"alibi:interro:(?P<case_pk>[0-9]+)"):
    def __init__(self, case_pk: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Interroger…",
                emoji=_btn_emoji(E.INTERROGATE),
                custom_id=f"alibi:interro:{case_pk}",
            )
        )
        self.case_pk = case_pk

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ):
        return cls(int(match["case_pk"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        store = cog.engine.storage_if_exists(interaction.guild)
        if store is None:
            return await interaction.response.send_message(
                MSG_NO_ACTIVE, ephemeral=True
            )
        asked = await store.count_player_questions(case.case_pk, interaction.user.id)
        qmax = config.max_questions_for_case(case)
        if asked >= qmax:
            return await interaction.response.send_message(
                f"Tes crédits d'interrogatoire sont épuisés ({qmax}).",
                ephemeral=True,
            )
        portraits = load_portraits_data()
        view = SuspectPickerView(
            case, portraits, mode="interrogate",
            questions_left=qmax - asked,
        )
        await interaction.response.send_message(view=view, ephemeral=True)


class AccuseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"alibi:accuse:(?P<case_pk>[0-9]+)"):
    def __init__(self, case_pk: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="Accuser…",
                emoji=_btn_emoji(E.ACCUSE),
                custom_id=f"alibi:accuse:{case_pk}",
            )
        )
        self.case_pk = case_pk

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ):
        return cls(int(match["case_pk"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        portraits = load_portraits_data()
        view = SuspectPickerView(case, portraits, mode="accuse")
        await interaction.response.send_message(view=view, ephemeral=True)


class EvidenceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"alibi:evidence:(?P<case_pk>[0-9]+)"):
    def __init__(self, case_pk: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Scellés",
                emoji=_btn_emoji(E.EVIDENCE),
                custom_id=f"alibi:evidence:{case_pk}",
            )
        )
        self.case_pk = case_pk

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ):
        return cls(int(match["case_pk"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        portraits = load_portraits_data()
        view = EvidenceView(case, portraits)
        await interaction.response.send_message(view=view, ephemeral=True)


class RefreshButton(discord.ui.DynamicItem[discord.ui.Button], template=r"alibi:refresh:(?P<case_pk>[0-9]+)"):
    def __init__(self, case_pk: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Actualiser",
                emoji=_btn_emoji(E.REFRESH),
                custom_id=f"alibi:refresh:{case_pk}",
            )
        )
        self.case_pk = case_pk

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ):
        return cls(int(match["case_pk"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        portraits = load_portraits_data()
        view = DossierView(case, portraits)
        try:
            await interaction.response.edit_message(view=view)
        except discord.HTTPException:
            await interaction.response.send_message(view=view, ephemeral=True)


class HistorySelect(discord.ui.DynamicItem[discord.ui.Select], template=r"alibi:history:(?P<case_pk>[0-9]+)"):
    """Select persistant du panneau Scellés — fonctionne même dans le message PUBLIC
    de `/scelles` qui reste affiché toute la partie (pas de timeout, survit au restart).
    Sa réponse est TOUJOURS un nouveau message éphémère, jamais un edit du message parent."""

    def __init__(self, case_pk: int, options: list[discord.SelectOption]):
        super().__init__(
            discord.ui.Select(
                placeholder=_PLACEHOLDERS["history"],
                min_values=1,
                max_values=1,
                options=options or [discord.SelectOption(label="(aucun suspect)", value="_none")],
                custom_id=f"alibi:history:{case_pk}",
            )
        )
        self.case_pk = case_pk

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
        /,
    ):
        # Les options du select brut reconstruit par discord.py depuis le message
        # suffisent : pas besoin de retoucher la base pour reconstruire ce menu.
        return cls(int(match["case_pk"]), list(item.options))

    async def callback(self, interaction: discord.Interaction) -> None:
        # DynamicItem ne proxy PAS .values vers l'item interne (seuls custom_id/row/type/
        # width/callback le sont) — il faut lire les valeurs sélectionnées sur self.item.
        suspect_id = self.item.values[0]
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        if case.case_pk != self.case_pk:
            return await interaction.response.send_message(
                "Ce panneau appartient à une ancienne enquête.", ephemeral=True
            )
        await _send_history(interaction, cog, case, suspect_id)


# Classes à enregistrer une seule fois via bot.add_dynamic_items(...)
DOSSIER_DYNAMIC_ITEMS = (
    StatusButton,
    InterrogateButton,
    AccuseButton,
    EvidenceButton,
    RefreshButton,
    HistorySelect,
)


# ---------------------------------------------------------------------------
# LayoutViews
# ---------------------------------------------------------------------------

class DossierView(discord.ui.LayoutView):
    """Panneau public principal — annoncé au lancement de l'enquête.

    timeout=None + DynamicItem : boutons actifs pendant toute la partie,
    y compris 3h plus tard et après redémarrage du bot (sans re-add_view).
    """

    def __init__(
        self,
        case: Case,
        portraits_meta: dict,
        *,
        ping_role: Optional[discord.Role] = None,
    ):
        super().__init__(timeout=None)
        file_e = E.e(E.FILE)
        file_prefix = f"{file_e} " if file_e else ""
        clock = E.e(E.CLOCK)
        clock_prefix = f"{clock} " if clock else ""
        pk = case.case_pk

        # Les LayoutView (IS_COMPONENTS_V2) interdisent le champ message `content` :
        # la mention du rôle notif doit vivre dans un TextDisplay (et ping via
        # allowed_mentions à l'envoi).
        children: list = []
        if ping_role is not None:
            children.append(
                discord.ui.TextDisplay(
                    f"{ping_role.mention} — Une nouvelle enquête démarre."
                )
            )
            children.append(discord.ui.Separator())

        children += [
            discord.ui.TextDisplay(_heading(E.CLASSIFIED, "DOSSIER CLASSIFIÉ")),
            discord.ui.TextDisplay(f"# {case.title}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"```\n"
                f"VICTIME  · {case.victim_name}\n"
                f"LIEU     · {case.location}\n"
                f"HEURE    · ~{case.time_of_death}\n"
                f"```"
            ),
            discord.ui.TextDisplay(f"**{case.victim_name}** · {case.victim_description}"),
            discord.ui.TextDisplay(case.crime_description),
            discord.ui.TextDisplay(f"-# **Aujourd'hui** · {case.investigation_moment}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(_heading(E.EVIDENCE, "Scellés publics", "###")),
            discord.ui.TextDisplay(_evidence_lines(case)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(_heading(E.SUSPECT, "Personnes d'intérêt", "###")),
            discord.ui.TextDisplay(_roster_lines(case, portraits_meta)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"-# {file_prefix}Dossier `{case.case_id}`  ·  {clock_prefix}{_time_left(case)} avant classement\n"
                f"-# **{config.max_questions_for_case(case)}** interrogatoires / enquêteur. "
                f"Croise les témoignages. Accuse en silence. Ne laisse rien filtrer."
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(
                StatusButton(pk),
                InterrogateButton(pk),
                AccuseButton(pk),
                EvidenceButton(pk),
                RefreshButton(pk),
            ),
        ]
        self.add_item(make_container(*children))


class EvidenceBulletinView(discord.ui.LayoutView):
    """Message public ponctuel : une preuve jusque-là privée vient d'être versée au dossier.

    Casse le silence d'une partie de plusieurs heures sans intervention du LLM — la preuve
    existait déjà depuis la génération, seule sa visibilité change (la vérité ne bouge pas)."""

    def __init__(self, case: Case, new_evidence: list):
        super().__init__(timeout=None)
        lines = "\n".join(f"**{e.id}** · {e.description}" for e in new_evidence)
        children = [
            discord.ui.TextDisplay(_heading(E.EVIDENCE, "BULLETIN D'ENQUÊTE", "###")),
            discord.ui.TextDisplay(
                f"-# Nouvel élément versé au dossier `{case.case_id}` — l'enquête continue."
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(lines),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "-# Consulte **Scellés** ou clique **Actualiser** sur le dossier pour le voir apparaître."
            ),
        ]
        self.add_item(make_container(*children))


class SuspectPickerView(discord.ui.LayoutView):
    """Panneau éphémère : choisir un suspect (interroger / accuser)."""

    def __init__(
        self,
        case: Case,
        portraits_meta: dict,
        *,
        mode: str,
        questions_left: Optional[int] = None,
    ):
        super().__init__(timeout=180)
        if mode == "interrogate":
            title = _heading(E.INTERROGATE, "Salle d'interrogatoire")
            hint = (
                f"-# **{questions_left}** question(s) encore au dossier. "
                "La réponse reste **hors procès-verbal public** — à toi de la divulguer."
            )
        else:
            title = _heading(E.ACCUSE, "Accusation secrète")
            hint = "-# Tampon privé. Tu peux le déplacer jusqu'au classement de l'affaire."

        children = [
            discord.ui.TextDisplay(title),
            discord.ui.Separator(),
            discord.ui.TextDisplay(hint),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SuspectSelect(case, portraits_meta, mode=mode)),
        ]
        self.add_item(make_container(*children))


class EvidenceView(discord.ui.LayoutView):
    """timeout=None + DynamicItem (HistorySelect) : reste utilisable toute la partie,
    y compris depuis le message PUBLIC posté par `/scelles`."""

    def __init__(self, case: Case, portraits_meta: dict):
        super().__init__(timeout=None)
        children = [
            discord.ui.TextDisplay(_heading(E.EVIDENCE, "Pièces à conviction")),
            discord.ui.TextDisplay(f"**{case.title}**"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Scellés publics"),
            discord.ui.TextDisplay(_evidence_lines(case)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Personnes d'intérêt"),
            discord.ui.TextDisplay(_roster_lines(case, portraits_meta)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"-# Dossier `{case.case_id}`  ·  {_time_left(case)} avant classement\n"
                f"-# Rouvre un procès-verbal ci-dessous — lecture **privée**, réservée à ton badge."
            ),
            discord.ui.ActionRow(HistorySelect(case.case_pk, _suspect_options(case, portraits_meta))),
        ]
        self.add_item(make_container(*children))


class StatusView(discord.ui.LayoutView):
    def __init__(
        self,
        case: Case,
        *,
        questions_left: int,
        questions_used: int,
        accused_name: Optional[str],
    ):
        super().__init__(timeout=120)
        clock = E.e(E.CLOCK)
        clock_prefix = f"{clock} " if clock else ""
        accuse_line = accused_name or "*(aucune accusation pour l'instant)*"
        bar_used = "█" * questions_used + "░" * questions_left
        children = [
            discord.ui.TextDisplay(_heading(E.STATUS, "Badge enquêteur")),
            discord.ui.TextDisplay(f"**{case.title}**"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"{clock_prefix}**Délai** · {_time_left(case)}\n"
                f"**Interrogatoires** · `{bar_used}` {questions_left} restant(s)\n"
                f"**Accusation** · {accuse_line}"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"-# Dossier `{case.case_id}` — tes notes d'interrogatoire restent sous scellés."
            ),
        ]
        self.add_item(make_container(*children))


class InterrogationResultView(discord.ui.LayoutView):
    """Procès-verbal d'interrogatoire (éphémère).

    `status` :
    - ``pending`` — le suspect réfléchit (avant les premiers tokens) ;
    - ``streaming`` — la déposition s'écrit progressivement ;
    - ``done`` — réponse finale + bouton « Reconvoquer… ».
    """

    def __init__(
        self,
        *,
        suspect_id: str,
        suspect_name: str,
        question: str,
        response: str,
        questions_left: int,
        is_duplicate: bool = False,
        case: Optional[Case] = None,
        portraits_meta: Optional[dict] = None,
        suspect_age: Optional[int] = None,
        suspect_role: str = "",
        status: str = "done",
    ):
        # Pendant pending/streaming on garde un timeout long : un raisonnement medium
        # + éventuel retry peut dépasser 2 minutes avant la vue finale.
        super().__init__(timeout=None if status != "done" else 300)
        self.status = status
        note = ""
        if is_duplicate:
            note = "-# Même piste déjà suivie — l'info ne change pas (crédit d'interrogatoire consommé)."
        elif status == "pending":
            note = "-# Le suspect réfléchit…"
        elif status == "streaming":
            note = "-# Le suspect répond…"
        tag_bits = []
        if suspect_age is not None:
            tag_bits.append(f"{suspect_age} ans")
        if suspect_role:
            tag_bits.append(suspect_role)
        subtitle = " · ".join(tag_bits)

        # Portrait seulement sur la vue finale : les edits streaming éviteraient sinon
        # de re-joindre le fichier à chaque tick (et risqueraient de perdre l'attachment).
        attach, media = (None, None)
        if status == "done":
            attach, media = portrait_thumbnail_media(suspect_id, portraits_meta)
        self._portrait_file = attach

        header_lines = [
            _heading(E.INTERROGATE, f"Procès-verbal — {suspect_name}"),
        ]
        if subtitle:
            header_lines.append(f"-# {subtitle}")

        if media is not None:
            try:
                header = discord.ui.Section(
                    *[discord.ui.TextDisplay(line) for line in header_lines],
                    accessory=discord.ui.Thumbnail(media, description=suspect_name),
                )
            except Exception:
                logger.exception("Thumbnail portrait impossible pour %s", suspect_id)
                header = None
        else:
            header = None

        if status == "pending":
            deposition = "_…_"
        elif status == "streaming":
            deposition = f"*{response}*" if response else "_…_"
            if response and not response.endswith("…"):
                deposition = f"*{response}…*"
        else:
            deposition = f"*{response}*"

        children: list = []
        if header is not None:
            children.append(header)
        else:
            for line in header_lines:
                children.append(discord.ui.TextDisplay(line))
        children.append(discord.ui.Separator())
        children += [
            discord.ui.TextDisplay(f"**Question**\n{question}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"**Déposition**\n{deposition}"),
        ]
        append_controls(
            children,
            note=note or (
                f"-# Crédits restants : **{questions_left}** · sous scellés (lecture privée)"
            ),
            button_row=(
                discord.ui.ActionRow(
                    _InterrogateAgainButton(case.case_pk, questions_left),
                )
                if status == "done" and case is not None and questions_left > 0
                else None
            ),
        )
        self.add_item(make_container(*children))

    @property
    def portrait_files(self) -> list[discord.File]:
        """Fichiers à joindre au send/edit pour que le Thumbnail `attachment://` résolve."""
        return [self._portrait_file] if self._portrait_file is not None else []


class _InterrogateAgainButton(discord.ui.Button):
    def __init__(self, case_pk: int, questions_left: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Reconvoquer…",
            emoji=_btn_emoji(E.INTERROGATE),
        )
        self.case_pk = case_pk
        self.questions_left = questions_left

    async def callback(self, interaction: discord.Interaction) -> None:
        cog, case = await _require_active_case(interaction)
        if cog is None or case is None:
            return
        portraits = load_portraits_data()
        view = SuspectPickerView(
            case, portraits, mode="interrogate", questions_left=self.questions_left
        )
        # Édite le même message ephémère au lieu d'en empiler un nouveau.
        await interaction.response.edit_message(view=view)


class HistoryView(discord.ui.LayoutView):
    """Transcript privé de TOUTES les questions déjà posées par CE joueur à un suspect.

    Réponse aux inquiétudes : une fois le message d'interrogatoire fermé/expiré, ce panneau
    (accessible via le sélecteur « historique » du panneau Preuves) permet de le revoir."""

    def __init__(
        self,
        suspect: Suspect,
        history: list[Interrogation],
        *,
        portraits_meta: Optional[dict] = None,
    ):
        super().__init__(timeout=180)
        tag = _suspect_tag(suspect)
        if not history:
            body = "-# Aucun procès-verbal à ton nom pour ce témoin."
        else:
            body = "\n\n".join(
                f"**Q ·** {h.question_raw}\n**R ·** *{h.response_text}*" for h in history
            )

        attach, media = portrait_thumbnail_media(suspect.id, portraits_meta)
        self._portrait_file = attach
        header_lines = [
            _heading(E.HISTORY, f"Archives — {suspect.name}"),
            f"-# {tag}",
        ]
        if media is not None:
            try:
                header = discord.ui.Section(
                    *[discord.ui.TextDisplay(line) for line in header_lines],
                    accessory=discord.ui.Thumbnail(media, description=suspect.name),
                )
            except Exception:
                logger.exception("Thumbnail portrait impossible pour %s", suspect.id)
                header = None
        else:
            header = None

        children: list = []
        if header is not None:
            children.append(header)
        else:
            for line in header_lines:
                children.append(discord.ui.TextDisplay(line))
        children += [
            discord.ui.Separator(),
            discord.ui.TextDisplay(body),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"-# **{len(history)}** dépôt(s) · réservé à ton badge"
            ),
        ]
        self.add_item(make_container(*children))

    @property
    def portrait_files(self) -> list[discord.File]:
        return [self._portrait_file] if self._portrait_file is not None else []


class AccusationResultView(discord.ui.LayoutView):
    def __init__(
        self,
        suspect_name: str,
        suspect_emoji: str = "",
        *,
        suspect_age: Optional[int] = None,
        suspect_role: str = "",
    ):
        super().__init__(timeout=120)
        prefix = f"{suspect_emoji} " if suspect_emoji else ""
        tag_bits = []
        if suspect_age is not None:
            tag_bits.append(f"{suspect_age} ans")
        if suspect_role:
            tag_bits.append(suspect_role)
        tag_suffix = f" ({', '.join(tag_bits)})" if tag_bits else ""
        children = [
            discord.ui.TextDisplay(_heading(E.ACCUSE, "Accusation sous scellés")),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"Tu pointes **{prefix}{suspect_name}**{tag_suffix}.\n"
                f"-# **Confidentiel.** Modifiable jusqu'au classement — bouton **Accuser…** ou `/accuser`."
            ),
        ]
        self.add_item(make_container(*children))


class ResolutionView(discord.ui.LayoutView):
    def __init__(
        self,
        case: Case,
        results: list[PlayerResult],
        get_member_name: Callable[[int], str],
        portraits_meta: Optional[dict] = None,
    ):
        super().__init__(timeout=None)
        portraits_meta = portraits_meta or {}
        guilty = case.suspects[case.guilty_suspect_id]
        portrait = _portrait(case.guilty_suspect_id, portraits_meta)
        portrait_prefix = f"{portrait} " if portrait else ""

        children = [
            discord.ui.TextDisplay(_heading(E.CASE_CLOSED, "AFFAIRE CLASSÉE")),
            discord.ui.TextDisplay(f"# {case.title}"),
            discord.ui.Separator(),
        ]
        if case.resolution_monologue:
            children += [
                discord.ui.TextDisplay(case.resolution_monologue),
                discord.ui.Separator(),
            ]
        children += [
            discord.ui.TextDisplay(
                f"**COUPABLE** · {portrait_prefix}{_suspect_tag(guilty)}\n"
                f"**MOBILE** · {case.motive}\n"
                f"**MÉTHODE** · {case.method} ({case.weapon})"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"```\n"
                f"LIEU     · {case.location}\n"
                f"HEURE    · {case.time_of_death}\n"
                f"VICTIME  · {case.victim_name}\n"
                f"```"
            ),
            discord.ui.TextDisplay(f"-# {case.investigation_moment}"),
        ]

        correct = [r for r in results if r.correct]
        wrong = [r for r in results if not r.correct]

        def _fmt(r: PlayerResult) -> str:
            badges = []
            for b in r.badges:
                label = BADGE_LABELS.get(b, b)
                emoji = BADGE_EMOJI.get(b, "")
                badges.append(f"{emoji} {label}".strip())
            badge_txt = f" — {', '.join(badges)}" if badges else ""
            return f"• {get_member_name(r.player_id)}{badge_txt}"

        if correct:
            children += [
                discord.ui.Separator(),
                discord.ui.TextDisplay("### Limiers"),
                discord.ui.TextDisplay("\n".join(_fmt(r) for r in correct)),
            ]
        if wrong:
            children += [
                discord.ui.Separator(),
                discord.ui.TextDisplay("### Fausses pistes"),
                discord.ui.TextDisplay("\n".join(_fmt(r) for r in wrong)),
            ]
        if not correct and not wrong:
            children += [
                discord.ui.Separator(),
                discord.ui.TextDisplay("-# Personne n'a osé pointer du doigt."),
            ]

        children += [
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"-# Dossier `{case.case_id}` · versé aux archives"),
        ]
        self.add_item(make_container(*children))


class HistoriqueView(discord.ui.LayoutView):
    def __init__(self, entries: list[dict]):
        super().__init__(timeout=180)
        children = [
            discord.ui.TextDisplay(_heading(E.HOF, "Archives")),
            discord.ui.Separator(),
        ]
        if not entries:
            children.append(discord.ui.TextDisplay("-# Les rayonnages sont vides."))
        else:
            for e in entries:
                date = str(e.get("resolved_at", ""))[:10]
                try:
                    mvp_ids = json.loads(e.get("mvp_json") or "[]")
                except (TypeError, ValueError):
                    mvp_ids = []
                mvp_line = ""
                if mvp_ids:
                    mentions = "  ".join(f"<@{pid}>" for pid in mvp_ids)
                    label = "MVP" if len(mvp_ids) == 1 else "MVPs"
                    mvp_line = f"  ·  **{label}** : {mentions}"
                children += [
                    discord.ui.TextDisplay(
                        f"**`{e['case_id']}`** · {e['title']}\n"
                        f"{e['summary']}\n"
                        f"-# Victime : {e['victim_name']}  ·  Coupable : {e['guilty_name']}  ·  {date}{mvp_line}"
                    ),
                    discord.ui.Separator(),
                ]
            # retirer le dernier separator orphelin
            if children and isinstance(children[-1], discord.ui.Separator):
                children.pop()
        self.add_item(make_container(*children))


class ErrorView(discord.ui.LayoutView):
    def __init__(self, message: str):
        super().__init__(timeout=60)
        self.add_item(make_container(
            discord.ui.TextDisplay(_heading(E.CLASSIFIED, "TRANSMISSION REFUSÉE")),
            discord.ui.Separator(),
            discord.ui.TextDisplay(message),
            discord.ui.Separator(),
            discord.ui.TextDisplay("-# Tampon bureau · ne pas diffuser"),
        ))


class GeneratingView(discord.ui.LayoutView):
    """Affiché pendant la constitution du dossier (followup temporaire).

    timeout=None : la rédaction peut dépasser 2 minutes ; un timeout court
    désactiverait les composants du message avant le passage au dossier public.
    """

    def __init__(
        self,
        attempt: int = 1,
        total: int = 1,
        note: str = "Rédaction du rapport initial…",
    ):
        super().__init__(timeout=None)
        if total > 1:
            step = f"-# Brouillon **{attempt}/{total}** · {note}"
        else:
            step = f"-# {note}"
        self.add_item(make_container(
            discord.ui.TextDisplay(_heading(E.CLASSIFIED, "CONSTITUTION DU DOSSIER")),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"{step}\n"
                f"-# Machine à écrire · scellés · contre-expertise — ne pas diffuser."
            ),
        ))


# ---------------------------------------------------------------------------
# Builders async (besoin du store)
# ---------------------------------------------------------------------------

async def build_status_view(cog: "EnqueteCog", case: Case, player_id: int) -> StatusView:
    guild = cog.bot.get_guild(case.guild_id)
    if guild is None:
        return StatusView(case, questions_left=0, questions_used=0, accused_name=None)
    store = cog.engine.storage_if_exists(guild)
    if store is None:
        return StatusView(case, questions_left=0, questions_used=0, accused_name=None)
    asked = await store.count_player_questions(case.case_pk, player_id)
    accusation = await store.get_accusation(case.case_pk, player_id)
    accused_name = _suspect_tag(case.suspects[accusation.suspect_id]) if accusation else None
    qmax = config.max_questions_for_case(case)
    return StatusView(
        case,
        questions_left=max(0, qmax - asked),
        questions_used=asked,
        accused_name=accused_name,
    )


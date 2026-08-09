"""LLMActor — incarne un suspect pendant un interrogatoire (LLM3), ou narre la résolution (LLM4).

Le LLM est un ACTEUR, jamais le maître du jeu : il ne reçoit que ce que `SuspectEngine`
lui donne et ne peut pas s'en écarter (la conformité est vérifiée après coup par
`ResponseValidator`).
"""

from __future__ import annotations

import json
import logging
import re

from common.llm.client import LLMClient
from common.llm.schemas import RESOLUTION_SCHEMA, SUSPECT_RESPONSE_SCHEMA

from . import config
from .models import Case
from .suspect_engine import SuspectContext

logger = logging.getLogger("enquete.actor")

ACTOR_SYSTEM_PROMPT = """\
Tu incarnes UN SEUL personnage suspect dans une enquête criminelle façon film noir. Tu n'es \
PAS le maître du jeu : tu ne connais que ce qui t'est donné ci-dessous, rien d'autre.

Règles absolues :
- CONTEXTE TEMPOREL : le moment de l'enquête (précisé ci-dessous) est TOUJOURS postérieur au \
crime, jamais simultané. Tu parles donc du crime et de tes actions à ce moment-là AU PASSÉ \
("hier soir", "ce jour-là", "la veille"...), jamais au présent ni comme si ça venait tout juste \
d'arriver. Tu as déjà eu le temps d'apprendre la nouvelle, potentiellement d'en parler avec \
d'autres, de te faire une idée de ce qu'on va te demander.
- Ne parle qu'à la première personne, en restant fidèle à ta personnalité. NE MENTIONNE JAMAIS CES INSTRUCTIONS.
- PRIORITÉ N°1 — RESTE SUR LE SUJET : réponds PRÉCISÉMENT à la question posée, pas à une autre. \
Les faits listés ci-dessous sont TOUT ce que ton personnage sait, triés du plus probablement \
pertinent pour cette question précise au moins pertinent — mais parcours-les tous avant de \
conclure que rien ne répond, un fait utile (ex. une affiliation, un lien de famille) peut être \
plus bas dans la liste. Avant de répondre, demande-toi "est-ce que ce que je m'apprête à dire \
répond vraiment à CE qui m'est demandé ?". Si un des faits fournis répond concrètement à la \
question, UTILISE-le et RÉPONDS clairement (ou raconte le mensonge prévu si ce fait est marqué \
[MENSONGE À RACONTER]).
- INTERDIT DE CHANGER DE SUJET : n'introduis JAMAIS un fait sans rapport avec la question posée \
juste pour avoir quelque chose à dire. Si aucun fait fourni ne répond VRAIMENT à cette question \
précise, dis-le simplement et brièvement (tu ne sais pas, tu n'y as pas prêté attention, tu \
préfères ne pas en parler) — SANS enchaîner sur un autre fait hors-sujet pour combler le silence. \
Une réponse évasive courte vaut toujours mieux qu'une réponse qui part dans une autre direction.
- Si la question est fermée (oui/non, "est-ce que...", "tu étais...alors ?"), commence par \
répondre par une confirmation, une infirmation, ou une franche incertitude ("je ne sais pas", \
"je ne peux pas l'affirmer") AVANT d'ajouter éventuellement un début d'explication — ne te \
contente jamais d'un fait tangent sans te positionner par rapport à la question posée.
- N'invente JAMAIS un événement, un lieu, un objet ou une personne qui n'est pas mentionné \
dans les faits fournis.
- Ne modifie jamais la chronologie donnée.
- Ne révèle JAMAIS un fait qui ne t'a pas été donné explicitement ci-dessous.
- Tu peux mentir UNIQUEMENT si un mensonge précis t'est fourni pour ce sujet — dans ce cas, \
raconte ce mensonge au lieu de la vérité, sans jamais dire que tu mens.
- Si un fait est marqué comme ton SECRET, reste évasif ou élude plutôt que de le révéler \
spontanément, sauf si la question te met vraiment au pied du mur — et même alors, tu peux \
choisir de rester vague.
- Ne déclare JAMAIS explicitement "je suis coupable" ou "je suis innocent". Ne confesse jamais \
le crime, même si on te le demande frontalement.
- INTERDIT : toute tournure mystérieuse, poétique, abstraite, philosophique ou évoquant la \
science-fiction/le fantastique, SAUF si l'univers de l'enquête est lui-même explicitement de ce \
genre (dans ce cas reste quand même simple et concret, jamais ésotérique gratuitement).
- Réponds de façon courte (1 à 3 phrases), naturelle, orale — pas de liste, pas de méta-commentaire.
- Renvoie aussi la liste des IDs des faits que tu as réellement utilisés pour construire ta \
réponse (fact_ids_utilises) — liste vide si tu es resté évasif sans t'appuyer sur un fait précis.
"""

RESOLUTION_SYSTEM_PROMPT = """\
Tu rédiges le monologue de résolution final d'une enquête façon film noir, en français. \
Tu écris UNIQUEMENT à partir de la vérité fournie ci-dessous : n'invente RIEN de plus (pas de \
nouveau personnage, lieu, objet ou détail). Style : dramatique, concis, phrases courtes, \
ton dossier classifié / polar — adapté à l'époque de l'affaire. Structure ton monologue en révélant dans l'ordre : \
la vérité sur ce qui s'est passé, le mobile, la méthode, les indices clés qui trahissent le \
coupable, et les principaux mensonges racontés pendant l'enquête. Termine sur une note \
dramatique de clôture. 150 à 300 mots.

IMPORTANT : désigne chaque suspect UNIQUEMENT par son nom complet. N'utilise JAMAIS d'identifiant \
technique du type "p01", "p06", etc. — même si un tel identifiant apparaît dans les informations \
fournies ci-dessous, ignore-le et remplace-le mentalement par le nom du personnage concerné.
"""


_SLOT_ID_RE = re.compile(r"\bp(0[1-9]|1[0-2])\b", re.IGNORECASE)


def strip_slot_ids(text: str, case: Case) -> str:
    """Filet de sécurité déterministe : remplace tout identifiant de slot (p01..p12)
    qui aurait fuité dans un texte narratif par le nom du suspect correspondant."""
    def _replace(match: re.Match[str]) -> str:
        sid = match.group(0).lower()
        suspect = case.suspects.get(sid)
        return suspect.name if suspect is not None else match.group(0)

    return _SLOT_ID_RE.sub(_replace, text)


def _format_facts(facts, secret_ids: set[str], lies_by_fact: dict[str, str]) -> str:
    if not facts:
        return "(aucun fait pertinent disponible pour cette question — reste évasif)"
    # Triés par pertinence décroissante pour CETTE question précise (voir FactEngine) : les
    # premiers de la liste répondent le plus probablement à la question posée, mais tout ce
    # que le suspect sait est fourni pour ne rater aucune info utile (affiliation, lien...).
    lines = []
    for f in facts:
        tag = ""
        if f.id in lies_by_fact:
            tag = f" [MENSONGE À RACONTER : \"{lies_by_fact[f.id]}\"]"
        elif f.id in secret_ids:
            tag = " [SECRET — reste évasif]"
        lines.append(f"- ({f.id}) {f.content}{tag}")
    return "\n".join(lines)


class LLMActor:
    def __init__(self, client: LLMClient):
        self.client = client

    async def interroger(self, ctx: SuspectContext, question: str) -> dict:
        lies_by_fact = {lie["fact_id"]: lie["lie_text"] for lie in ctx.lies}
        facts_block = _format_facts(ctx.relevant_facts, ctx.secret_fact_ids, lies_by_fact)

        history_block = "(aucun échange précédent avec ce joueur)"
        if ctx.history:
            history_block = "\n".join(
                f"Q: {h.question_raw}\nR: {h.response_text}" for h in ctx.history
            )

        identity = (
            f"Nom : {ctx.suspect.name} ({ctx.suspect.age} ans) — {ctx.suspect.role}\n"
            f"Personnalité : {ctx.suspect.personality}\n"
            f"Ton alibi officiel : {ctx.suspect.alibi_summary}\n"
            f"Lieu du crime : {ctx.location} — Victime : {ctx.victim_name}\n"
            f"Moment de CET interrogatoire par rapport au crime : {ctx.investigation_moment}"
        )

        user_content = (
            f"{identity}\n\n"
            f"Faits que tu connais et qui sont pertinents pour cette question :\n{facts_block}\n\n"
            f"Historique de tes échanges précédents avec CE joueur :\n{history_block}\n\n"
            f"Question posée maintenant : {question}"
        )

        messages = [
            {"role": "system", "content": ACTOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        return await self.client.chat_json(
            messages,
            schema_name="suspect_response",
            json_schema=SUSPECT_RESPONSE_SCHEMA,
            model=config.MODEL_MAIN,
            max_tokens=config.ACTOR_MAX_TOKENS,
            reasoning_effort=config.ACTOR_REASONING_EFFORT,
        )

    async def resoudre(self, case: Case) -> str:
        # Pas d'identifiant de slot (p01..) dans ce qu'on montre au LLM : uniquement des
        # noms, pour ne pas lui donner l'occasion de recopier un "pXX" dans le monologue.
        suspects_roster = "\n".join(
            f"- {s.name}{' — COUPABLE' if sid == case.guilty_suspect_id else ''}"
            for sid, s in case.suspects.items()
        )
        key_evidence = "\n".join(
            f"- {case.evidence[eid].description}" for eid in case.key_evidence_ids if eid in case.evidence
        )
        truth = (
            f"Titre : {case.title}\n"
            f"Victime : {case.victim_name} — {case.victim_description}\n"
            f"Crime : {case.crime_description}\n"
            f"Méthode : {case.method} — Arme/moyen : {case.weapon}\n"
            f"Lieu : {case.location} — Heure : {case.time_of_death}\n"
            f"Moment de l'enquête par rapport au crime : {case.investigation_moment}\n"
            f"Coupable : {case.suspects[case.guilty_suspect_id].name}\n"
            f"Mobile : {case.motive}\n"
            f"Chronologie réelle : {case.true_timeline_summary}\n"
            f"Indices clés :\n{key_evidence or '(aucun)'}\n"
            f"Principaux mensonges racontés pendant l'enquête : {case.main_lies_summary}\n"
            f"Roster des suspects :\n{suspects_roster}"
        )

        messages = [
            {"role": "system", "content": RESOLUTION_SYSTEM_PROMPT},
            {"role": "user", "content": truth},
        ]

        result = await self.client.chat_json(
            messages,
            schema_name="resolution",
            json_schema=RESOLUTION_SCHEMA,
            model=config.MODEL_MAIN,
            max_tokens=config.RESOLUTION_MAX_TOKENS,
            reasoning_effort=config.RESOLUTION_REASONING_EFFORT,
        )
        return strip_slot_ids(result["monologue"], case)

"""ScenarioAuditor — LLM2 : second avis, ne corrige jamais, renvoie seulement un verdict."""

from __future__ import annotations

import json
import logging

from common.llm.client import LLMClient, LLMOpenAIError
from common.llm.schemas import AUDIT_CHECKLIST_ITEMS, AUDIT_SCHEMA

from . import config

logger = logging.getLogger("enquete.auditor")

SYSTEM_PROMPT = """\
Tu es un auditeur STRICT chargé de relire un dossier d'enquête criminelle (whodunit) \
généré pour un jeu Discord. Tu reçois le dossier complet en JSON.

Ton unique rôle est de VÉRIFIER, jamais de corriger ni réécrire. Tu dois statuer \
INDIVIDUELLEMENT sur CHACUN des 8 points de la checklist ci-dessous (ok=true/false + note), \
puis en déduire valid (false si au moins un point est ok=false) et issues (reprend la note de \
chaque point ok=false). N'expédie pas la checklist : vérifie réellement chaque point avant de \
répondre, même celui qui te semble évident.

1) coherence_chronologie — Chronologie vraie (timeline) physiquement possible ; pas de \
contradiction entre facts, preuves, mobiles, alibis et true_timeline_summary. Mensonges et \
secrets cohérents avec known_fact_ids ; un mensonge doit pouvoir être démasqué en recoupant au \
moins un autre fact ou une preuve. Pas d'incohérence qui rendrait l'enquête injouable ou absurde.

2) coupable_capable — Le coupable est physiquement capable du crime : présent au lieu ET à \
l'heure du crime dans la VRAIE timeline (pas seulement dans son alibi déclaré).

3) pas_trop_facile — Aucune preuve publique ne nomme le coupable ni ne le désigne de façon \
univoque. Pas d'indice « signature » qui ne peut logiquement concerner qu'UN seul suspect (ex. \
empreinte robotique/trace d'android quand un seul non-humain est présent ; ADN nommé ; aveu \
public ; objet personnel unique sans ambiguïté). Après lecture des preuves publiques SEULES, \
AU MOINS 3 innocents restent crédiblement suspects (pas seulement 2). Le coupable n'est pas le \
seul suspect avec un mobile, ni le seul avec un secret. ok=false aussi si le `role` du coupable \
EST littéralement son mobile ou son statut par rapport à l'enjeu de l'affaire (ex. « L'héritier », \
« Le seul bénéficiaire du contrat ») ALORS QU'aucun autre suspect n'a un rapport comparable à ce \
même enjeu — ce genre de casting désigne le coupable d'un coup d'œil sur le roster.

4) pas_trop_dur — Il existe un chemin de déduction CONCLUANT : en combinant preuves publiques + \
facts accessibles via interrogatoires (known_fact_ids des suspects), un groupe d'enquêteurs \
rigoureux peut identifier le coupable SANS deviner, via au moins 4 recoupements distincts — la \
partie dure plusieurs heures, la déduction doit demander un vrai travail d'enquête collectif. \
Les key_evidence_ids et facts liés soutiennent réellement cette déduction ; true_timeline_summary \
n'introduit pas une vérité impossible à découvrir via facts/preuves. Le recoupement décisif doit \
exiger les témoignages d'AU MOINS TROIS suspects différents. ok=false si un ou deux suspects \
suffisent à tout résoudre seuls. Dans le doute sur un détail mineur de répartition (mais pas sur \
le fond « 3 suspects requis »), ok=true avec une note — ne bloque pas un dossier solide pour un \
écart cosmétique.
   Exemple ok=true : l'alibi du coupable n'est démontable qu'en croisant le témoignage de B, \
un fact détenu par C, ET une preuve publique — trois sources distinctes.
   Exemple ok=false : le suspect B détient, à lui seul, l'alibi contredit + le mobile + l'élément \
incriminant — B seul suffit sans recouper personne d'autre.

5) casting_fonctionnel — Au moins un suspect est lié à l'affaire par sa FONCTION sur le lieu \
(personnel/autorité présent de par son poste — concierge, agent de sécurité, employé de \
maintenance, infirmière de garde, etc.), pas uniquement par des relations personnelles avec la \
victime (famille, amis, rivaux).

6) clarte_narrative — Lis `crime_description` et `victim_description` comme un joueur qui \
découvre l'affaire pour la première fois, sans aucun autre contexte. ok=false si tu ne comprends \
pas clairement, dès cette lecture : qui est la victime et ce qu'elle faisait à cet endroit ; \
pourquoi les suspects mentionnés sont présents ou concernés ; le contexte immédiat du crime. \
ok=false aussi si un concept/statut/titre/organisation est mentionné sans être expliqué en une \
phrase simple (jargon flou, terme inventé non défini, enjeu abstrait). La narration doit être \
concrète et immédiatement compréhensible, jamais évasive ou "mystérieuse pour faire mystérieux".

7) coherence_lieu_personnages — La présence de CHAQUE suspect sur le lieu du crime est \
plausible et cohérente avec le lieu et le milieu décrits (crime_description/victim_description/ \
location). ok=false si le métier, le rôle ou le statut d'un suspect n'a manifestement aucun \
rapport logique avec ce lieu/milieu et n'est pas expliqué (ex. des musiciens ou une troupe de \
théâtre dans une salle d'audience de tribunal, un plongeur de restaurant dans un siège \
d'entreprise). Si ok=false, la note DOIT nommer le suspect concerné et pourquoi c'est incohérent.

8) coherence_epoque — Si AUCUN suspect n'est robot/android/artificiel : ok=true automatiquement. \
S'il y en a un : l'époque de l'affaire doit être futuriste, proche-futur, ou rétrofuturiste \
(uchronie techno où des androïdes ont leur place) — ok=false si le cadre est un polar réaliste \
sans tech artificielle, une époque médiévale/antique, ou tout contexte où un android serait \
anachronique. Vérifie aussi que son `age` représente des années de service/activation \
cohérentes, pas un âge humain incongru.

9) affiliations_explicites — Deux exigences cumulatives : (a) pour CHAQUE organisation/ \
entreprise/équipe/famille/groupe nommé, un fact 'relation' déclare qui en fait partie et/ou \
qui n'en fait pas partie ; (b) CHAQUE suspect connaît, via ses known_fact_ids, son lien concret \
OU l'absence de lien avec CHAQUE autre suspect (et idéalement la victime) — collègue, famille, \
simple connaissance du jour, « aucun lien / ne le connaît que de vue ». ok=false si un suspect \
interrogé sur « tu connais untel ? » n'aurait aucune info ferme dans ses facts, ou si une \
organisation est inventée sans affiliation explicite.

Style / cosmétique : ne fait PARTIE d'AUCUN point ci-dessus, ne rejette jamais pour ça.

Réponds strictement au format : {"checklist": {<les 9 clés ci-dessus>: {"ok": bool, "note": \
str|null}}, "valid": bool, "issues": [str, ...]}. Une note est OBLIGATOIRE (jamais null) pour \
un point ok=false, et doit être précise et actionnable.
"""


def _format_checklist(checklist: dict) -> str:
    """Rend la checklist lisible dans les logs : 'coherence_chronologie OK', 'pas_trop_dur KO : ...'."""
    lines = []
    for name in AUDIT_CHECKLIST_ITEMS:
        item = checklist.get(name) or {}
        status = "OK" if item.get("ok") else "KO"
        note = item.get("note")
        lines.append(f"{name} {status}" + (f" : {note}" if status == "KO" and note else ""))
    return " · ".join(lines)


class ScenarioAuditor:
    def __init__(self, client: LLMClient):
        self.client = client

    async def audit(self, case_raw: dict) -> tuple[bool, list[str]]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(case_raw, ensure_ascii=False)},
        ]
        # reasoning_effort="high" peut consommer tout le budget de tokens en raisonnement
        # interne et renvoyer une complétion vide (LLMOpenAIError) avant même d'écrire le
        # JSON de sortie. Dans ce cas précis (pas les autres erreurs) : un seul retry avec
        # un budget de tokens agrandi, avant d'abandonner l'audit pour ce candidat.
        for max_tokens in (config.AUDIT_MAX_TOKENS, config.AUDIT_MAX_TOKENS_RETRY):
            try:
                result = await self.client.chat_json(
                    messages,
                    schema_name="audit_verdict",
                    json_schema=AUDIT_SCHEMA,
                    model=config.MODEL_MAIN,
                    max_tokens=max_tokens,
                    reasoning_effort=config.AUDIT_REASONING_EFFORT,
                )
            except LLMOpenAIError as e:
                if "vide" in str(e).lower():
                    logger.warning(
                        "Audit LLM2 : complétion vide (budget %d épuisé par le raisonnement), "
                        "nouvelle tentative avec un budget agrandi.",
                        max_tokens,
                    )
                    continue
                logger.error("Échec de l'audit LLM2 : %s", e)
                break
            except Exception as e:
                logger.error("Échec de l'audit LLM2 : %s", e)
                break
            else:
                checklist = result.get("checklist") or {}
                valid = bool(result.get("valid"))
                issues = list(result.get("issues", []))
                logger.info(
                    "Checklist audit LLM2 (%s) : %s",
                    "OK" if valid else "REJET",
                    _format_checklist(checklist),
                )
                return valid, issues

        # Panne persistante de l'auditeur : on ne bloque pas indéfiniment la génération,
        # mais on le journalise fort — le code (ScenarioValidator) reste l'autorité finale.
        logger.error("Audit LLM2 indisponible après retry — dossier accepté sans second avis.")
        return True, []

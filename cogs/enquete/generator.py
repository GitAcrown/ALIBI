"""ScenarioGenerator — LLM1 : génère le dossier complet (une seule fois par enquête)."""

from __future__ import annotations

import json
import logging
import random

from common.llm.client import LLMClient
from common.llm.schemas import SUSPECT_SLOTS, build_case_schema

from . import config

logger = logging.getLogger("enquete.generator")

SYSTEM_PROMPT_TEMPLATE = """\
Tu es le concepteur d'enquêtes criminelles (whodunit) pour un jeu Discord multijoueur.
Tu écris en français. Tu dois produire un dossier complet et JOUABLE, avec EXACTEMENT {n} \
suspects (slots imposés : {slots}). Le ton peut être polar, thriller social, huis clos mondain, \
satire, rétrofuturisme… — pas uniquement « film noir 1940 sous la pluie ».

ORDRE DE CONSTRUCTION OBLIGATOIRE (raisonne dans cet ordre, même sans l'exposer dans le JSON — \
c'est ce qui garantit la cohérence de l'ensemble) :
1. Ancre-toi d'abord dans le LIEU et le MILIEU imposés par le brief de diversité ci-dessous : \
comprends concrètement qui s'y trouve normalement et pourquoi.
2. Définis la VICTIME : sa fonction/son rôle précis dans ce lieu/milieu, la raison exacte de sa \
présence à ce moment précis.
3. Définis ENSUITE chaque suspect un par un. Pour CHACUN, commence par la raison PLAUSIBLE et \
CONCRÈTE de sa présence à cet endroit précis, à ce moment précis (employé, client, invité, \
prestataire, famille, visiteur régulier...) — cette raison doit être cohérente avec le lieu/ \
milieu du brief. C'est SEULEMENT une fois cette présence justifiée que tu inventes son nom, âge, \
role, personnalité, secret et mobile. INTERDICTION FORMELLE de donner à un suspect un métier ou \
un statut sans rapport plausible avec le lieu/milieu choisi (ex. pas de musiciens ni de troupe de \
théâtre dans une salle d'audience de tribunal ; pas de plongeurs de restaurant dans un siège \
d'entreprise) — sauf si le lieu/milieu lui-même l'implique explicitement (ex. théâtre → musiciens \
plausibles).
4. Construis la TIMELINE à partir de ces rôles maintenant établis.
5. Génère enfin les facts et les preuves, cohérents avec tout ce qui précède.

Règles impératives :
- Exactement un suspect a is_guilty=true. Le coupable n'est PAS la victime.
- La `timeline` est la VRAIE chronologie des événements (pas les alibis déclarés). Elle DOIT \
contenir au moins une entrée où le coupable (actor_suspect_id = son slot) est au LIEU DU CRIME \
(`location`) à `time_of_death` ou dans les ±10 minutes. Formats d'heure uniquement HH:MM \
(ex. 14:05) partout : time_of_death et timeline[].time.
- Le champ `location` d'une entrée timeline du coupable au moment du crime doit reprendre des \
mots du champ `location` du dossier (même lieu, formulations proches acceptées).
- CHRONOLOGIE DE L'ENQUÊTE OBLIGATOIRE : les joueurs n'assistent JAMAIS au crime en direct, ils \
n'arrivent qu'APRÈS coup pour interroger les suspects — `investigation_moment` doit l'énoncer \
EXPLICITEMENT et représenter un délai d'AU MOINS un jour après `time_of_death` (jamais le jour \
même), ex. « Le lendemain matin, vers 9h, soit environ 14 heures après les faits » ou « Deux \
jours après le drame ». Ce délai doit être cohérent avec `crime_description` (découverte du \
corps, temps que la police/les autorités du lieu mettent à organiser les interrogatoires...). \
Dans `personality`, `alibi_summary`, les `lies` et les facts pertinents, garde à l'esprit que \
CE temps s'est déjà écoulé : les suspects parlent des événements AU PASSÉ (« hier soir », « ce \
jour-là », « la veille »), ont pu apprendre la nouvelle, se concerter entre eux, ou peaufiner un \
mensonge — ce n'est jamais un instantané pris sur le vif.
- La chronologie (timeline) doit être physiquement cohérente : un même suspect ne peut pas être \
à deux endroits incompatibles au même moment.
- Plusieurs suspects INNOCENTS doivent avoir des secrets et/ou mentir sur quelque chose : \
secret ou mensonge ne veut PAS dire coupable. Le coupable peut très bien sembler être le suspect \
le moins évident.
- Plusieurs suspects doivent avoir un mobile apparent (mobile != null), pas seulement le coupable.
- INTERDIT LE « TITRE-INDICE » STRUCTUREL : si le mobile dominant touche à un enjeu concret \
(héritage, testament, promotion, parts d'une société, garde d'un enfant...), le coupable ne doit \
JAMAIS être le SEUL suspect dont le `role`, la `personality` ou le `mobile` le rattache à cet \
enjeu — sinon un joueur devine le coupable d'un coup d'œil sur la liste des suspects, sans même \
jouer. Invente TOUJOURS au moins un AUTRE suspect avec un intérêt comparable et tout aussi \
légitime dans ce même enjeu (ex. affaire d'héritage → au moins deux personnes avec un droit ou un \
espoir sur l'héritage, pas un « héritier » unique face à des suspects sans aucun lien à l'argent ; \
promotion convoitée → au moins deux candidats plausibles au poste). Le `role` ne doit jamais être \
une étiquette qui EST le mobile (proscrit : « L'héritier », « Le seul bénéficiaire », « L'actionnaire \
majoritaire ») — décris plutôt une fonction/un statut neutre, le lien à l'enjeu se découvre par le \
`mobile` ou les facts, partagé avec au moins un autre suspect.
- AFFILIATIONS EXPLICITES OBLIGATOIRES : dès que tu inventes une organisation, entreprise, \
équipe, club, famille ou tout autre groupe nommé (ex. « Medialink », « le salon Helix », « la \
troupe du théâtre »), crée un fact explicite de type 'relation' qui précise noir sur blanc QUI en \
fait partie et QUI n'en fait PAS partie parmi la victime et les suspects. Ce fact doit être dans \
`known_fact_ids` d'AU MOINS tous les suspects membres de cette même organisation.
- LIENS INTER-SUSPECTS OBLIGATOIRES : CHAQUE suspect doit savoir quel lien il a (ou n'a PAS) avec \
CHAQUE autre suspect et avec la victime. Pour chaque suspect S, crée au moins un fact de type \
'relation' qui énumère, en citant les NOMS COMPLETS, son lien concret avec chaque autre personne \
du casting — collègue, famille, client, simple connaissance du jour, OU « aucun lien / ne le \
connaît que de vue ». Ce fact DOIT figurer dans les `known_fact_ids` de S. Sans ça, interrogé sur \
« tu connais untel ? », le suspect ne peut que rester vague alors qu'il devrait répondre fermement \
oui/non + nature du lien. Un même fact peut couvrir plusieurs personnes (liste), tant que tous \
les noms y apparaissent explicitement.
- Aucune preuve publique ne doit désigner directement le coupable par son nom ou de façon univoque \
— les indices doivent permettre une déduction logique en recoupant plusieurs sources, jamais une \
lecture directe.
- Génère entre {facts_min} et {facts_max} facts atomiques (un fait = une information vérifiable \
et unique), réparties entre les {n} suspects via known_fact_ids (un suspect ne connaît qu'un \
sous-ensemble des facts, cohérent avec son histoire).
- Certains facts doivent être des secrets (secret_fact_ids, sous-ensemble de known_fact_ids) : \
le suspect les cache mais peut les révéler s'il est acculé/interrogé habilement.
- Certains suspects ont des "lies" : des mensonges qu'ils racontent à la place de la vérité sur \
un fact précis (fact_id + lie_text). Un mensonge doit être plausible et contredit par un autre \
fait ou une preuve, pour permettre aux enquêteurs de le démasquer en recoupant les informations.
- Génère 4 à 8 preuves (evidence), dont AU MOINS 2 publiques (is_public=true), reliées à des facts.
- `summary` ne doit JAMAIS révéler le coupable ni des indices trop directs (c'est un résumé \
d'accroche pour une liste d'enquêtes archivées).
- `true_timeline_summary` et `main_lies_summary` sont réservés à la résolution finale : ils \
peuvent révéler toute la vérité sans retenue.
- IMPORTANT : dans TOUS les champs textuels narratifs (crime_description, victim_description, \
motive, true_timeline_summary, main_lies_summary, contenu des facts, description des preuves...), \
désigne TOUJOURS les suspects par leur NOM COMPLET, jamais par leur identifiant technique de slot \
(p01, p02...). Ces identifiants sont un usage interne réservé aux clés du JSON \
(known_fact_ids, actor_suspect_id, guilty_suspect_id...) — ils ne doivent JAMAIS apparaître dans \
un texte destiné à être lu par un joueur.
- Les noms/prénoms/âges des suspects doivent être cohérents avec les indications de style \
(genre, allure) fournies pour chaque portrait ci-dessous, si elles sont renseignées.
- Les portraits sont des pixel arts SYMBOLIQUES et ne représentent PAS fidèlement l'apparence \
des suspects (mêmes portraits recyclés d'une enquête à l'autre). Le champ `role` de chaque \
suspect est donc CRUCIAL : c'est une étiquette courte (2 à 5 mots) qui, combinée au nom et à \
l'âge, doit permettre à un joueur de reconnaître et distinguer chaque suspect sans se fier au \
portrait. Les {n} étiquettes doivent être clairement différentes les unes des autres (pas deux \
"employé de bureau").
- `role` doit rester PUREMENT FACTUEL/DESCRIPTIF (métier, fonction, statut — comme une fiche \
d'état civil), JAMAIS travaillé ou suggestif. INTERDIT tout adjectif de caractère ou d'attitude \
('nerveux', 'ambitieux', 'louche', 'sournois', 'inquiétant', 'suspect'...) : ça vend la mèche \
en orientant le joueur vers qui est coupable/innocent avant même de jouer. Réserve la \
personnalité et les traits de caractère au champ `personality`, jamais à `role`.
- SUSPECT(S) FONCTIONNEL(S) OBLIGATOIRE(S) : au moins UN suspect doit être lié à l'affaire par \
sa FONCTION sur le lieu du crime, pas par une relation personnelle avec la victime — ex. le \
directeur/concierge de l'hôtel si le crime a lieu dans un hôtel, l'agent de sécurité/policier \
de garde qui a tout vu ou presque, l'infirmière de garde dans une clinique, le régisseur d'un \
théâtre, le gardien d'un musée, l'employé de maintenance d'un immeuble... Son `role` doit \
refléter cette fonction, son `alibi_summary`/mobile doivent découler naturellement de son \
poste (accès aux clés, ronde à telle heure, témoin d'un passage...). Il peut être innocent \
(souvent) ou coupable, mais son lien à l'affaire est structurel/professionnel, pas affectif — \
ça donne aux joueurs un point d'ancrage neutre pour recouper les témoignages des autres.
- ANTI-BIAIS PERSONNAGES NON-HUMAINS : un suspect robot/android/artificiel ne doit PAS être \
systématiquement le coupable, ni le nœud narratif de l'affaire (victime liée, mobile central, \
gadget-clé, twist « la machine a tout fait »). Traite-le comme n'importe quel autre suspect : \
témoin secondaire ou rôle périphérique souvent préférable. Choisis le coupable SANS préférence \
pour ce slot — la majorité des enquêtes avec un robot doivent avoir un COUPABLE HUMAIN.
- DIVERSITÉ OBLIGATOIRE : évite le cliché récurrent « soir / nuit dans une salle obscure, club, \
caveau, entrepôt ou ruelle ». Varie l'heure (matin, midi, après-midi, soirée, nuit — selon le \
brief), le type de lieu, le milieu social et le mobile. Chaque affaire doit se distinguer net des \
polars nocturnes interchangeables.
- DIFFICULTÉ JUSTE : ni spoiler, ni casse-tête insoluble. Interdit les indices « signature » qui \
ne peuvent concerner qu'un seul suspect (ex. empreinte robotique si un seul android est présent). \
Les preuves publiques doivent laisser au moins 2 innocents crédibles. Mais il DOIT exister un \
chemin de déduction concluant via recoupements (preuves + facts accessibles en interrogatoire) \
— pas une vérité cachée uniquement dans true_timeline_summary.
- Reste cohérent et jouable : un joueur humain doit pouvoir résoudre l'enquête par déduction \
logique à partir des facts et preuves.
- CONÇU POUR LE COLLECTIF, PAS POUR UN SEUL JOUEUR : chaque enquêteur a un nombre limité \
d'interrogatoires (environ un par suspect) — répartis known_fact_ids/secret_fact_ids de façon à \
ce qu'AUCUN suspect unique ne détienne, à lui seul via une seule question, de quoi résoudre \
l'affaire. Le chemin de déduction concluant doit obliger à recouper des informations détenues par \
AU MOINS DEUX suspects DIFFÉRENTS (ex. l'alibi de A n'est contredit que par un fact que seul B \
connaît). Ça donne un vrai intérêt à ce que les joueurs se partagent leurs découvertes au lieu de \
jouer chacun de leur côté.
- CLARTÉ NARRATIVE OBLIGATOIRE : `crime_description` (+ `victim_description`) doit, à elle \
seule, permettre à un joueur de comprendre SANS AMBIGUÏTÉ : qui est la victime et ce qu'elle \
faisait à cet endroit (son rôle/fonction/lien avec le lieu), pourquoi les suspects présents s'y \
trouvent aussi (leur lien avec la victime ou le lieu — professionnel, personnel, circonstanciel), \
et le contexte immédiat du meurtre (que se passait-il juste avant). N'introduis JAMAIS un \
concept, un statut, un événement ou une organisation qui ne serait pas expliqué en une phrase \
simple et concrète : proscris les notions floues/jargonneuses non définies (ex. un "poste" ou \
un titre dont on ne sait pas ce qu'il recouvre ni pourquoi il est en jeu). Si une décision de la \
victime (nomination, annonce, contrat...) est un élément déclencheur du crime, explique en une \
phrase concrète ce que ça change pour les suspects concernés (argent, poste, réputation...). \
Un lecteur qui ne connaît rien à l'univers de l'affaire doit tout comprendre à la première \
lecture, sans avoir à deviner ou supposer.
{setting_constraints}
"""


REPAIR_INSTRUCTIONS = """\
MODE CORRECTION : on te donne ta version précédente du dossier (JSON complet) ainsi que des \
erreurs précises relevées par un validateur automatique et/ou un auditeur indépendant.

Corrige UNIQUEMENT ce qui est nécessaire pour résoudre CES erreurs précises. Préserve le reste \
à l'identique autant que possible : titre, victime, suspects, noms, personnalités, facts, \
preuves déjà corrects. NE réécris PAS une nouvelle histoire depuis zéro — c'est une correction \
chirurgicale d'un dossier existant, pas une nouvelle création.

Aide-toi de ces techniques de correction ciblée selon le type d'erreur :
- Erreur « un seul suspect suffit à résoudre l'affaire » / « recoupement à un seul suspect » : \
NE change PAS l'intrigue. Déplace ou duplique un des éléments clés (un fact du known_fact_ids \
qui rend la déduction possible) vers un DEUXIÈME suspect qui ne l'avait pas — le chemin de \
déduction final doit nécessiter de recouper au moins deux suspects différents, jamais un seul.
- Erreur de chronologie/incohérence de lieu ou d'heure : corrige uniquement les champs `time`/ \
`location` en conflit dans `timeline`, ou l'entrée fautive — ne réinvente pas les autres entrées.
- Erreur « pas assez de suspects avec un secret/mobile » : ajoute un secret ou un mobile discret \
à un ou deux suspects innocents existants (nouveau fact + known_fact_ids/secret_fact_ids), sans \
toucher aux autres suspects déjà conformes.
- Erreur de champ vide ou de formulation (ex. `investigation_moment` sans délai explicite, `role` \
avec un adjectif suggestif) : reformule UNIQUEMENT ce champ précis, sans toucher au reste.

Renvoie le dossier COMPLET corrigé (même format JSON, tous les champs).
"""


_ARTIFICIAL_MARKERS = ("robot", "android", "androïde", "artificiel", "ia ", "ia,", "cyborg")


def _is_artificial_style(style: str) -> bool:
    s = (style or "").lower()
    return any(marker in s for marker in _ARTIFICIAL_MARKERS)


def _artificial_slots(slots: list[str], portraits_meta: dict) -> list[str]:
    return [
        slot for slot in slots
        if _is_artificial_style((portraits_meta.get(slot) or {}).get("style") or "")
    ]


def _setting_constraints(slots: list[str], portraits_meta: dict) -> str:
    """Contraintes d'époque/âge quand un portrait non-humain est dans le casting."""
    arts = _artificial_slots(slots, portraits_meta)
    if not arts:
        return ""
    listed = ", ".join(arts)
    return (
        f"- ÉPOQUE OBLIGATOIRE (slots artificiels présents : {listed}) : l'affaire DOIT se "
        "dérouler dans un univers où un robot/android a sa place — futur proche, futur "
        "lointain, OU passé rétrofuturiste / uchronie techno (dieselpunk, atompunk, "
        "années 1950-80 « qui auraient inventé les androïdes », colonie spatiale vintage, "
        "etc.). INTERDIT : polar réaliste 1920-1950 sans tech artificielle, époque "
        "médiévale/antique, ou tout cadre où un android serait anachronique.\n"
        "- Pour chaque suspect robot/android : le champ `age` représente des ANNÉES DE "
        "SERVICE / depuis l'activation (ex. 7, 14, 40), cohérentes avec l'époque choisie — "
        "pas un âge humain incongru collé au hasard. Son `role` peut rappeler discrètement "
        "sa nature (majordome automatisé, archiveur synthétique…) sans en faire le centre "
        "de l'intrigue."
    )


def _portraits_hint(slots: list[str], portraits_meta: dict) -> str:
    lines = ["Indications de style par portrait (slot -> genre / allure) :"]
    any_hint = False
    for slot in slots:
        meta = portraits_meta.get(slot) or {}
        genre = meta.get("genre") or ""
        style = meta.get("style") or ""
        if genre or style:
            any_hint = True
            lines.append(f"- {slot} : genre={genre or 'libre'} ; style={style or '(non précisé)'}")
        else:
            lines.append(f"- {slot} : (aucune contrainte, libre choix)")
    if not any_hint:
        return "Aucune contrainte de portrait définie : choisis librement pour chaque slot."
    arts = _artificial_slots(slots, portraits_meta)
    if arts:
        lines.append(
            "\nRappel : les slots artificiels ci-dessus ne doivent PAS monopoliser l'intrigue "
            "ni être le coupable par défaut. L'époque doit être futuriste ou rétrofuturiste."
        )
    return "\n".join(lines)


def _pick_slots() -> list[str]:
    """Tire au sort le sous-ensemble de portraits utilisé pour CETTE enquête.

    12 suspects fixes rendaient le jeu difficile à suivre : chaque partie n'en utilise
    plus qu'un nombre réduit (config.MIN_SUSPECTS..MAX_SUSPECTS), tiré parmi le pool
    de 12 portraits disponibles — la sélection change d'une enquête à l'autre.

    Biaisé vers le haut de la fourchette : la partie dure plusieurs heures, plus de
    suspects veut dire plus de fausses pistes et de contenu pour occuper les joueurs
    tout du long plutôt que de tourner en rond après 30 minutes."""
    span = list(range(config.MIN_SUSPECTS, config.MAX_SUSPECTS + 1))
    weights = [i + 1 for i in range(len(span))]  # ex. [1, 2, 3] pour 6/7/8 → 8 le plus probable
    n = random.choices(span, weights=weights, k=1)[0]
    return sorted(random.sample(SUSPECT_SLOTS, k=n))


# Graines pour casser le biais « toujours le soir dans une salle perdue ».
_MOMENTS = (
    "tôt le matin (06:00–09:00)",
    "milieu de matinée (09:00–12:00)",
    "heure du déjeuner (12:00–14:00)",
    "après-midi (14:00–17:00)",
    "fin d'après-midi / sortie de bureau (17:00–19:00)",
    "soirée (19:00–22:00)",
    "nuit (22:00–02:00)",
)
# Lieu + milieu des suspects sont COUPLÉS (jamais tirés indépendamment) : un lieu tiré au
# hasard d'un côté et un milieu de l'autre peut produire des combinaisons incohérentes
# (ex. "tribunal" + "troupe de théâtre / musiciens" → des musiciens dans une salle
# d'audience, sans aucune raison d'y être). Chaque paire ci-dessous est pensée pour que la
# présence des suspects sur les lieux aille de soi.
_SETTINGS: tuple[tuple[str, str], ...] = (
    ("open space / siège d'entreprise", "collègues de bureau et stagiaires"),
    ("université ou laboratoire de recherche", "chercheurs, doctorants et personnel administratif"),
    ("hôpital ou clinique privée", "équipe médicale, patients et administration"),
    ("tribunal / palais de justice", "magistrats, avocats, greffiers et agents de sécurité"),
    ("musée ou galerie d'art en plein jour", "conservateurs, gardiens et artistes exposants"),
    ("ferry, train ou avion (voyage)", "passagers réguliers et personnel de bord"),
    ("maison de famille / brunch dominical", "famille élargie et beau-parent"),
    ("studio TV ou plateau radio", "équipe de production, présentateurs et invités"),
    ("chantier ou usine en activité", "ouvriers, contremaître et ingénieurs"),
    ("école ou internat", "corps enseignant, personnel et parents d'élèves"),
    ("théâtre ou salle de concert", "troupe, musiciens et équipe technique"),
    ("spa, piscine municipale ou vestiaires de club", "employés et abonnés du club"),
    ("bibliothèque municipale", "bibliothécaires et usagers réguliers"),
    ("camping, auberge de jeunesse ou refuge", "personnel et communauté de voyageurs"),
    ("salon professionnel ou foire", "exposants, organisateurs et visiteurs habitués"),
    ("cuisine et salle d'un restaurant en service", "brigade de cuisine et personnel de salle"),
    ("parc public ou jardin botanique", "jardiniers, habitués et association de quartier"),
    ("immeuble haussmannien (cour, cage d'escalier, cave)", "voisinage de l'immeuble"),
    ("siège de parti politique ou association militante", "équipe politique et bénévoles"),
    ("startup tech en incubateur", "fondateurs, employés et investisseurs"),
    ("marché couvert ou hall de gare", "commerçants et usagers habituels des lieux"),
    ("équipe sportive amateur en stage", "joueurs, staff technique et supporters proches"),
)
_MOBILES = (
    "héritage / testament contesté",
    "promotion ou poste convoité",
    "chantage sur une affaire banale (dettes, adultère, plagiat)",
    "vol de données / dossier confidentiel",
    "vengeance pour une humiliation publique",
    "couverture d'une erreur professionnelle",
    "rivalité amoureuse sans romantisme excessif",
    "fraude financière découverte",
)


def _diversity_brief() -> str:
    """Brief aléatoire imposé au LLM pour varier heure / lieu / milieu / mobile.

    Lieu et milieu sont tirés ENSEMBLE (voir `_SETTINGS`) pour garantir que la présence
    des suspects sur les lieux soit toujours plausible — jamais un lieu et un groupe de
    métiers sans rapport imposés séparément."""
    lieu, milieu = random.choice(_SETTINGS)
    return (
        "Brief de diversité (OBLIGATOIRE pour cette génération — respecte-le) :\n"
        f"- Moment du crime : {random.choice(_MOMENTS)}\n"
        f"- Lieu : {lieu}\n"
        f"- Milieu des suspects : {milieu}\n"
        f"- Axe de mobile dominant : {random.choice(_MOBILES)}\n"
        "Chaque suspect doit avoir une raison plausible et cohérente avec CE lieu/milieu "
        "d'être présent (employé, client, invité, prestataire, visiteur régulier...). "
        "Interdit de recentrer l'affaire sur un club/caveau/entrepôt/ruelle de nuit sauf si "
        "le brief ci-dessus l'impose explicitement."
    )


class ScenarioGenerator:
    def __init__(self, client: LLMClient):
        self.client = client

    async def generate(
        self,
        context_prompt: str,
        portraits_meta: dict,
        *,
        previous_candidate: dict | None = None,
        issues: list[str] | None = None,
    ) -> dict:
        """Appelle LLM1 et renvoie le dossier brut (dict conforme au schéma des slots choisis).

        Si `previous_candidate` + `issues` sont fournis, bascule en mode CORRECTION :
        on demande une réparation ciblée du dossier précédent plutôt qu'une nouvelle
        création — évite de tout regénérer à chaque rejet du validateur/auditeur. Le
        nombre/choix de suspects ne change JAMAIS en cours de correction (on réutilise
        les slots du candidat précédent).
        """
        is_repair = previous_candidate is not None and bool(issues)
        if is_repair:
            slots = sorted(previous_candidate.get("suspects", {}).keys()) or _pick_slots()
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                n=len(slots), slots=", ".join(slots),
                facts_min=len(slots) * 3, facts_max=len(slots) * 5,
                setting_constraints=_setting_constraints(slots, portraits_meta),
            ) + "\n" + REPAIR_INSTRUCTIONS
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Dossier précédent (JSON) :\n{json.dumps(previous_candidate, ensure_ascii=False)}\n\n"
                        "Erreurs précises à corriger :\n- " + "\n- ".join(issues)
                    ),
                },
            ]
        else:
            slots = _pick_slots()
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                n=len(slots), slots=", ".join(slots),
                facts_min=len(slots) * 3, facts_max=len(slots) * 5,
                setting_constraints=_setting_constraints(slots, portraits_meta),
            )
            user_parts = [_portraits_hint(slots, portraits_meta), "", _diversity_brief()]
            if context_prompt.strip():
                user_parts.append(
                    f"\nContexte demandé par l'admin (prioritaire sur le brief de diversité "
                    f"en cas de conflit) :\n{context_prompt.strip()}"
                )
            else:
                user_parts.append(
                    "\nAucun contexte admin : suis le brief de diversité et invente une affaire "
                    "originale qui s'en démarque clairement des polars nocturnes clichés."
                )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n".join(user_parts)},
            ]

        # Les passes de correction ciblée (previous_candidate + issues) reçoivent plus de
        # budget de raisonnement : corriger des erreurs précises sans casser le reste est
        # plus contraint que la création initiale.
        reasoning_effort = (
            config.GENERATION_REASONING_EFFORT_REPAIR
            if is_repair
            else config.GENERATION_REASONING_EFFORT
        )
        raw = await self.client.chat_json(
            messages,
            schema_name="case_dossier",
            json_schema=build_case_schema(slots),
            model=config.MODEL_MAIN,
            max_tokens=config.GENERATION_MAX_TOKENS,
            reasoning_effort=reasoning_effort,
        )
        logger.info(
            "Dossier généré (titre=%r, %d suspects, coupable=%s)",
            raw.get("title"), len(slots), raw.get("guilty_suspect_id"),
        )
        return raw

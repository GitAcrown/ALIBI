"""ScenarioValidator — validation déterministe du dossier généré (LLM1).

Aucune de ces règles ne fait appel à un LLM : c'est le code qui est l'autorité finale.
`validate()` renvoie une liste d'erreurs (vide = dossier valide, prêt pour l'audit LLM2).
Cette étape est volontairement INSTANTANÉE — l'appel LLM lent, c'est ScenarioAuditor ensuite.
"""

from __future__ import annotations

import re
import unicodedata

from . import config
from common.llm.schemas import SUSPECT_SLOTS

_TIME_RE = re.compile(
    r"(?P<h>\d{1,2})\s*[h:]\s*(?P<m>\d{2})",
    re.IGNORECASE,
)

# Mots trop génériques pour juger qu'un lieu « matche » le lieu du crime.
_LOC_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "au", "aux", "en",
    "et", "ou", "a", "à", "sur", "sous", "dans", "par", "pour", "avec", "chez",
    "the", "of", "at", "to", "in", "on",
}

# Marqueurs (tokens normalisés) qui prouvent qu'un délai d'au moins un jour est explicitement
# énoncé dans `investigation_moment` — sert de garde-fou déterministe contre un LLM qui situerait
# implicitement l'enquête le jour même du crime.
_DELAY_DAY_MARKERS = {"lendemain", "surlendemain", "jours", "jour", "semaine", "semaines"}
_SAME_DAY_MARKERS = {"immediatement", "instant", "meme", "aussitot", "tandis"}
# Un délai en heures ne compte que s'il est déjà proche d'une journée (≥ 12h) — "quelques
# heures après" reste implicitement le jour même du crime.
_HOURS_RE = re.compile(r"(\d{1,3})\s*(?:h\b|heures?)", re.IGNORECASE)


def _has_explicit_day_delay(text: str) -> bool:
    if _normalize_tokens(text) & _DELAY_DAY_MARKERS:
        return True
    return any(int(h) >= 12 for h in _HOURS_RE.findall(text))

# Marqueurs (tokens normalisés) d'un enjeu concret (héritage, promotion...) qui, s'il n'est
# rattaché QU'AU coupable via role/mobile, désigne le coupable d'un coup d'œil sans même jouer.
_STRUCTURAL_STAKE_MARKERS = {
    "heritier", "heritiere", "heritage", "legataire", "legs", "testament", "succession",
    "beneficiaire", "actionnaire", "parts", "promotion", "poste", "contrat", "brevet",
    "royalties", "dividendes", "garde",
}


def _stake_tokens(suspect: dict) -> set[str]:
    text = f"{suspect.get('role', '')} {suspect.get('mobile') or ''}"
    return _normalize_tokens(text) & _STRUCTURAL_STAKE_MARKERS


# `role` doit être factuel (métier/statut) — ces adjectifs de caractère/attitude vendent la
# mèche en orientant vers qui est suspect/innocent avant même de jouer.
_ROLE_SUSPICIOUS_ADJECTIVES = {
    "nerveux", "nerveuse", "louche", "sournois", "sournoise", "inquiétant", "inquiétante",
    "suspect", "suspecte", "menaçant", "menaçante", "coupable", "innocent", "innocente",
    "ambitieux", "ambitieuse", "sombre", "mystérieux", "mystérieuse", "étrange",
    "manipulateur", "manipulatrice", "cruel", "cruelle", "malhonnête", "sinistre",
    "tourmenté", "tourmentée", "vicieux", "vicieuse", "froid", "froide", "calculateur",
    "calculatrice", "dangereux", "dangereuse",
}


def _parse_minutes(value: str | None) -> int | None:
    """Convertit '23:30', '23h30', '23H30'… en minutes depuis minuit, ou None."""
    if not value:
        return None
    m = _TIME_RE.search(str(value).strip())
    if not m:
        return None
    h, mi = int(m.group("h")), int(m.group("m"))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def _times_close(a: str | None, b: str | None, *, window: int) -> bool:
    ma, mb = _parse_minutes(a), _parse_minutes(b)
    if ma is None or mb is None:
        return False
    diff = abs(ma - mb)
    # Gère le passage minuit (23:50 vs 00:10).
    diff = min(diff, 24 * 60 - diff)
    return diff <= window


def _normalize_tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    tokens = set(re.findall(r"[a-z0-9]{3,}", folded))
    return tokens - _LOC_STOPWORDS


def _locs_compatible(crime_loc: str, candidate: str) -> bool:
    """Vrai si le lieu candidat partage un token significatif avec le lieu du crime.

    Évite l'égalité stricte trop fragile ('Caveau du Neon' vs 'caveau arrière du Neon').
    """
    a, b = _normalize_tokens(crime_loc), _normalize_tokens(candidate)
    if not a or not b:
        return False
    return bool(a & b)


def _fold_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    return folded


def _name_mentioned(name: str, haystack: str) -> bool:
    """Vrai si le nom complet (ou au moins le nom de famille) apparaît dans le texte plié."""
    folded_name = _fold_text(name).strip()
    if not folded_name:
        return False
    if folded_name in haystack:
        return True
    tokens = re.findall(r"[a-z0-9]{3,}", folded_name)
    if not tokens:
        return False
    # Nom de famille = dernier token significatif (ex. « Clara Montfort » → montfort).
    surname = tokens[-1]
    return bool(re.search(rf"\b{re.escape(surname)}\b", haystack))


def _surname(name: str) -> str:
    """Dernier token significatif d'un nom complet (ex. « Clara Montfort » → montfort)."""
    folded = _fold_text(name).strip()
    tokens = re.findall(r"[a-z0-9]{3,}", folded)
    return tokens[-1] if tokens else ""


def _same_surname(name_a: str, name_b: str) -> bool:
    a, b = _surname(name_a), _surname(name_b)
    return bool(a) and a == b


def _shared_affiliation_fact(name_a: str, name_b: str, facts: list[dict]) -> dict | None:
    """Cherche un fact 'relation' déjà existant qui cite les deux noms (ex. affiliation à
    une même organisation/entreprise/équipe) — signal qu'un lien professionnel/collectif
    est DÉJÀ posé par le dossier, à réutiliser plutôt qu'inventer un « aucun lien »."""
    for f in facts:
        if f.get("type") != "relation":
            continue
        content = _fold_text(str(f.get("content", "")))
        if _name_mentioned(name_a, content) and _name_mentioned(name_b, content):
            return f
    return None


def auto_patch_relations(raw: dict) -> dict:
    """Répare mécaniquement (sans appel LLM) le maillage relationnel entre suspects.

    Le validateur exige que chaque suspect ait un fact 'relation' nommant CHAQUE autre
    suspect — avec 7-8 suspects ça représente jusqu'à 56 mentions nommées à réussir d'un
    coup pour le LLM, une des causes les plus fréquentes de rejet/relance. C'est une règle
    purement mécanique (présence/absence d'un nom dans un texte) : on la corrige donc ici
    par du code plutôt que de renvoyer le dossier en correction au LLM.

    Le lien injecté n'est JAMAIS un « aucun lien / de vue » choisi au hasard s'il existe un
    indice contraire déjà posé par le dossier généré (aucune invention, uniquement des
    signaux déjà présents dans `raw`) :
    1. Même nom de famille → parenté probable à éclaircir.
    2. Un fact 'relation' existant cite déjà les deux noms (même organisation/affaire) →
       réutilise cette référence plutôt que de nier tout lien.
    3. Sinon seulement : simple connaissance / aucun lien particulier.
    """
    suspects: dict = raw.get("suspects", {})
    facts: list[dict] = raw.get("facts", [])
    facts_by_id = {f["id"]: f for f in facts}
    existing_ids = {f["id"] for f in facts}
    # Snapshot des facts D'ORIGINE (avant tout patch) pour la détection d'affiliation
    # partagée : les facts fourre-tout qu'on injecte ci-dessous listent volontairement
    # PLUSIEURS noms à la fois et ne doivent jamais être pris pour un signal d'affiliation
    # réelle pour un AUTRE suspect (sinon effet boule de neige/circulaire).
    original_relation_facts = [f for f in facts if f.get("type") == "relation"]

    for sid, s in suspects.items():
        known_relation_text = " ".join(
            _fold_text(str(facts_by_id[fid].get("content", "")))
            for fid in s.get("known_fact_ids", [])
            if fid in facts_by_id and facts_by_id[fid].get("type") == "relation"
        )
        name_a = str(s.get("name", ""))
        missing_sids = [
            other_sid for other_sid, other in suspects.items()
            if other_sid != sid
            and not _name_mentioned(str(other.get("name", "")), known_relation_text)
        ]
        if not missing_sids:
            continue

        lines = []
        for other_sid in missing_sids:
            name_b = str(suspects[other_sid].get("name", ""))
            if _same_surname(name_a, name_b):
                lines.append(
                    f"{name_a} et {name_b} portent le même nom de famille — un lien de "
                    "parenté est à éclaircir."
                )
                continue
            shared = _shared_affiliation_fact(name_a, name_b, original_relation_facts)
            if shared is not None:
                lines.append(
                    f"{name_a} et {name_b} sont tous deux liés à la même organisation/affaire "
                    f"mentionnée dans le dossier (cf. fait {shared['id']}) — connaissance "
                    "professionnelle probable."
                )
                continue
            lines.append(
                f"{name_a} et {name_b} n'ont pas de lien particulier connu : simple "
                "connaissance du jour, ou ne se connaissent que de vue."
            )

        suffix = 1
        new_id = f"AUTOFIX_REL_{sid}_{suffix}"
        while new_id in existing_ids:
            suffix += 1
            new_id = f"AUTOFIX_REL_{sid}_{suffix}"
        existing_ids.add(new_id)
        new_fact = {
            "id": new_id,
            "type": "relation",
            "content": " ".join(lines),
            "keywords": ["relation", "lien", "connaissance"],
        }
        facts.append(new_fact)
        facts_by_id[new_id] = new_fact
        s.setdefault("known_fact_ids", []).append(new_id)

    raw["facts"] = facts
    raw["suspects"] = suspects
    return raw


# Fenêtre max autour de time_of_death pour juger le coupable « capable ».
# Volontairement étroite : ±10 min suffit (présence au moment du crime), pas une heure.
_CAPABLE_TIME_WINDOW_MIN = 10


def _guilty_physically_capable(
    guilty_entries: list[dict],
    location: str,
    time_of_death: str,
) -> bool:
    """Le coupable a-t-il une entrée de timeline vraie le plaçant près des lieux/heure ?

    Exige overlap de lieu (champ location ou description) ET heure dans ±10 min.
    """
    for e in guilty_entries:
        entry_loc = str(e.get("location", ""))
        entry_desc = str(e.get("description", ""))
        loc_ok = (
            _locs_compatible(location, entry_loc)
            or _locs_compatible(location, entry_desc)
        )
        if loc_ok and _times_close(
            e.get("time"), time_of_death, window=_CAPABLE_TIME_WINDOW_MIN
        ):
            return True
    return False


def validate(raw: dict) -> list[str]:
    issues: list[str] = []

    suspects: dict = raw.get("suspects", {})
    facts: list[dict] = raw.get("facts", [])
    evidence: list[dict] = raw.get("evidence", [])
    timeline: list[dict] = raw.get("timeline", [])

    fact_ids = {f["id"] for f in facts}
    evidence_ids = {e["id"] for e in evidence}

    # --- Champs obligatoires non vides ---
    for key in (
        "title", "summary", "victim_name", "victim_description", "crime_description",
        "method", "weapon", "location", "time_of_death", "investigation_moment", "motive",
        "true_timeline_summary", "main_lies_summary",
    ):
        if not str(raw.get(key, "")).strip():
            issues.append(f"Champ obligatoire vide : {key}")

    # --- L'enquête doit explicitement se dérouler au moins le lendemain du crime ---
    investigation_moment = str(raw.get("investigation_moment", ""))
    if investigation_moment.strip():
        same_day_marker = bool(_normalize_tokens(investigation_moment) & _SAME_DAY_MARKERS)
        if same_day_marker or not _has_explicit_day_delay(investigation_moment):
            issues.append(
                "investigation_moment doit énoncer EXPLICITEMENT un délai d'au moins un jour "
                "après le crime (ex. 'le lendemain matin, 14h après les faits') — pas le jour "
                f"même ni une formulation vague : {investigation_moment!r}"
            )

    # --- Exactement un coupable, cohérent avec guilty_suspect_id ---
    guilty_flags = [sid for sid, s in suspects.items() if s.get("is_guilty")]
    if len(guilty_flags) != 1:
        issues.append(f"Il doit y avoir exactement un coupable, trouvé : {guilty_flags}")
    guilty_id = raw.get("guilty_suspect_id")
    if guilty_id not in suspects:
        issues.append(f"guilty_suspect_id '{guilty_id}' ne correspond à aucun suspect")
    elif guilty_flags and guilty_id not in guilty_flags:
        issues.append("guilty_suspect_id ne correspond pas au suspect marqué is_guilty=true")

    # --- Champs obligatoires non vides par suspect (identification sans portrait fiable) ---
    for sid, s in suspects.items():
        for key in ("name", "role", "personality", "alibi_summary"):
            if not str(s.get(key, "")).strip():
                issues.append(f"Suspect {sid} : champ obligatoire vide : {key}")
        role_tokens = _normalize_tokens(str(s.get("role", "")))
        leaked = role_tokens & _ROLE_SUSPICIOUS_ADJECTIVES
        if leaked:
            issues.append(
                f"Suspect {sid} : le rôle '{s.get('role')}' contient un adjectif suggestif "
                f"({', '.join(sorted(leaked))}) — doit rester factuel (métier/statut uniquement)"
            )

    # --- Références de facts/evidence valides ---
    for sid, s in suspects.items():
        known = set(s.get("known_fact_ids", []))
        secret = set(s.get("secret_fact_ids", []))
        for fid in known:
            if fid not in fact_ids:
                issues.append(f"Suspect {sid} : known_fact_ids référence un fact inconnu '{fid}'")
        for fid in secret:
            if fid not in fact_ids:
                issues.append(f"Suspect {sid} : secret_fact_ids référence un fact inconnu '{fid}'")
        if not secret.issubset(known):
            issues.append(f"Suspect {sid} : secret_fact_ids doit être un sous-ensemble de known_fact_ids")
        for lie in s.get("lies", []):
            if lie["fact_id"] not in fact_ids:
                issues.append(f"Suspect {sid} : mensonge référence un fact inconnu '{lie['fact_id']}'")

    # --- Confrontabilité : une preuve dont aucun fait n'est connu d'un suspect est
    # inutile en interrogatoire (personne ne peut confirmer/infirmer/mentir dessus).
    known_fact_ids_all: set[str] = set()
    for s in suspects.values():
        known_fact_ids_all.update(s.get("known_fact_ids", []))
    for e in evidence:
        related = e.get("related_fact_ids", [])
        for fid in related:
            if fid not in fact_ids:
                issues.append(f"Preuve {e['id']} référence un fact inconnu '{fid}'")
        if not related:
            issues.append(
                f"Preuve {e['id']} n'est reliée à aucun fact (related_fact_ids vide) — "
                "aucun suspect ne peut être confronté à son contenu"
            )
        elif not any(fid in known_fact_ids_all for fid in related):
            issues.append(
                f"Preuve {e['id']} : aucun de ses facts liés ({', '.join(related)}) n'est "
                "connu d'un suspect — personne ne peut confirmer/infirmer/mentir sur ce "
                "détail en interrogatoire, la preuve est inutile"
            )

    for fid in raw.get("key_evidence_ids", []):
        if fid not in evidence_ids:
            issues.append(f"key_evidence_ids référence une preuve inconnue '{fid}'")

    for t in timeline:
        actor = t.get("actor_suspect_id")
        if actor is not None and actor not in suspects:
            issues.append(f"Entrée timeline '{t.get('id')}' référence un suspect inconnu '{actor}'")

    # --- Chronologie physiquement cohérente ---
    by_actor: dict[str, list[dict]] = {}
    for t in timeline:
        actor = t.get("actor_suspect_id")
        if actor:
            by_actor.setdefault(actor, []).append(t)
    for actor, entries in by_actor.items():
        seen_by_time: dict[str, str] = {}
        for e in entries:
            time = e.get("time")
            loc = e.get("location")
            # Clé normalisée pour ne pas traiter 23h30 et 23:30 comme deux horaires distincts.
            tkey = _parse_minutes(time)
            key = str(tkey) if tkey is not None else str(time)
            if key in seen_by_time and seen_by_time[key] != loc:
                issues.append(
                    f"Suspect {actor} apparaît à deux endroits différents à {time} "
                    f"('{seen_by_time[key]}' et '{loc}')"
                )
            seen_by_time[key] = loc

    # --- Coupable physiquement capable (timeline = vérité, pas les alibis) ---
    if guilty_id in suspects:
        guilty_entries = by_actor.get(guilty_id, [])
        location = str(raw.get("location", "")).strip()
        time_of_death = str(raw.get("time_of_death", "")).strip()
        if not guilty_entries:
            issues.append("Le coupable n'apparaît dans aucune entrée de la timeline")
        elif not _guilty_physically_capable(guilty_entries, location, time_of_death):
            issues.append(
                "Le coupable n'a aucune entrée de timeline le plaçant près des lieux et de "
                "l'heure du crime (timeline = VRAIE chronologie : une entrée du coupable au "
                f"lieu du crime dans les ±{_CAPABLE_TIME_WINDOW_MIN} min de time_of_death, HH:MM)"
            )

    # --- Diversité des secrets / mobiles (ne pas désigner le coupable trop facilement) ---
    min_trait = config.min_suspects_with_trait(len(suspects))
    innocents_with_secret = sum(
        1 for sid, s in suspects.items() if sid != guilty_id and s.get("secret_fact_ids")
    )
    if innocents_with_secret < min_trait:
        issues.append(
            f"Pas assez de suspects innocents avec un secret ({innocents_with_secret} < "
            f"{min_trait}) — le coupable serait trop facile à repérer, pas assez de fausses "
            "pistes pour occuper les joueurs sur toute la durée"
        )
    with_mobile = sum(1 for s in suspects.values() if s.get("mobile"))
    if with_mobile < min_trait:
        issues.append(
            f"Pas assez de suspects avec un mobile apparent ({with_mobile} < {min_trait})"
        )

    # --- Pas de « titre-indice » structurel : le coupable ne doit pas être le seul suspect
    # rattaché (via role/mobile) à l'enjeu concret du mobile dominant (héritage, promotion...) —
    # sinon le coupable se devine d'un coup d'œil sur le roster, sans interroger personne.
    if guilty_id in suspects:
        guilty_stakes = _stake_tokens(suspects[guilty_id])
        for marker in guilty_stakes:
            holders = [sid for sid, s in suspects.items() if marker in _stake_tokens(s)]
            if len(holders) == 1:
                issues.append(
                    f"Le coupable ({guilty_id}) est le SEUL suspect dont le role/mobile évoque "
                    f"'{marker}' — ça le désigne d'un coup d'œil sans même jouer. Ajoute au moins "
                    "un autre suspect avec un intérêt comparable sur ce même enjeu."
                )

    # --- Volume de contenu jouable (proportionnel au nombre de suspects, cf. generator) ---
    min_facts = max(12, len(suspects) * 3)
    if len(facts) < min_facts:
        issues.append(f"Trop peu de facts générés ({len(facts)} < {min_facts})")

    # --- Liens inter-suspects : chaque suspect doit connaître son lien (ou l'absence de
    # lien) avec CHAQUE autre suspect — sinon interrogé sur « tu connais untel ? » il
    # reste vague faute d'info, même quand le lien est évident dans le casting.
    facts_by_id = {f["id"]: f for f in facts}
    for sid, s in suspects.items():
        known_relation_text = " ".join(
            _fold_text(str(facts_by_id[fid].get("content", "")))
            for fid in s.get("known_fact_ids", [])
            if fid in facts_by_id and facts_by_id[fid].get("type") == "relation"
        )
        missing = [
            other.get("name", other_sid)
            for other_sid, other in suspects.items()
            if other_sid != sid and not _name_mentioned(str(other.get("name", "")), known_relation_text)
        ]
        if missing:
            issues.append(
                f"Suspect {sid} ({s.get('name')}) : aucun fact 'relation' connu ne précise "
                f"son lien (ou l'absence de lien) avec : {', '.join(missing[:5])}"
                + ("…" if len(missing) > 5 else "")
            )
    # Au moins un fact relation par suspect (souvent 1 fact listant tous ses liens).
    relation_facts = sum(1 for f in facts if f.get("type") == "relation")
    if relation_facts < len(suspects):
        issues.append(
            f"Pas assez de facts de type 'relation' ({relation_facts} < {len(suspects)}) — "
            "chaque suspect doit avoir de quoi décrire ses liens avec les autres"
        )
    public_evidence = [e for e in evidence if e.get("is_public")]
    if len(public_evidence) < 2:
        issues.append(f"Pas assez de preuves publiques ({len(public_evidence)} < 2)")
    elif len(public_evidence) > 3:
        issues.append(
            f"Trop de preuves publiques dès le lancement ({len(public_evidence)} > 3) — "
            "repasse l'excédent en is_public=false, le reste se révèle en cours de partie"
        )

    # --- Aucune preuve ne désigne directement le coupable ---
    guilty_name = str(suspects.get(guilty_id, {}).get("name", "")).strip().lower()
    if guilty_name:
        for e in public_evidence:
            if guilty_name and guilty_name in str(e.get("description", "")).lower():
                issues.append(
                    f"La preuve publique '{e['id']}' cite nommément le coupable — trop direct"
                )

    # --- Nombre de suspects dans les bornes attendues, slots valides (pool de portraits) ---
    if not (config.MIN_SUSPECTS <= len(suspects) <= config.MAX_SUSPECTS):
        issues.append(
            f"Nombre de suspects hors bornes ({len(suspects)}, attendu entre "
            f"{config.MIN_SUSPECTS} et {config.MAX_SUSPECTS})"
        )
    invalid_slots = [s for s in suspects if s not in SUSPECT_SLOTS]
    if invalid_slots:
        issues.append(f"Slots suspects invalides (hors du pool de portraits) : {invalid_slots}")

    return issues

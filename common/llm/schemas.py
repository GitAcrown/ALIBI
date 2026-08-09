"""JSON schemas stricts (OpenAI Structured Outputs) pour le pipeline d'enquête.

Règles imposées par le mode strict d'OpenAI :
- `additionalProperties: false` partout ;
- `required` doit lister TOUTES les clés d'un objet (y compris les "optionnelles",
  rendues nullable via `anyOf: [schema, {"type": "null"}]`) ;
- pas de contraintes numériques de longueur (`minItems`/`maxItems`/`pattern`...) fiables
  en mode strict : les 12 suspects sont donc modélisés comme un OBJET à clés fixes
  (`p01`..`p12`), garanti complet par `required`, plutôt qu'un tableau de taille libre.
"""

SUSPECT_SLOTS = [f"p{n:02d}" for n in range(1, 13)]

FACT_TYPES = ["timeline", "alibi", "relation", "objet", "lieu", "autre"]
GENRES = ["masculin", "feminin", "agenre"]

_LIE_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_id": {"type": "string", "description": "Fait sur lequel porte le mensonge."},
        "lie_text": {"type": "string", "description": "Ce que le suspect affirme à la place de la vérité."},
    },
    "required": ["fact_id", "lie_text"],
    "additionalProperties": False,
}

_SUSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Prénom et nom complets du suspect."},
        "age": {"type": "integer"},
        "gender": {"type": "string", "enum": GENRES},
        "role": {
            "type": "string",
            "description": (
                "Courte étiquette d'identification FACTUELLE (2-5 mots) : métier, fonction ou "
                "statut objectif, ex. 'Barman de nuit', 'Journaliste au Courrier', 'Chauffeur "
                "de la famille'. INTERDIT tout adjectif psychologique ou de caractère "
                "('nerveux', 'ambitieuse', 'louche', 'sournois', 'inquiétant'...) : ça "
                "vendrait la mèche en suggérant qui est suspect/innocent avant même de jouer. "
                "Reste neutre et descriptif, comme une fiche d'état civil. Les portraits sont "
                "des pixel arts symboliques et NE représentent PAS fidèlement le suspect : "
                "cette étiquette (affichée avec le nom et l'âge) est le principal moyen pour "
                "les joueurs de reconnaître qui est qui. Doit être distinctive par rapport aux "
                "autres suspects."
            ),
        },
        "personality": {"type": "string", "description": "Personnalité et manière de parler, 1-2 phrases."},
        "alibi_summary": {"type": "string"},
        "mobile": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Mobile apparent, ou null si aucun.",
        },
        "is_guilty": {"type": "boolean"},
        "known_fact_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs des facts que ce suspect connaît et peut mentionner.",
        },
        "secret_fact_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Sous-ensemble de known_fact_ids que le suspect cache (secret ≠ culpabilité).",
        },
        "lies": {
            "type": "array",
            "items": _LIE_SCHEMA,
            "description": "Mensonges que ce suspect peut raconter sur certains facts.",
        },
    },
    "required": [
        "name", "age", "gender", "role", "personality", "alibi_summary",
        "mobile", "is_guilty", "known_fact_ids", "secret_fact_ids", "lies",
    ],
    "additionalProperties": False,
}

_TIMELINE_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "time": {"type": "string", "description": "Heure au format HH:MM."},
        "location": {"type": "string"},
        "actor_suspect_id": {
            "anyOf": [{"type": "string", "enum": SUSPECT_SLOTS}, {"type": "null"}],
            "description": "Slot suspect concerné (p01..p12), ou null si victime/tiers.",
        },
        "description": {"type": "string"},
    },
    "required": ["id", "time", "location", "actor_suspect_id", "description"],
    "additionalProperties": False,
}

_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Identifiant unique, ex. F001."},
        "type": {"type": "string", "enum": FACT_TYPES},
        "content": {"type": "string", "description": "Un fait atomique, vérifiable, une seule information."},
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-6 mots-clés (français, minuscules) pour retrouver ce fait par mot-clé.",
        },
    },
    "required": ["id", "type", "content", "keywords"],
    "additionalProperties": False,
}

_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Identifiant unique, ex. E01."},
        "description": {"type": "string"},
        "is_public": {"type": "boolean"},
        "related_fact_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "description", "is_public", "related_fact_ids"],
    "additionalProperties": False,
}

def build_case_schema(slots: list[str]) -> dict:
    """Construit le schéma strict pour UN sous-ensemble précis de slots suspects.

    Le mode strict d'OpenAI exige des clés fixes (`required` = TOUTES les clés d'un
    objet) : le nombre de suspects par enquête n'est donc pas une simple contrainte
    numérique, il faut regénérer le schéma pour le sous-ensemble de slots choisi
    (tiré aléatoirement parmi SUSPECT_SLOTS à chaque nouvelle enquête)."""
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {
                "type": "string",
                "description": "Résumé court (1-2 phrases) SANS révéler le coupable, pour un hall of fame.",
            },
            "victim_name": {"type": "string"},
            "victim_description": {"type": "string"},
            "crime_description": {"type": "string"},
            "method": {"type": "string"},
            "weapon": {"type": "string"},
            "location": {"type": "string"},
            "time_of_death": {"type": "string", "description": "Format HH:MM."},
            "investigation_moment": {
                "type": "string",
                "description": (
                    "Phrase EXPLICITE qui situe le moment des interrogatoires par rapport au "
                    "crime, ex. 'Le lendemain matin, vers 9h, soit environ 14 heures après les "
                    "faits.' ou 'Deux jours après le drame, alors que la nouvelle a fait le tour "
                    "du quartier.'. Doit toujours représenter un délai d'AU MOINS un jour après "
                    "time_of_death (jamais le jour même) : le joueur ne peut pas observer les "
                    "suspects sur place au moment des faits, seulement les interroger après coup."
                ),
            },
            "timeline": {"type": "array", "items": _TIMELINE_ENTRY_SCHEMA},
            "facts": {"type": "array", "items": _FACT_SCHEMA},
            "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
            "suspects": {
                "type": "object",
                "properties": {slot: _SUSPECT_SCHEMA for slot in slots},
                "required": list(slots),
                "additionalProperties": False,
            },
            "guilty_suspect_id": {"type": "string", "enum": list(slots)},
            "motive": {"type": "string"},
            "true_timeline_summary": {
                "type": "string",
                "description": "Résumé factuel de ce qui s'est réellement passé, pour la résolution finale.",
            },
            "key_evidence_ids": {"type": "array", "items": {"type": "string"}},
            "main_lies_summary": {
                "type": "string",
                "description": "Résumé des principaux mensonges racontés par les suspects, pour la résolution.",
            },
        },
        "required": [
            "title", "summary", "victim_name", "victim_description", "crime_description",
            "method", "weapon", "location", "time_of_death", "investigation_moment", "timeline",
            "facts", "evidence", "suspects", "guilty_suspect_id", "motive",
            "true_timeline_summary", "key_evidence_ids", "main_lies_summary",
        ],
        "additionalProperties": False,
    }


# Schéma "plein" (12 slots) — utilisé nulle part par défaut désormais (chaque partie
# construit son schéma via `build_case_schema(slots)`), gardé pour compat/tests éventuels.
CASE_SCHEMA = build_case_schema(SUSPECT_SLOTS)

# Chaque point du contrôle de l'auditeur (cf. ScenarioAuditor.SYSTEM_PROMPT) doit être
# explicitement statué OK/KO avec une justification courte — pas seulement un verdict
# global. Ça force le LLM à vraiment vérifier chaque axe plutôt que de répondre en bloc,
# et ça rend les logs de génération lisibles (on voit exactement quel point a échoué).
_CHECK_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "note": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Justification courte (obligatoire si ok=false, sinon null).",
        },
    },
    "required": ["ok", "note"],
    "additionalProperties": False,
}

AUDIT_CHECKLIST_ITEMS = (
    "coherence_chronologie",
    "coupable_capable",
    "pas_trop_facile",
    "pas_trop_dur",
    "casting_fonctionnel",
    "clarte_narrative",
    "coherence_lieu_personnages",
    "coherence_epoque",
    "affiliations_explicites",
)

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "checklist": {
            "type": "object",
            "properties": {name: _CHECK_ITEM_SCHEMA for name in AUDIT_CHECKLIST_ITEMS},
            "required": list(AUDIT_CHECKLIST_ITEMS),
            "additionalProperties": False,
            "description": "Un statut OK/KO par point de contrôle, à évaluer individuellement.",
        },
        "valid": {
            "type": "boolean",
            "description": "false si au moins un point de la checklist est ok=false.",
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Liste des incohérences détectées (vide si valid=true) — reprend les notes des points KO.",
        },
    },
    "required": ["checklist", "valid", "issues"],
    "additionalProperties": False,
}

SUSPECT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reponse": {"type": "string", "description": "Réponse du suspect, courte et naturelle, à la première personne."},
        "fact_ids_utilises": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs des facts réellement utilisés pour construire la réponse (vide si évasif).",
        },
    },
    "required": ["reponse", "fact_ids_utilises"],
    "additionalProperties": False,
}

RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "monologue": {
            "type": "string",
            "description": "Monologue de résolution façon film noir, concis et dramatique, en français.",
        },
    },
    "required": ["monologue"],
    "additionalProperties": False,
}

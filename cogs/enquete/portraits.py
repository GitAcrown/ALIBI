"""Gestion de assets/portraits_data.json + upload des portraits en emojis d'application."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional, Union

import discord

from . import config
from common.llm.schemas import SUSPECT_SLOTS

logger = logging.getLogger("enquete.portraits")

_IMG_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_EMOJI_RE = re.compile(r"<a?:\w+:(\d+)>")
_slot_files_cache: Optional[dict[str, Path]] = None


def load_portraits_data() -> dict:
    path = Path(config.PORTRAITS_DATA_PATH)
    if not path.exists():
        return {slot: {"emoji": "", "genre": "agenre", "style": ""} for slot in SUSPECT_SLOTS}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for slot in SUSPECT_SLOTS:
        data.setdefault(slot, {"emoji": "", "genre": "agenre", "style": ""})
    return data


def save_portraits_data(data: dict) -> None:
    global _slot_files_cache
    path = Path(config.PORTRAITS_DATA_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    _slot_files_cache = None


def _numeric_key(path: Path) -> tuple:
    m = re.search(r"(\d+)", path.stem)
    return (int(m.group(1)) if m else 0, path.stem)


def _resolve_slot_files(portraits_dir: Path) -> dict[str, Path]:
    """Associe chaque slot p01..p12 à un fichier image.

    Priorité aux fichiers déjà nommés p01.*..p12.* ; à défaut, retombe sur les fichiers
    présents (ex. silhd1.png..silhd12.png) triés numériquement et mappés dans l'ordre.
    """
    if not portraits_dir.is_dir():
        return {}

    all_files = [p for p in portraits_dir.iterdir() if p.suffix.lower() in _IMG_EXTENSIONS]
    by_slot: dict[str, Path] = {}
    for slot in SUSPECT_SLOTS:
        matches = [p for p in all_files if p.stem.lower() == slot]
        if matches:
            by_slot[slot] = matches[0]

    if by_slot:
        return by_slot

    remaining = sorted(all_files, key=_numeric_key)
    for slot, path in zip(SUSPECT_SLOTS, remaining):
        by_slot[slot] = path
    return by_slot


def portrait_path(slot: str) -> Optional[Path]:
    """Chemin local du portrait pixel art pour un slot (p01..p12), ou None."""
    global _slot_files_cache
    if _slot_files_cache is None:
        _slot_files_cache = _resolve_slot_files(Path(config.PORTRAITS_DIR))
    path = _slot_files_cache.get(slot)
    if path is None or not path.is_file():
        return None
    return path


def portrait_file(slot: str) -> Optional[discord.File]:
    """`discord.File` prêt à joindre (thumbnail LayoutView), ou None si pas d'asset."""
    path = portrait_path(slot)
    if path is None:
        return None
    return discord.File(path, filename=f"portrait_{slot}{path.suffix.lower()}")


def emoji_cdn_url(emoji_str: str, *, size: int = 256) -> Optional[str]:
    """URL CDN d'un emoji custom `<:name:id>` — secours si le fichier local manque."""
    m = _EMOJI_RE.fullmatch((emoji_str or "").strip())
    if not m:
        return None
    return f"https://cdn.discordapp.com/emojis/{m.group(1)}.png?size={size}&quality=lossless"


def portrait_thumbnail_media(
    slot: str, portraits_meta: Optional[dict] = None
) -> tuple[Optional[discord.File], Optional[Union[discord.File, str]]]:
    """Prépare le média thumbnail d'un suspect.

    Renvoie `(file_à_joindre, media_pour_Thumbnail)` :
    - fichier local en priorité (image pleine résolution) ;
    - sinon URL CDN de l'emoji application ;
    - sinon `(None, None)`.
    """
    file = portrait_file(slot)
    if file is not None:
        return file, file
    meta = (portraits_meta or {}).get(slot) or {}
    url = emoji_cdn_url(meta.get("emoji") or "")
    return None, url


async def setup_portrait_emojis(bot: discord.Client) -> tuple[list[str], list[str]]:
    """Upload les 12 portraits comme emojis d'application et met à jour portraits_data.json.

    Renvoie (succès, erreurs) — des messages lisibles pour l'admin qui a lancé la commande.
    """
    portraits_dir = Path(config.PORTRAITS_DIR)
    slot_files = _resolve_slot_files(portraits_dir)
    data = load_portraits_data()

    successes: list[str] = []
    errors: list[str] = []

    missing = [slot for slot in SUSPECT_SLOTS if slot not in slot_files]
    if missing:
        errors.append(
            f"Fichiers introuvables pour : {', '.join(missing)} (dans `{portraits_dir}/`)"
        )

    existing_emojis = {e.name: e for e in await bot.fetch_application_emojis()}

    for slot, path in slot_files.items():
        emoji_name = f"alibi_{slot}"
        legacy_name = f"redacted_{slot}"
        try:
            if emoji_name in existing_emojis:
                emoji = existing_emojis[emoji_name]
            elif legacy_name in existing_emojis:
                # Ancien préfixe (bot encore nommé REDACTED) — réutilise sans re-upload.
                emoji = existing_emojis[legacy_name]
            else:
                with path.open("rb") as f:
                    image_bytes = f.read()
                emoji = await bot.create_application_emoji(name=emoji_name, image=image_bytes)
            data[slot]["emoji"] = str(emoji)
            successes.append(f"{slot} → {emoji}")
        except discord.HTTPException as e:
            errors.append(f"{slot} ({path.name}) : échec upload — {e}")
        except OSError as e:
            errors.append(f"{slot} ({path.name}) : lecture fichier impossible — {e}")

    save_portraits_data(data)
    return successes, errors

"""Helpers LayoutView / Components v2 partagés."""

from __future__ import annotations

from typing import Optional

import discord


def append_controls(
    children: list,
    *,
    note: str = "",
    button_row: Optional[discord.ui.ActionRow] = None,
    select_row: Optional[discord.ui.ActionRow] = None,
) -> None:
    """Pied de vue : note optionnelle, puis boutons, puis select."""
    text = (note or "").strip()
    if text:
        if not text.startswith("-#"):
            text = f"-# {text}"
        children += [discord.ui.Separator(), discord.ui.TextDisplay(text)]
    if button_row is not None or select_row is not None:
        children.append(discord.ui.Separator())
        if button_row is not None:
            children.append(button_row)
        if select_row is not None:
            if button_row is not None:
                children.append(discord.ui.Separator())
            children.append(select_row)


def make_container(*children, accent: Optional[int] = None, spoiler: bool = False) -> discord.ui.Container:
    kwargs = {}
    if accent is not None:
        kwargs["accent_color"] = accent
    if spoiler:
        kwargs["spoiler"] = True
    return discord.ui.Container(*children, **kwargs)

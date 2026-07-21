"""Provider contract shared by every platform.

A provider knows three things and nothing about the UI:
  * how to authorise (interactively, once) and refresh its own token,
  * how to fetch a fresh `Metrics` snapshot,
  * its display name and accent colour.

The widget never imports a concrete platform — it only ever sees `Provider`
and `Metrics`. Adding a new platform means dropping one file in this folder.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Metrics:
    """One snapshot from a single platform.

    `ok` is False when the value could not be refreshed (disabled, not
    authorised, network/API error); the widget then shows a dash and keeps the
    platform out of the totals.

    `followers` may be None for a row that deliberately reports no follower
    count (e.g. a second row of the same community, which would otherwise be
    counted twice in the tray total); the widget shows a dash.
    """
    followers: Optional[int] = 0
    views: int = 0
    likes: Optional[int] = None
    ok: bool = True
    error: str = ""


class Provider(ABC):
    name: str = ""           # stable id: token file name, settings key
    label: str = ""          # shown in the tray menu / popup
    default_color = (200, 200, 200)

    # Name of the provider whose followers cell this row shares — for another
    # row of the same community (its Metrics.followers stays None so the tray
    # total counts the members once). The popup stretches the partner's cell
    # over the whole contiguous run of sharing rows.
    followers_span_with: str = ""

    # Name of the provider this row can be folded into for display — e.g. VK
    # Clips into VK Video. When the user turns the merge on, this row is not
    # drawn and its numbers are added to the target row's. Totals are unchanged
    # either way (both rows are always polled and always counted).
    merge_into: str = ""

    def __init__(self, config: dict, tokens, on_config_change=None):
        # `config` is a live reference into settings["providers"][name]; mutating
        # it and calling save_settings() persists toggles made from the menu.
        self.config = config if config is not None else {}
        self.tokens = tokens
        self.color = tuple(self.config.get("color", self.default_color))
        self._on_config_change = on_config_change

    def save_config(self) -> None:
        """Persist changes a provider made to its own `config` (e.g. burning a
        one-shot setup token). No-op when the owner wired no saver."""
        if self._on_config_change:
            self._on_config_change()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @abstractmethod
    def ensure_auth(self) -> bool:
        """Make sure we hold a usable token, authorising interactively if needed.

        Runs on a worker thread (it may block on a browser round-trip), never on
        the UI thread. Returns True when authorised.
        """

    @abstractmethod
    def fetch(self) -> Metrics:
        """Return a fresh snapshot. May raise — the widget guards every call."""

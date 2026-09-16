"""Provider contract shared by every platform.

A provider knows three things and nothing about the UI:
  * how to authorise (interactively, once) and refresh its own token,
  * how to fetch a fresh `Metrics` snapshot,
  * its display name and accent colour.

The widget never imports a concrete platform — it only ever sees `Provider`
and `Metrics`. Adding a new platform means dropping one file in this folder.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

# Consecutive walks that must agree on "no fewer items, fewer views" before
# the drop is believed — see stale_strike().
STALE_STRIKES = 2


@dataclass
class Metrics:
    """One snapshot from a single platform.

    `ok` is False when the value could not be refreshed (disabled, not
    authorised, network/API error); the widget then shows a dash and keeps the
    platform out of the totals.

    `followers` may be None for a row that deliberately reports no follower
    count (e.g. a second row of the same community, which would otherwise be
    counted twice in the tray total); the widget shows a dash.

    `likes` and `comments` are None when the row can't report them — the walk
    that sums them is off or has never succeeded — so the popup shows a dash
    rather than a plausible zero.
    """
    followers: Optional[int] = 0
    views: int = 0
    likes: Optional[int] = None
    comments: Optional[int] = None
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


def stale_strike(log: logging.Logger, extra: dict, label: str,
                 n: int, views: int) -> int:
    """Stale-read guard for a views total summed over a walk of items.

    Platforms serve per-item counters from eventually consistent replicas. VK
    now and then answers a whole walk with old, much lower numbers (seen live:
    the same 17 videos summing 26,293 one call, 98,360 the next); YouTube's
    per-video viewCount jitters by a few views between calls seconds apart
    (804, 801, 804...), so a quiet-night walk can land a handful below the
    previous one. A summed total rarely falls for real without an item
    disappearing — which also lowers the item count — so "no fewer items,
    fewer views" is stale data... unless a delete-and-repost hid inside one
    refresh window, or the platform corrected a counter (YouTube strips views
    it deems invalid): same count, views down for good. A stale VK replica is
    gone by the next scheduled walk, a deletion is not, so the drop is
    believed once STALE_STRIKES consecutive walks show it. YouTube's jitter is
    an independent draw per read, so there a low read CAN repeat and is then
    accepted as real: the hold removes the single low read, the common case,
    and the residue is a few views that the next growth overtakes — a bounded
    miss, chosen over a guard that could sit on a real drop for good. Likes
    and comments are not guarded (likes fall for real on an un-like, comments
    when one is deleted); a held pass keeps the cached likes and comments
    too, since all three sums come from one walk.

    `extra` is the provider's cached walk state: views_total and items_count
    as written by its last accepted walk, stale_strikes as left by the last
    held one (0 after an accepted walk). Returns the strike number to record
    when this walk must be HELD: the caller keeps its cached totals and
    stamps the pass as done (or the walk would re-run on every poll).
    Returns 0 when the walk is to be accepted; the caller then records
    views_total, items_count and stale_strikes=0 together.
    """
    prev_n = extra.get("items_count")
    if prev_n is None or "views_total" not in extra:
        return 0                       # first guarded walk: nothing to compare
    prev_views = int(extra["views_total"])
    if n < int(prev_n) or views >= prev_views:
        return 0
    strikes = int(extra.get("stale_strikes", 0)) + 1
    if strikes < STALE_STRIKES:
        log.warning("%s: walk returned %d items but views fell %d -> %d; "
                    "stale replica (%d/%d), keeping cached totals",
                    label, n, prev_views, views, strikes, STALE_STRIKES)
        return strikes
    log.warning("%s: views still %d -> %d over %d items on walk %d; "
                "a real drop, accepting", label, prev_views, views, n, strikes)
    return 0

"""Baselines for the popup's deltas.

A baseline is what a counter read when the user last acknowledged it by clicking
the popup. The delta shown next to each value is `current - baseline`, so it
keeps accumulating across polls until the next click — and across restarts,
which is the whole reason it lives on disk: close the widget for a day, reopen
it, and the delta still says how much came in while you were away.

Shape, one entry per provider, mirroring `Metrics`:

    {"tiktok": {"followers": 190, "views": 10096, "likes": 668}, ...}

A metric missing from the file has no baseline yet and shows no delta; the next
poll seeds it. Loss of this file is harmless — every counter just re-seeds from
the numbers as they stand.
"""

from __future__ import annotations

import json
import os

from .settings import DIR

STATE_FILE = os.path.join(DIR, "state.json")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

"""Per-provider token storage.

One JSON file per platform under `social_widget/tokens/`. `extra` carries
provider-specific cached ids (e.g. the resolved Instagram business-account id)
so we don't rediscover them on every poll.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional


class TokenStore:
    def __init__(self, path: str):
        self.path = path
        self.access_token:  Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expiry: float = 0.0
        self.extra: dict = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.access_token  = d.get("access_token")
            self.refresh_token = d.get("refresh_token")
            self.expiry        = d.get("expiry", 0)
            self.extra         = d.get("extra", {}) or {}
        except Exception:
            pass

    def reload(self) -> None:
        """Re-read from disk — for state another process writes (e.g. the
        `signed_in` marker telegram_login.py stamps)."""
        self._load()

    def _write(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "access_token":  self.access_token,
                    "refresh_token": self.refresh_token,
                    "expiry":        self.expiry,
                    "extra":         self.extra,
                }, f)
        except Exception:
            pass

    def save(self, access_token: str, refresh_token: Optional[str],
             expires_in: int, extra: Optional[dict] = None) -> None:
        self.access_token = access_token
        if refresh_token:                       # keep the old one if a refresh
            self.refresh_token = refresh_token  # response omits it (TikTok/Google)
        self.expiry = time.time() + expires_in - 120
        if extra is not None:
            self.extra = extra
        self._write()

    def set_extra(self, key: str, value) -> None:
        self.extra[key] = value
        self._write()

    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expiry

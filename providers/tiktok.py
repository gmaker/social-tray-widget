"""TikTok provider — OAuth PKCE, follower/likes from user/info, views summed
over video/list. Ported from the original single-file widget."""

from __future__ import annotations

import hashlib
import logging
import random
import string
import urllib.parse

import requests

from .base import Metrics, Provider
from ..oauth import LoopbackCapture

log = logging.getLogger("social.tiktok")

_AUTH      = "https://www.tiktok.com/v2/auth/authorize/"
_TOKEN     = "https://open.tiktokapis.com/v2/oauth/token/"
_USERINFO  = "https://open.tiktokapis.com/v2/user/info/"
_VIDEOLIST = "https://open.tiktokapis.com/v2/video/list/"
_SCOPES    = "user.info.basic,user.info.profile,user.info.stats,video.list"


class TikTokProvider(Provider):
    name          = "tiktok"
    label         = "TikTok"
    default_color = (254, 44, 85)

    def _client(self):
        return self.config.get("client_key", ""), self.config.get("client_secret", "")

    def _redirect(self):
        return self.config.get("redirect_uri", "http://localhost:8080/callback")

    # ── auth ────────────────────────────────────────────────────────────────
    def ensure_auth(self) -> bool:
        if self.tokens.is_valid():
            return True
        if self._refresh():
            return True
        return self._interactive_auth()

    def _refresh(self) -> bool:
        if not self.tokens.refresh_token:
            return False
        ck, cs = self._client()
        try:
            r = requests.post(
                _TOKEN,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"client_key": ck, "client_secret": cs,
                      "grant_type": "refresh_token",
                      "refresh_token": self.tokens.refresh_token},
                timeout=20,
            )
            if r.ok:
                d = r.json()
                self.tokens.save(d["access_token"],
                                 d.get("refresh_token", self.tokens.refresh_token),
                                 d.get("expires_in", 86400))
                return True
            log.error("tiktok refresh HTTP %s: %s", r.status_code, r.text[:500])
        except Exception:
            log.exception("tiktok refresh failed")
        return False

    def _interactive_auth(self) -> bool:
        ck, cs = self._client()
        if not ck:
            log.error("tiktok: client_key not set in settings")
            return False
        redirect  = self._redirect()
        chars     = string.ascii_letters + string.digits + "-._~"
        verifier  = "".join(random.choice(chars) for _ in range(64))
        challenge = hashlib.sha256(verifier.encode()).hexdigest()
        state     = "".join(random.choice(chars) for _ in range(32))
        url = _AUTH + "?" + urllib.parse.urlencode({
            "client_key": ck, "scope": _SCOPES, "response_type": "code",
            "redirect_uri": redirect, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        params = LoopbackCapture(redirect).capture(url)
        if params.get("state") != state or "code" not in params:
            log.error("tiktok auth failed: %s", params)
            return False
        try:
            r = requests.post(
                _TOKEN,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"client_key": ck, "client_secret": cs,
                      "code": params["code"], "grant_type": "authorization_code",
                      "redirect_uri": redirect, "code_verifier": verifier},
                timeout=20,
            )
            if r.ok:
                d = r.json()
                self.tokens.save(d["access_token"], d.get("refresh_token", ""),
                                 d.get("expires_in", 86400))
                return True
            log.error("tiktok token exchange HTTP %s: %s", r.status_code, r.text[:500])
        except Exception:
            log.exception("tiktok token exchange failed")
        return False

    # ── fetch ───────────────────────────────────────────────────────────────
    def fetch(self) -> Metrics:
        if not self.tokens.is_valid() and not self._refresh():
            if not self._interactive_auth():
                return Metrics(ok=False, error="not authorised")

        r = requests.get(
            _USERINFO,
            params={"fields": "display_name,follower_count,likes_count"},
            headers={"Authorization": f"Bearer {self.tokens.access_token}"},
            timeout=20,
        )
        r.raise_for_status()
        user = r.json().get("data", {}).get("user", {})
        followers = int(user.get("follower_count", 0))
        likes     = int(user.get("likes_count", 0))

        views = self._fetch_views() if self.config.get("count_views", True) else 0
        return Metrics(followers=followers, views=views, likes=likes)

    def _fetch_views(self) -> int:
        total, cursor, has_more = 0, 0, True
        while has_more:
            r = requests.post(
                _VIDEOLIST,
                params={"fields": "id,view_count"},
                headers={"Authorization": f"Bearer {self.tokens.access_token}",
                         "Content-Type": "application/json"},
                json={"max_count": 20, "cursor": cursor},
                timeout=20,
            )
            if not r.ok:
                log.error("tiktok video/list HTTP %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()
            data = r.json().get("data", {})
            for v in data.get("videos", []):
                total += int(v.get("view_count", 0))
            has_more = bool(data.get("has_more", False))
            cursor   = int(data.get("cursor", 0))
        return total

"""YouTube provider — Google OAuth2. One channels.list call yields both the
subscriber count and the channel's total view count; likes cost more.

Setup: a Google Cloud project with the *YouTube Data API v3* enabled and an
OAuth client of type "Web application" whose authorised redirect URI exactly
matches `redirect_uri` below (default http://localhost:8080/callback). Publish
the consent screen — while it is in "Testing" mode Google expires refresh tokens
after 7 days and every poll then dies with `invalid_grant`.

Note: YouTube rounds the public subscriber count to 3 significant figures; the
API cannot return the exact number even to the channel owner.

Quota, the reason likes are cached: the daily budget is 10,000 units.
channels.list is 1 unit, so polling followers/views every minute costs 1,440 a
day — 14%. Likes have no channel-level total: they mean walking the uploads
playlist and reading statistics 50 videos at a time, which is
2 x ceil(videos / 50) units a pass. At 133 videos that is 6 units — 10,080 a day
once a minute, i.e. over budget on its own. Once every `likes_refresh_min`
minutes it rounds to a fifth of the quota with room for the channel to grow.

Config (settings.json -> providers.youtube):
    "client_id" / "client_secret": required
    "count_likes":       default true; false skips the uploads walk entirely
    "likes_refresh_min": default 15
"""

from __future__ import annotations

import logging
import time
import urllib.parse

import requests

from .base import Metrics, Provider
from ..oauth import LoopbackCapture

log = logging.getLogger("social.youtube")

_AUTH      = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN     = "https://oauth2.googleapis.com/token"
_CHANNELS  = "https://www.googleapis.com/youtube/v3/channels"
_PLAYLIST  = "https://www.googleapis.com/youtube/v3/playlistItems"
_VIDEOS    = "https://www.googleapis.com/youtube/v3/videos"
_SCOPE     = "https://www.googleapis.com/auth/youtube.readonly"

_PER_PAGE = 50    # playlistItems page size and the videos.list id cap


class YouTubeProvider(Provider):
    name          = "youtube"
    label         = "YouTube"
    default_color = (255, 0, 0)

    def _client(self):
        return self.config.get("client_id", ""), self.config.get("client_secret", "")

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
        ci, cs = self._client()
        try:
            r = requests.post(_TOKEN, data={
                "client_id": ci, "client_secret": cs,
                "grant_type": "refresh_token",
                "refresh_token": self.tokens.refresh_token,
            }, timeout=20)
            if r.ok:
                d = r.json()
                # Google's refresh response never includes a new refresh_token.
                self.tokens.save(d["access_token"], self.tokens.refresh_token,
                                 d.get("expires_in", 3600))
                return True
            log.error("youtube refresh HTTP %s: %s", r.status_code, r.text[:500])
        except Exception:
            log.exception("youtube refresh failed")
        return False

    def _interactive_auth(self) -> bool:
        ci, cs = self._client()
        if not ci:
            log.error("youtube: client_id not set in settings")
            return False
        redirect = self._redirect()
        url = _AUTH + "?" + urllib.parse.urlencode({
            "client_id": ci, "redirect_uri": redirect, "response_type": "code",
            "scope": _SCOPE, "access_type": "offline", "prompt": "consent",
        })
        params = LoopbackCapture(redirect).capture(url)
        if "code" not in params:
            log.error("youtube auth failed: %s", params)
            return False
        try:
            r = requests.post(_TOKEN, data={
                "client_id": ci, "client_secret": cs, "code": params["code"],
                "grant_type": "authorization_code", "redirect_uri": redirect,
            }, timeout=20)
            if r.ok:
                d = r.json()
                self.tokens.save(d["access_token"], d.get("refresh_token", ""),
                                 d.get("expires_in", 3600))
                return True
            log.error("youtube token exchange HTTP %s: %s", r.status_code, r.text[:500])
        except Exception:
            log.exception("youtube token exchange failed")
        return False

    # ── fetch ───────────────────────────────────────────────────────────────
    def fetch(self) -> Metrics:
        if not self.tokens.is_valid() and not self._refresh():
            if not self._interactive_auth():
                return Metrics(ok=False, error="not authorised")

        r = requests.get(
            _CHANNELS,
            # contentDetails rides along for free and carries the uploads
            # playlist id that the likes walk starts from.
            params={"part": "statistics,contentDetails", "mine": "true"},
            headers=self._auth(),
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return Metrics(ok=False, error="no channel for this account")
        st = items[0].get("statistics", {})
        uploads = ((items[0].get("contentDetails") or {})
                   .get("relatedPlaylists") or {}).get("uploads", "")
        return Metrics(followers=int(st.get("subscriberCount", 0)),
                       views=int(st.get("viewCount", 0)),
                       likes=self._likes(uploads))

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.tokens.access_token}"}

    # ── likes ───────────────────────────────────────────────────────────────
    def _likes(self, uploads: str):
        """Likes summed over every upload, refreshed at most every
        `likes_refresh_min` minutes and cached in the token file between runs.

        Returns None when the number isn't available, which the widget shows as
        a dash rather than a misleading zero.
        """
        if not self.config.get("count_likes", True) or not uploads:
            return None
        cached = self.tokens.extra.get("likes_total")
        every  = int(self.config.get("likes_refresh_min", 15)) * 60
        if cached is not None:
            if time.time() < self.tokens.extra.get("likes_at", 0) + every:
                return int(cached)
        try:
            total = self._sum_likes(uploads)
        except Exception:
            # Likes are the extra; never let them cost us subs and views.
            log.exception("youtube: likes walk failed, reusing cached value")
            return None if cached is None else int(cached)
        self.tokens.set_extra("likes_total", total)
        self.tokens.set_extra("likes_at", time.time())
        return total

    def _sum_likes(self, uploads: str) -> int:
        ids   = self._upload_ids(uploads)
        total = 0
        for i in range(0, len(ids), _PER_PAGE):
            r = requests.get(_VIDEOS, params={
                "part": "statistics", "id": ",".join(ids[i:i + _PER_PAGE]),
                "maxResults": _PER_PAGE,
            }, headers=self._auth(), timeout=20)
            r.raise_for_status()
            for item in r.json().get("items", []):
                # likeCount is absent when the uploader hides it.
                total += int((item.get("statistics") or {}).get("likeCount") or 0)
        return total

    def _upload_ids(self, uploads: str) -> list:
        ids, page = [], None
        while True:
            params = {"part": "contentDetails", "playlistId": uploads,
                      "maxResults": _PER_PAGE}
            if page:
                params["pageToken"] = page
            r = requests.get(_PLAYLIST, params=params, headers=self._auth(),
                             timeout=20)
            r.raise_for_status()
            body = r.json()
            ids += [it["contentDetails"]["videoId"]
                    for it in body.get("items", [])
                    if (it.get("contentDetails") or {}).get("videoId")]
            page = body.get("nextPageToken")
            if not page:
                return ids

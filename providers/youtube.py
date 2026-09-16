"""YouTube provider — Google OAuth2. One channels.list call yields the
subscriber count; views, likes and comments are summed over every upload.

Setup: a Google Cloud project with the *YouTube Data API v3* enabled and an
OAuth client of type "Web application" whose authorised redirect URI exactly
matches `redirect_uri` below (default http://localhost:8080/callback). Publish
the consent screen — while it is in "Testing" mode Google expires refresh tokens
after 7 days and every poll then dies with `invalid_grant`.

Note: YouTube rounds the public subscriber count to 3 significant figures; the
API cannot return the exact number even to the channel owner.

Views deliberately do NOT come from the channel's statistics.viewCount. That
is an aggregate YouTube recomputes with hours of lag (and, for Shorts, by a
stricter "engaged views" method), so a fresh Short sitting at a thousand
views shows up there a day later — if fully at all. Per-video counts are
current, so views ride the same uploads walk as likes: statistics carries
viewCount, likeCount and commentCount, so the other two numbers are free.
The trade-off is that views of since-deleted videos, which the channel
aggregate keeps for ever, drop out of the sum. The channel counter is still used as a fallback
when the walk is off or has never succeeded.

Per-video counts are current but not consistent: the same video answers 804
views one call and 801 the next, seconds apart, from whichever replica the
request lands on. Summed over a whole channel, that lands a walk a few views
below the previous one whenever real growth is slower than the jitter (a
quiet night), and the popup shows a small minus. A walk that returns no fewer
videos than the last one but fewer views is therefore held for a pass and
believed only when the next walk repeats it (base.stale_strike). That
catches the single low read; two in a row still show, briefly, until growth
overtakes them.

Quota, the reason the walk is cached: the daily budget is 10,000 units.
channels.list is 1 unit, so polling followers every minute costs 1,440 a day —
14%. The walk reads the uploads playlist and then statistics 50 videos at a
time, which is 2 x ceil(videos / 50) units a pass. At 133 videos that is
6 units — 10,080 a day once a minute, i.e. over budget on its own. Once every
`likes_refresh_min` minutes it rounds to a fifth of the quota with room for
the channel to grow.

Config (settings.json -> providers.youtube):
    "client_id" / "client_secret": required
    "count_likes":       default true; false skips the uploads walk entirely
                         (likes and comments go dash, views fall back to the
                         channel counter)
    "likes_refresh_min": default 15 — minutes between walk passes
"""

from __future__ import annotations

import logging
import time
import urllib.parse

import requests

from .base import Metrics, Provider, stale_strike
from ..oauth import LoopbackCapture

log = logging.getLogger("social.youtube")

_AUTH      = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN     = "https://oauth2.googleapis.com/token"
_CHANNELS  = "https://www.googleapis.com/youtube/v3/channels"
_PLAYLIST  = "https://www.googleapis.com/youtube/v3/playlistItems"
_VIDEOS    = "https://www.googleapis.com/youtube/v3/videos"
_SCOPE     = "https://www.googleapis.com/auth/youtube.readonly"

_PER_PAGE = 50    # playlistItems page size and the videos.list id cap

# The walk's sums as cached in the token file, in (views, likes, comments) order.
_CACHE_KEYS = ("views_total", "likes_total", "comments_total")


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
        if not r.ok:
            # The body names the reason (quotaExceeded / accessNotConfigured /
            # forbidden); HTTPError's message alone is just "403 Forbidden".
            log.error("youtube channels.list HTTP %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return Metrics(ok=False, error="no channel for this account")
        self._track_channel(str(items[0].get("id") or ""))
        st = items[0].get("statistics", {})
        uploads = ((items[0].get("contentDetails") or {})
                   .get("relatedPlaylists") or {}).get("uploads", "")
        views, likes, comments = self._walk_totals(
            uploads, int(st.get("viewCount", 0)))
        return Metrics(followers=int(st.get("subscriberCount", 0)),
                       views=views, likes=likes, comments=comments)

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.tokens.access_token}"}

    def _track_channel(self, cid: str) -> None:
        """Record the channel behind the token and, when it CHANGES (a re-auth
        into another Google account), drop the previous channel's cached walk.
        The token file survives a re-auth with its `extra` intact, so without
        this the old channel's totals would be served for the rest of the
        window and then defended by the stale-read guard against the new
        channel's first walk as a "drop". A first sighting only records the
        id and trusts an existing cache to be this channel's, so a switch
        made before this build costs at most one extra window, once.
        Mirrors vk._VKBase._track_group."""
        extra = self.tokens.extra
        prev  = str(extra.get("channel_id") or "")
        if not cid or cid == prev:
            return
        if prev:
            for k in _CACHE_KEYS + ("items_count", "stale_strikes",
                                    "walk_at", "likes_at"):
                extra.pop(k, None)
        self.tokens.set_extra("channel_id", cid)

    # ── uploads walk: views + likes + comments ──────────────────────────────
    def _walk_totals(self, uploads: str, channel_views: int) -> tuple:
        """(views, likes, comments) summed over every upload, refreshed at most
        every `likes_refresh_min` minutes and cached in the token file between
        runs.

        Per-video viewCount is current where the channel aggregate lags by
        hours, so views come from this walk too — same statistics call, no
        extra quota. When the walk is off, or nothing has been cached yet and
        it fails, views fall back to the channel counter and likes/comments to
        None (a dash rather than a misleading zero).
        """
        if not self.config.get("count_likes", True) or not uploads:
            return channel_views, None, None
        extra = self.tokens.extra
        # A token file from before comments were counted lacks comments_total:
        # not "have", so it walks once now instead of serving a cache with a
        # hole in it.
        have   = all(k in extra for k in _CACHE_KEYS)
        cached = tuple(int(extra[k]) for k in _CACHE_KEYS) if have else None
        every  = int(self.config.get("likes_refresh_min", 15)) * 60
        if have and time.time() < extra.get("walk_at", 0) + every:
            return cached
        try:
            views, likes, comments, n = self._sum_uploads(uploads)
        except Exception:
            # The walk is the extra; never let it cost us the follower count.
            log.exception("youtube: uploads walk failed, reusing cached values")
            if have:
                return cached
            # Older token files: whatever partial cache they hold, dash the rest.
            views = extra.get("views_total")
            likes = extra.get("likes_total")
            return (channel_views if views is None else int(views),
                    None if likes is None else int(likes), None)
        # Stale-read guard (base.stale_strike): per-video viewCount jitters
        # between reads, so "no fewer videos, fewer views" is held for one pass —
        # only once there is a full cache to hold. The held pass is stamped
        # like a real one: the cadence, and with it the quota, must not
        # depend on which replica answered.
        strike = stale_strike(log, extra, self.name, n, views) if have else 0
        if strike:
            self.tokens.update_extra({"stale_strikes": strike,
                                      "walk_at": time.time()})
            return cached
        # One write: the sums, their video count and timestamp belong
        # together. Drop the pre-views-walk stamp so an upgraded token file
        # doesn't keep it.
        self.tokens.extra.pop("likes_at", None)
        self.tokens.update_extra({"views_total": views, "likes_total": likes,
                                  "comments_total": comments,
                                  "items_count": n, "walk_at": time.time(),
                                  "stale_strikes": 0})
        return views, likes, comments

    def _sum_uploads(self, uploads: str) -> tuple:
        """(views, likes, comments, videos summed) over the uploads playlist."""
        ids   = self._upload_ids(uploads)
        views = likes = comments = n = 0
        for i in range(0, len(ids), _PER_PAGE):
            r = requests.get(_VIDEOS, params={
                "part": "statistics", "id": ",".join(ids[i:i + _PER_PAGE]),
                "maxResults": _PER_PAGE,
            }, headers=self._auth(), timeout=20)
            if not r.ok:
                log.error("youtube videos.list HTTP %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()
            for item in r.json().get("items", []):
                st = item.get("statistics") or {}
                views += int(st.get("viewCount") or 0)
                # likeCount is absent when the uploader hides it, commentCount
                # when comments are turned off for the video.
                likes    += int(st.get("likeCount") or 0)
                comments += int(st.get("commentCount") or 0)
                n += 1
        return views, likes, comments, n

    def _upload_ids(self, uploads: str) -> list:
        ids, page = [], None
        while True:
            params = {"part": "contentDetails", "playlistId": uploads,
                      "maxResults": _PER_PAGE}
            if page:
                params["pageToken"] = page
            r = requests.get(_PLAYLIST, params=params, headers=self._auth(),
                             timeout=20)
            if not r.ok:
                log.error("youtube playlistItems HTTP %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()
            body = r.json()
            ids += [it["contentDetails"]["videoId"]
                    for it in body.get("items", [])
                    if (it.get("contentDetails") or {}).get("videoId")]
            page = body.get("nextPageToken")
            if not page:
                return ids

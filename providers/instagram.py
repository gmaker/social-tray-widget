"""Instagram provider — official Graph API ("Instagram API with Instagram Login").

Replaces an older anonymous read of the undocumented
`i.instagram.com/api/v1/users/web_profile_info/` endpoint, which Instagram
rate-limited to a permanent HTTP 429. Everything here goes through
graph.instagram.com with a real token.

Setup, once:
  1. Meta app at developers.facebook.com with the "Instagram API with Instagram
     Login" product. The account needs the Instagram Tester role, granted on the
     Roles tab and then accepted from the Instagram account itself
     (Settings -> Apps and websites -> Tester invites).
  2. Dashboard -> Instagram -> API setup with Instagram login ->
     Generate access tokens -> copy the token.
  3. Paste it into settings.json -> providers.instagram.setup_token

That token is already long-lived (60 days) — this product has no short-lived
stage, so there is nothing to exchange and no app secret to keep on disk. On
first run we adopt it, store it in tokens/instagram.json, and clear
`setup_token` from settings.json. It renews itself from then on and the
dashboard is never needed again.

Metrics:
  * followers -> /me?fields=followers_count      one call, exact
  * likes     -> `like_count` summed over /me/media
  * views     -> lifetime `views` insight, summed over every post

Likes ride along on the media listing for free. Views don't: the media node
documents `view_count` / `total_views_count`, but this product returns neither —
it answers 200 and silently omits them — so views come from /insights, which
needs the `instagram_business_manage_insights` scope. That would be one call per
post, except the `?ids=` multi-read works here and takes 50 at a time, so a
50-post account costs one call, not fifty.

Both totals are cached for `views_refresh_min` minutes: Instagram's rate limit is
"4800 x impressions per 24h", so a quiet account has a small budget and this is
the only part of a poll costing more than one call.

Config (settings.json -> providers.instagram):
    "setup_token":       dashboard token, adopted on first run and cleared
    "count_views":       default true; false keeps likes but skips the insights
                         calls, leaving views at 0
    "views_refresh_min": default 15
"""

from __future__ import annotations

import logging
import time

import requests

from .base import Metrics, Provider

log = logging.getLogger("social.instagram")

_API      = "https://graph.instagram.com"
_VERSION  = "v25.0"
_REFRESH  = f"{_API}/refresh_access_token"
_ME       = f"{_API}/{_VERSION}/me"
_MEDIA    = f"{_API}/{_VERSION}/me/media"
_INSIGHTS = f"{_API}/{_VERSION}/insights"

_PER_PAGE = 100   # media nodes per /me/media page
_PER_READ = 50    # ids per ?ids= multi-read — the Graph API cap

# "The media was posted before the most recent time that the user's account was
# converted to a business account" — permanent, that post will never have
# insights.
_PREDATES_CONVERSION = 2108006

_LONG_LIVED = 60 * 86400
# A long-lived token refuses to refresh until it is 24h old and can't be
# refreshed at all once expired, so renew it with a week still on the clock.
_MIN_AGE        = 24 * 3600 + 600
_REFRESH_MARGIN = 7 * 86400


def _predates_conversion(resp) -> bool:
    try:
        error = resp.json().get("error") or {}
        return error.get("error_subcode") == _PREDATES_CONVERSION
    except Exception:
        return False


def _insight_value(payload: dict) -> int:
    """Dig the number out of one insights reply:
    {"data": [{"name": "views", "values": [{"value": 122}], ...}]}"""
    for metric in payload.get("data") or []:
        for entry in metric.get("values") or []:
            if entry.get("value") is not None:
                return int(entry["value"])
    return 0


class InstagramProvider(Provider):
    name          = "instagram"
    label         = "Instagram"
    default_color = (225, 48, 108)

    # ── auth ────────────────────────────────────────────────────────────────
    def ensure_auth(self) -> bool:
        if self.tokens.access_token:
            if self._due_for_refresh():
                self._refresh()
            if self.tokens.is_valid():
                return True
            log.warning("instagram: stored token is expired and past the refresh "
                        "window — generate a new setup_token in the dashboard")
        return self._adopt()

    def _due_for_refresh(self) -> bool:
        issued = self.tokens.extra.get("issued_at", 0)
        now    = time.time()
        return (now - issued > _MIN_AGE
                and self.tokens.expiry - now < _REFRESH_MARGIN)

    def _adopt(self) -> bool:
        """Take the token pasted into settings.json and make it ours.

        The dashboard hands out a 60-day token directly, so there is nothing to
        exchange — `ig_exchange_token` rejects these with a misleading "Session
        key invalid". Refresh it once instead, so the 60 days run from now and
        Instagram states the real expiry; if it declines (it won't refresh a
        token under 24h old), keep the token exactly as handed over and let the
        usual cycle renew it later.
        """
        setup = (self.config.get("setup_token") or "").strip()
        if not setup:
            log.error("instagram: no usable token and setup_token is empty — "
                      "generate one in the Meta app dashboard (Instagram -> API "
                      "setup with Instagram login -> Generate token) and put it "
                      "in settings.json -> providers.instagram.setup_token")
            return False

        if not self._refresh(setup):
            probe = requests.get(_ME, params={"fields": "username",
                                              "access_token": setup}, timeout=20)
            if not probe.ok:
                log.error("instagram: setup_token rejected, HTTP %s: %s",
                          probe.status_code, probe.text[:500])
                return False
            self._store({"access_token": setup, "expires_in": _LONG_LIVED})

        self.config["setup_token"] = ""
        self.save_config()
        log.info("instagram: setup_token adopted — the dashboard is done with")
        return True

    def _refresh(self, token: str = "") -> bool:
        try:
            r = requests.get(_REFRESH, params={
                "grant_type":   "ig_refresh_token",
                "access_token": token or self.tokens.access_token,
            }, timeout=20)
            if r.ok:
                self._store(r.json())
                log.info("instagram: token refreshed")
                return True
            # Not fatal on the periodic path — the token normally has days left.
            log.error("instagram refresh HTTP %s: %s", r.status_code, r.text[:500])
        except Exception:
            log.exception("instagram refresh failed")
        return False

    def _store(self, payload: dict) -> None:
        extra = dict(self.tokens.extra)
        extra["issued_at"] = time.time()
        # Instagram has no separate refresh token: the access token renews itself.
        self.tokens.save(payload["access_token"], None,
                         int(payload.get("expires_in", _LONG_LIVED)), extra)

    # ── fetch ───────────────────────────────────────────────────────────────
    def fetch(self) -> Metrics:
        if not self.tokens.is_valid() and not self.ensure_auth():
            return Metrics(ok=False, error="not authorised")

        r = requests.get(_ME, params={
            "fields":       "username,followers_count,media_count",
            "access_token": self.tokens.access_token,
        }, timeout=20)
        if not r.ok:
            log.error("instagram /me HTTP %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        followers = int(r.json().get("followers_count", 0))
        views, likes = self._totals()
        return Metrics(followers=followers, views=views, likes=likes)

    def _totals(self) -> tuple:
        """(views, likes) across every post, re-read at most every
        `views_refresh_min` minutes and cached in the token file between runs."""
        extra  = self.tokens.extra
        cached = int(extra.get("views_total", 0)), int(extra.get("likes_total", 0))
        every  = int(self.config.get("views_refresh_min", 15)) * 60
        if time.time() < extra.get("totals_at", 0) + every:
            return cached
        try:
            views, likes = self._sum_media()
        except Exception:
            # These are the secondary numbers; don't lose the follower count
            # over them. The next poll retries.
            log.exception("instagram: media pass failed, reusing cached totals")
            return cached
        self.tokens.set_extra("views_total", views)
        self.tokens.set_extra("likes_total", likes)
        self.tokens.set_extra("totals_at", time.time())
        return views, likes

    def _sum_media(self) -> tuple:
        nodes = self._media()
        likes = sum(int(node.get("like_count") or 0) for node in nodes)
        if not self.config.get("count_views", True):
            return 0, likes
        skip  = set(self.tokens.extra.get("no_insights") or [])
        ids   = [node["id"] for node in nodes if node["id"] not in skip]
        views = sum(self._views_of(ids[i:i + _PER_READ])
                    for i in range(0, len(ids), _PER_READ))
        return views, likes

    def _media(self) -> list:
        nodes  = []
        url    = _MEDIA
        params = {"fields": "id,like_count", "limit": _PER_PAGE,
                  "access_token": self.tokens.access_token}
        while url:
            r = requests.get(url, params=params, timeout=20)
            if not r.ok:
                log.error("instagram /me/media HTTP %s: %s",
                          r.status_code, r.text[:500])
            r.raise_for_status()
            page = r.json()
            nodes += [node for node in page.get("data", []) if node.get("id")]
            # `paging.next` already carries the cursor and the token.
            url    = (page.get("paging") or {}).get("next")
            params = None
        return nodes

    def _views_of(self, ids: list) -> int:
        """Lifetime views for up to `_PER_READ` posts in one multi-read."""
        if not ids:
            return 0
        r = requests.get(_INSIGHTS, params={
            "ids":          ",".join(ids),
            "metric":       "views",
            "access_token": self.tokens.access_token,
        }, timeout=30)
        if r.ok:
            return sum(_insight_value(v) for v in r.json().values())
        # A multi-read is all-or-nothing: one post Instagram won't report on
        # zeroes the whole batch. Pay for single calls this once — the posts that
        # can never have insights get remembered below, so the next pass is a
        # single call again.
        log.warning("instagram insights batch HTTP %s, retrying one by one: %s",
                    r.status_code, r.text[:300])
        return sum(self._views_of_one(i) for i in ids)

    def _views_of_one(self, media_id: str) -> int:
        r = requests.get(f"{_API}/{_VERSION}/{media_id}/insights", params={
            "metric": "views", "access_token": self.tokens.access_token,
        }, timeout=20)
        if r.ok:
            return _insight_value(r.json())
        if _predates_conversion(r):
            # Permanent — blacklist it so it stops poisoning the batch. Anything
            # else (a transient error, a clip too fresh to report) is left alone
            # to be retried next pass.
            skip = list(self.tokens.extra.get("no_insights") or [])
            self.tokens.set_extra("no_insights", skip + [media_id])
            log.info("instagram: media %s predates the professional-account "
                     "switch and has no insights — not asking again", media_id)
        else:
            log.info("instagram: no views for media %s (HTTP %s)",
                     media_id, r.status_code)
        return 0

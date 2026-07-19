"""VK providers — a community's wall and its VK Video channel, one service key.

A VK Video channel is its backing community: the same numeric id serves both,
so one community can feed two rows. To keep the tray total honest the video
row reports followers=None (a dash) — the community's members are already
counted by the wall row.

  * VKProvider       followers = community members (groups.getById, exact)
                     likes / views = summed over wall posts (wall.get)
  * VKVideoProvider  followers = None (same community — never counted twice)
                     likes / views = summed over videos (video.get)

Auth is a *service key* from any VK ID app (dev.vk.com -> Приложения ->
Создать приложение; the key is in the app's settings). No OAuth, no expiry,
5 rps. Don't trust the docs on what it can call: they say video.get needs a
user token — live calls show the service key works (verified 2026-07-20,
api.vk.ru v5.199). The community access key from group settings is NOT
enough: wall.get answers error 27 and video.get error 5 with it.

Both walks address the community as owner_id=-<numeric id> — never as
wall.get's domain=, which resolves bare digits to a USER id (error 18 at
best, a stranger's wall summed silently at worst). A screen name is resolved
through utils.resolveScreenName once, cached keyed by the name so re-pointing
`group` in settings re-resolves, and the resolved type must be a community —
a user's screen name resolves too, and negating a user id would land on
whatever community happens to share the number.

Both walks page 100 items per request, bounded by the response's total count
(a window whose items are all hidden comes back empty without meaning the
end), and are cached for `views_refresh_min` minutes, so a poll normally
costs one groups.getById. The historical (now undocumented) wall.get quota
of ~5000 calls/day starts to matter only past several thousand posts at the
default cadence.

Old posts (pre-2017) may lack the views counter — counted as 0. Clips are
not returned by video.get and are not counted.

Config (settings.json -> providers.vk / providers.vkvideo):
    "service_token":     the service key; the same one can serve both rows
    "group":             community screen name or numeric id (no minus)
    "count_views":       default true; false skips the walk, likes go dash
    "views_refresh_min": default 15
"""

from __future__ import annotations

import logging
import time

import requests

from .base import Metrics, Provider

log = logging.getLogger("social.vk")

_API = "https://api.vk.ru/method"
_V   = "5.199"
_PER_PAGE = 100

# Retriable VK errors: too many rps / flood / daily quota. Anything else is a
# real fault and should surface as the row's error.
_THROTTLE_CODES = {6, 9, 29}

# resolveScreenName types that negate into a community owner_id.
_COMMUNITY_TYPES = ("group", "page", "event")


class VKError(Exception):
    """code 0 = synthesized locally (no such VK code); the rest are VK's own."""

    def __init__(self, code: int, msg: str):
        super().__init__(f"VK error {code}: {msg}")
        self.code = code


def _call(token: str, method: str, **params) -> dict:
    params.update(access_token=token, v=_V)
    r = requests.get(f"{_API}/{method}", params=params, timeout=20)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        e = d["error"]
        raise VKError(int(e.get("error_code", 0)), e.get("error_msg", "?"))
    return d.get("response", {})


class _VKBase(Provider):
    """Shared plumbing: config access, auth check, cached summation walks."""

    def _token(self) -> str:
        return str(self.config.get("service_token") or "").strip()

    def _group(self) -> str:
        return str(self.config.get("group") or "").strip().lstrip("-")

    def ensure_auth(self) -> bool:
        # A service key is a static credential — nothing interactive to do.
        if not self._token() or not self._group():
            log.error("%s: service_token/group not set in settings", self.name)
            return False
        return True

    def _group_id(self) -> str:
        """The community's numeric id; a screen name is resolved once and
        cached keyed by the name, so re-pointing `group` re-resolves."""
        g = self._group()
        if g.isdigit():
            return g
        extra = self.tokens.extra
        if extra.get("group_id") and extra.get("group_id_for") == g:
            return str(extra["group_id"])
        resp = _call(self._token(), "utils.resolveScreenName", screen_name=g) or {}
        gid  = str(resp.get("object_id", ""))
        kind = resp.get("type", "")
        if not gid:
            raise VKError(0, f"cannot resolve screen name {g!r}")
        if kind not in _COMMUNITY_TYPES:
            # A user's screen name resolves too; -<user id> would address an
            # unrelated community that happens to share the number.
            raise VKError(0, f"{g!r} is a {kind or '?'}, not a community")
        # The group changed — the cached totals belong to the old one; drop
        # the timestamp so the next _totals() call re-walks immediately.
        extra.pop("totals_at", None)
        self.tokens.set_extra("group_id", gid)
        self.tokens.set_extra("group_id_for", g)
        return gid

    def _members(self) -> int:
        resp = _call(self._token(), "groups.getById",
                     group_id=self._group(), fields="members_count")
        g = (resp.get("groups") or [{}])[0]
        if g.get("deactivated"):
            raise VKError(15, f"community is {g['deactivated']}")
        if "members_count" not in g:
            # A silent 0 would read as real data and poison the baselines.
            raise VKError(15, "members_count hidden (closed community?)")
        return int(g["members_count"])

    def _totals(self) -> tuple:
        """(views, likes) via the subclass's `_walk_page`, re-read at most every
        `views_refresh_min` minutes and cached in the token file between runs."""
        if not self.config.get("count_views", True):
            return 0, None
        extra  = self.tokens.extra
        walked = "totals_at" in extra
        cached = ((int(extra.get("views_total", 0)),
                   int(extra.get("likes_total", 0)))
                  if walked else (0, None))   # dash until a walk succeeded
        every  = int(self.config.get("views_refresh_min", 15)) * 60
        if walked and time.time() < extra.get("totals_at", 0) + every:
            return cached
        views = likes = offset = 0
        total = None                  # real bound learned from the first page
        try:
            while total is None or offset < total:
                items, total = self._walk_page(offset)
                for v, l in items:
                    views += v
                    likes += l
                # Hidden items make pages short or even empty — the response's
                # total count is the bound, not the page contents.
                offset += _PER_PAGE
        except VKError as exc:
            if exc.code not in _THROTTLE_CODES:
                raise
            # Rate/quota pressure — keep the follower count alive and let the
            # next pass retry the walk.
            log.warning("%s: %s during walk, reusing cached totals",
                        self.name, exc)
            return cached
        except (requests.RequestException, ValueError) as exc:
            # Transport trouble — a timeout, a 5xx, a non-JSON body from a
            # middlebox — is as transient as a throttle: same treatment.
            log.warning("%s: %s during walk, reusing cached totals",
                        self.name, exc)
            return cached
        self.tokens.set_extra("views_total", views)
        self.tokens.set_extra("likes_total", likes)
        self.tokens.set_extra("totals_at", time.time())
        return views, likes

    def _walk_page(self, offset: int) -> tuple:
        """One page of (views, likes) pairs plus the response's total count."""
        raise NotImplementedError


class VKProvider(_VKBase):
    name          = "vk"
    label         = "VK"
    default_color = (0, 119, 255)      # VK's brand blue #0077FF

    def fetch(self) -> Metrics:
        self._group_id()   # re-resolves (and re-walks) if `group` was re-pointed
        followers    = self._members()
        views, likes = self._totals()
        return Metrics(followers=followers, views=views, likes=likes)

    def _walk_page(self, offset: int) -> tuple:
        resp = _call(self._token(), "wall.get",
                     owner_id=f"-{self._group_id()}",
                     count=_PER_PAGE, offset=offset)
        return ([((p.get("views") or {}).get("count", 0),
                  (p.get("likes") or {}).get("count", 0))
                 for p in resp.get("items", [])],
                int(resp.get("count", 0)))


class VKVideoProvider(_VKBase):
    name          = "vkvideo"
    label         = "VK Video"
    # The violet end of VK Video's logo gradient — its blue end is VKProvider's.
    default_color = (122, 133, 255)
    # One community, two rows: the popup centres VK's followers cell over both.
    followers_span_with = "vk"

    def fetch(self) -> Metrics:
        self._group_id()   # re-resolves (and re-walks) if `group` was re-pointed
        views, likes = self._totals()
        # followers=None on purpose: the backing community's members already
        # count on the VK row; a second copy would double the tray total.
        return Metrics(followers=None, views=views, likes=likes)

    def _walk_page(self, offset: int) -> tuple:
        resp = _call(self._token(), "video.get",
                     owner_id=f"-{self._group_id()}",
                     count=_PER_PAGE, offset=offset)
        return ([(int(v.get("views") or 0),
                  (v.get("likes") or {}).get("count", 0))
                 for v in resp.get("items", [])],
                int(resp.get("count", 0)))

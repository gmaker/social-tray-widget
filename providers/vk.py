"""VK providers — a community's wall and its VK Video channel, one service key.

A VK Video channel is its backing community: the same numeric id serves both,
so one community can feed two rows. To keep the tray total honest the video
row reports followers=None (a dash) — the community's members are already
counted by the wall row.

  * VKProvider       followers = community members (groups.getById, exact)
                     likes / views / comments = summed over wall posts (wall.get)
  * VKVideoProvider  followers = None (same community — never counted twice)
                     likes / views = summed over the long videos (video.get)
                     comments = summed too, but read off the community wall:
                     video.get strips the field under a service key, the copy
                     of the video embedded in its wall post keeps it (see
                     _WALL_COMMENTS)
  * VKClipsProvider  followers = None (same community again)
                     likes / views / comments = summed over the community's
                     clips, which the public API exposes only by explicit id
                     (see below)

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

Old posts (pre-2017) may lack the views counter — counted as 0.

Clips (short vertical videos) are NOT in video.get's owner listing and have no
list method in the public API at all. But they share the id sequence of the
community's long videos, and video.get videos=<explicit ids> returns them as
type=short_video with real views and likes — as long as the id list is pure:
one regular-video id mixed in silently zeroes every clip in the response
(undocumented, verified 2026-07-22). VKClipsProvider therefore discovers clip
ids by scanning the video-id neighbourhood for type=short_video, caches them,
and reads their stats in a clips-only batch. It needs at least one regular
video to anchor the id space; a clip-only community can't be bootstrapped.

Config (settings.json -> providers.vk / providers.vkvideo / providers.vkclips):
    "service_token":     the service key; the same one can serve all rows
    "group":             community screen name or numeric id (no minus)
    "count_views":       default true; false skips the walk, likes and
                         comments go dash
    "views_refresh_min": default 15
"""

from __future__ import annotations

import logging
import re
import time

import requests

from .base import Metrics, Provider, stale_strike

log = logging.getLogger("social.vk")

_API = "https://api.vk.ru/method"
_V   = "5.199"
_PER_PAGE = 100

# Retriable VK errors: too many rps / flood / daily quota. Anything else is a
# real fault and should surface as the row's error.
_THROTTLE_CODES = {6, 9, 29}

# resolveScreenName types that negate into a community owner_id.
_COMMUNITY_TYPES = ("group", "page", "event")

# requests' ConnectionError/ProxyError text embeds the request URL - service
# key included - so anything that logs str(exc) must go through this first.
_TOKEN_RE = re.compile(r"access_token=[^&\s]+")

# Clip discovery/stat tuning (VKClipsProvider).
_CLIP_CHUNK  = 50    # ids consumed per video.get videos= call (the API cap)
_SCAN_MARGIN = 60    # start the id scan this far below the earliest known id
_SCAN_STOP   = 3     # consecutive empty windows that mean "past the last id"
_SCAN_CAP    = 60    # windows hard-cap, a backstop against a runaway scan


def _is_stub(o: dict) -> bool:
    """A clip returned in a purity-violated response comes back present but
    zeroed (views 0 AND date 0). A genuinely new clip with no views yet still
    carries a real date, so it is not mistaken for a stub."""
    return int(o.get("views") or 0) == 0 and int(o.get("date") or 0) == 0


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


# Comment counts of a community's LONG videos, harvested from its wall. Under a
# service key video.get strips `comments` (and can_comment) from a long video —
# in the listing, by id, with extended=1, at every API version — and
# video.getComments is refused (error 28). But the very same video object,
# embedded as a wall post's attachment, keeps the field (verified 2026-09-16:
# the numbers differ from the post's own comments.count — post 100 has 4, its
# video 1 — and on clips, where video.get does return the field, they match it
# exactly). So whichever row walks the wall first fills this memo — normally the
# VK row, whose wall.get responses carry the attachments anyway — and the VK
# Video row reads it: a poll costs no extra call. A harvest stays live for its
# walker's own refresh window (a VK row on a slower cadence is not
# second-guessed), and one the VK row rejected as a stale replica is dropped.
# When the VK row is off, or no live harvest exists, the video row walks the
# wall itself. A video never posted to the wall has no entry.
#   owner id -> {"at": walk start, "ttl": walker's window, "done": whole wall
#                seen, "map": {video id: comments}}
_WALL_COMMENTS: dict = {}


def _wall_page(token: str, owner: str, offset: int, ttl: float) -> dict:
    """One wall.get page (the raw response), harvested into _WALL_COMMENTS;
    `ttl` is the walker's refresh window, how long the harvest stays live."""
    resp = _call(token, "wall.get", owner_id=owner, count=_PER_PAGE,
                 offset=offset)
    _harvest_wall(owner, offset, resp, ttl)
    return resp


def _harvest_wall(owner: str, offset: int, resp: dict, ttl: float) -> None:
    """Record the long-video comment counts a wall page carries. Page 0 starts
    a fresh walk; `done` flips once the page reaches the wall's total, so an
    aborted walk is never mistaken for a complete one. Every non-clip type
    counts (music_video and movie are long videos too — clips have their own
    row). Attachments missing the field (VK may one day strip it here too)
    are left out rather than recorded as 0."""
    if offset == 0:
        _WALL_COMMENTS[owner] = {"at": time.time(), "ttl": ttl,
                                 "done": False, "map": {}}
    entry = _WALL_COMMENTS.get(owner)
    if entry is None:                  # a page of a walk this process never began
        return
    own = int(owner)
    for post in resp.get("items", []):
        atts = list(post.get("attachments") or [])
        for repost in post.get("copy_history") or []:
            atts += repost.get("attachments") or []
        for a in atts:
            v = a.get("video") if a.get("type") == "video" else None
            if (v and int(v.get("owner_id") or 0) == own
                    and v.get("type") != "short_video"
                    and v.get("comments") is not None):
                entry["map"][int(v["id"])] = int(v["comments"])
    entry["done"] = offset + _PER_PAGE >= int(resp.get("count", 0))


def _wall_video_comments(token: str, owner: str, every: float) -> dict:
    """{video id: comments} for the community's long videos, off its wall:
    a complete harvest still live — younger than its walker's window or
    this row's, whichever is longer — if there is one, otherwise a walk made
    now (the VK row's own paging, 100 posts a call)."""
    entry = _WALL_COMMENTS.get(owner)
    if (entry and entry["done"]
            and time.time() - entry["at"] < max(every, entry.get("ttl", 0))):
        return entry["map"]
    offset, total = 0, None
    while total is None or offset < total:
        resp   = _wall_page(token, owner, offset, every)
        total  = int(resp.get("count", 0))
        offset += _PER_PAGE
    return _WALL_COMMENTS[owner]["map"]


class _VKBase(Provider):
    """Shared plumbing: config access, auth check, cached summation walks."""

    # Whether the walk can count comments. Wall posts carry comments.count,
    # clips (video.get by explicit id, type=short_video) a bare `comments`,
    # and long videos get theirs off the wall (see _WALL_COMMENTS). A row
    # that can't count reports None, which the popup draws as a dash — never
    # a plausible 0; the flag stays for any row that loses the ability.
    counts_comments = True

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
            return self._track_group(g)
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
        self.tokens.set_extra("group_id_for", g)
        return self._track_group(gid)

    def _track_group(self, gid: str) -> str:
        """Record the resolved community id and, when it CHANGES (a re-point,
        numeric or screen-name), drop the previous community's cached totals
        and clip ids — otherwise a re-pointed row would serve the old
        community's numbers (or, for clips, mix its id slots into the new
        community). The numeric path used to skip this entirely."""
        extra = self.tokens.extra
        prev  = str(extra.get("group_id") or "")
        if prev and prev != gid:
            for k in ("totals_at", "views_total", "likes_total",
                      "comments_total", "clip_ids", "items_count",
                      "stale_strikes"):
                extra.pop(k, None)
        if prev != gid:
            self.tokens.set_extra("group_id", gid)
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
        """(views, likes, comments) via the subclass's `_walk_page`, re-read at
        most every `views_refresh_min` minutes and cached in the token file
        between runs."""
        if not self.config.get("count_views", True):
            return 0, None, None
        extra  = self.tokens.extra
        walked = "totals_at" in extra
        # A token file from before comments were counted: its views/likes still
        # serve when a walk fails (comments dash), but it isn't fresh — walk now.
        has_comments = "comments_total" in extra   # may be cached as None
        # The flag wins over the cache too: a row that can't count never
        # serves a number, whatever an older token file has in it.
        cached_c = (None if not self.counts_comments
                    or extra.get("comments_total") is None
                    else int(extra["comments_total"]))
        cached = ((int(extra.get("views_total", 0)),
                   int(extra.get("likes_total", 0)), cached_c)
                  if walked else (0, None, None))   # dash until a walk succeeded
        every  = int(self.config.get("views_refresh_min", 15)) * 60
        if (walked and has_comments
                and time.time() < extra.get("totals_at", 0) + every):
            return cached
        try:
            views, likes, comments, n = self._compute_totals()
            if not self.counts_comments:
                comments = None
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
                        self.name, _TOKEN_RE.sub("access_token=***", str(exc)))
            return cached
        # Stale-replica guard (base.stale_strike): VK's backends are
        # eventually consistent, and now and then a walk returns no fewer
        # items but old, much lower view counts. A held walk still stamps
        # the pass, or it would re-run on EVERY poll — 15x the cadence and
        # past the daily quota on a big wall. Guarded only once the cache is
        # complete: a hold on a pre-comments token file would be re-walked
        # on the very next poll anyway (has_comments bypasses the window).
        strike = (stale_strike(log, extra, self.name, n, views)
                  if has_comments else 0)
        if strike:
            self._stale_walk()
            self.tokens.update_extra({"stale_strikes": strike,
                                      "totals_at": time.time()})
            return cached
        # One write: the sums, their item count and timestamp belong together.
        self.tokens.update_extra({"views_total": views, "likes_total": likes,
                                  "comments_total": comments,
                                  "items_count": n, "totals_at": time.time(),
                                  "stale_strikes": 0})
        return views, likes, comments

    def _compute_totals(self) -> tuple:
        """(views, likes, comments, items summed). Default: page the owner's
        items through `_walk_page`; overridden where the totals come from an
        explicit id list (clips have no list method)."""
        views = likes = comments = offset = n = 0
        total = None                  # real bound learned from the first page
        while total is None or offset < total:
            items, total = self._walk_page(offset)
            for v, l, c in items:
                views    += v
                likes    += l
                comments += c
            n += len(items)
            # Hidden items make pages short or even empty — the response's
            # total count is the bound, not the page contents.
            offset += _PER_PAGE
        return views, likes, comments, n

    def _walk_page(self, offset: int) -> tuple:
        """One page of (views, likes, comments) triples plus the response's
        total count."""
        raise NotImplementedError

    def _stale_walk(self) -> None:
        """The walk just made is being held as a stale replica. A row whose
        walk feeds shared state drops what that walk left there."""


class VKProvider(_VKBase):
    name          = "vk"
    label         = "VK"
    default_color = (0, 119, 255)      # VK's brand blue #0077FF

    def fetch(self) -> Metrics:
        self._group_id()   # re-resolves (and re-walks) if `group` was re-pointed
        followers = self._members()
        views, likes, comments = self._totals()
        return Metrics(followers=followers, views=views, likes=likes,
                       comments=comments)

    def _walk_page(self, offset: int) -> tuple:
        # Through _wall_page, so the video attachments' comment counts land in
        # _WALL_COMMENTS for the VK Video row on the way.
        resp = _wall_page(self._token(), f"-{self._group_id()}", offset,
                          int(self.config.get("views_refresh_min", 15)) * 60)
        return ([((p.get("views") or {}).get("count", 0),
                  (p.get("likes") or {}).get("count", 0),
                  (p.get("comments") or {}).get("count", 0))
                 for p in resp.get("items", [])],
                int(resp.get("count", 0)))

    def _stale_walk(self) -> None:
        # The held walk's pages were harvested on the way in; a replica this
        # row would not believe is not for the VK Video row either.
        _WALL_COMMENTS.pop(f"-{self._group_id()}", None)


class VKVideoProvider(_VKBase):
    name          = "vkvideo"
    label         = "VK Video"
    # The violet end of VK Video's logo gradient — its blue end is VKProvider's.
    default_color = (122, 133, 255)
    # One community, two rows: the popup centres VK's followers cell over both.
    followers_span_with = "vk"

    def fetch(self) -> Metrics:
        self._group_id()   # re-resolves (and re-walks) if `group` was re-pointed
        views, likes, comments = self._totals()
        # followers=None on purpose: the backing community's members already
        # count on the VK row; a second copy would double the tray total.
        return Metrics(followers=None, views=views, likes=likes,
                       comments=comments)

    def _compute_totals(self) -> tuple:
        """(views, likes, comments, videos summed). Views and likes page
        through video.get; comments come off the wall (_WALL_COMMENTS). A
        video that was never posted to the wall counts 0 — and when none of
        the videos is there the row can't count and reports None."""
        token, owner = self._token(), f"-{self._group_id()}"
        own = int(owner)
        views = likes = offset = n = 0
        ids, total = [], None
        while total is None or offset < total:
            resp  = _call(token, "video.get", owner_id=owner,
                          count=_PER_PAGE, offset=offset)
            items = resp.get("items", [])
            for v in items:
                views += int(v.get("views") or 0)
                likes += int((v.get("likes") or {}).get("count", 0))
                # A video added from another owner keeps that owner's id
                # sequence — it shares no id space with ours, so it can
                # never be looked up in the harvest (which is ours only).
                if int(v.get("owner_id") or 0) == own:
                    ids.append(int(v["id"]))
            total   = int(resp.get("count", 0))
            n      += len(items)
            offset += _PER_PAGE
        if not ids:            # nothing of ours to look up: no wall walk
            return views, likes, 0, n
        every = int(self.config.get("views_refresh_min", 15)) * 60
        try:
            wall = _wall_video_comments(token, owner, every)
        except (VKError, requests.RequestException, ValueError) as exc:
            # The wall is the extra here — views and likes are already in
            # hand — so a wall that is off, throttled or unreachable costs
            # the comments only: the last known count, or the dash.
            log.warning("%s: %s reading the wall for comments, keeping the "
                        "last count", self.name,
                        _TOKEN_RE.sub("access_token=***", str(exc)))
            c = self.tokens.extra.get("comments_total")
            return views, likes, (None if c is None else int(c)), n
        covered  = [i for i in ids if i in wall]
        comments = sum(wall[i] for i in covered) if covered else None
        return views, likes, comments, n


class VKClipsProvider(_VKBase):
    name          = "vkclips"
    label         = "VK Clips"
    # VK Clips' magenta — from the brand, not the palette pipeline; distinct
    # from VK's blue and VK Video's periwinkle so the three VK rows read apart.
    default_color = (230, 100, 160)
    # Same community as VK / VK Video — members counted once (spanned cell).
    followers_span_with = "vk"
    # The user can fold this row into VK Video from the tray menu.
    merge_into          = "vkvideo"

    def fetch(self) -> Metrics:
        self._group_id()   # re-resolves (and drops stale totals) on a re-point
        views, likes, comments = self._totals()
        return Metrics(followers=None, views=views, likes=likes,
                       comments=comments)

    def _compute_totals(self) -> tuple:
        owner  = f"-{self._group_id()}"
        target = self._clips_count()
        ids    = [int(i) for i in (self.tokens.extra.get("clip_ids") or [])]
        views, likes, comments, live = self._clip_stats(owner, ids)
        if len(live) < target:
            # A new clip — or one that was transiently absent last pass.
            # Rescan for more ids and re-stat the UNION of the cache and the
            # scan: the scan can only ADD clips it reaches, never drop a
            # cache-live clip it happened not to cover. Genuinely deleted ids
            # still prune — _clip_stats omits anything that comes back absent.
            found = self._scan_clips(owner, target)
            if found:
                views, likes, comments, live = self._clip_stats(
                    owner, sorted(set(ids) | found))
        self.tokens.set_extra("clip_ids", sorted(live))
        return views, likes, comments, len(live)

    def _clips_count(self) -> int:
        resp = _call(self._token(), "groups.getById",
                     group_id=self._group_id(), fields="clips_count")
        g = (resp.get("groups") or [{}])[0]
        if "clips_count" not in g:
            # Mirror _members: a hidden count (closed/restricted community) must
            # not read as a real 0 and poison the baseline.
            raise VKError(15, "clips_count hidden (closed community?)")
        return int(g["clips_count"])

    def _clip_stats(self, owner: str, ids: list) -> tuple:
        """Sum (views, likes, comments) over a PURE clip-id list and return the
        set of ids that came back as real objects. Deleted/unknown ids are
        simply absent; never mix a regular-video id in here — it zeroes every
        clip."""
        views = likes = comments = 0
        live  = set()
        for i in range(0, len(ids), _CLIP_CHUNK):
            videos = ",".join(f"{owner}_{c}" for c in ids[i:i + _CLIP_CHUNK])
            resp = _call(self._token(), "video.get", videos=videos)
            for o in resp.get("items", []):
                if _is_stub(o):        # only if purity was violated — skip it
                    continue
                live.add(int(o["id"]))
                views    += int(o.get("views") or 0)
                likes    += int((o.get("likes") or {}).get("count", 0))
                comments += int(o.get("comments") or 0)
        return views, likes, comments, live

    def _scan_clips(self, owner: str, target: int) -> set:
        """Find clip ids by scanning the community's video-id neighbourhood for
        type=short_video. Two parts: every 50-id window that already holds a
        known id (so a wide, gappy id space costs one call per populated
        window, not per void — and clips interspersed among the long videos are
        covered wherever they sit), then a walk UPWARD past the highest known
        id, where new clips land. Empty when there is no anchor (a clip-only
        community)."""
        anchor = (self._video_ids(owner)
                  + [int(i) for i in (self.tokens.extra.get("clip_ids") or [])])
        if not anchor:
            log.warning("vkclips: no regular video to locate the clip id space")
            return set()
        lo, hi = min(anchor), max(anchor)
        starts = {(a // _CLIP_CHUNK) * _CLIP_CHUNK for a in anchor}
        starts.add(((lo - _SCAN_MARGIN) // _CLIP_CHUNK) * _CLIP_CHUNK)
        found = set()
        for s in sorted(starts):
            found |= self._clips_in_window(owner, s)[0]
            if target and len(found) >= target:
                return found
        # New clips get the highest ids: walk on past the top until the id
        # space runs out (empty windows) or the backstop trips.
        cur = (hi // _CLIP_CHUNK + 1) * _CLIP_CHUNK
        empty = windows = 0
        while empty < _SCAN_STOP and windows < _SCAN_CAP:
            clips, n = self._clips_in_window(owner, cur)
            found |= clips
            empty   = empty + 1 if n == 0 else 0
            cur    += _CLIP_CHUNK
            windows += 1
            if target and len(found) >= target:
                break
        return found

    def _clips_in_window(self, owner: str, start: int) -> tuple:
        """(clip ids, item count) for the 50-id window at `start`."""
        block = ",".join(f"{owner}_{start + k}" for k in range(_CLIP_CHUNK))
        items = _call(self._token(), "video.get", videos=block).get("items", [])
        clips = {int(o["id"]) for o in items if o.get("type") == "short_video"}
        return clips, len(items)

    def _video_ids(self, owner: str) -> list:
        """Ids of the community's regular (long) videos — the scan anchor."""
        ids, offset, total = [], 0, None
        while total is None or offset < total:
            resp = _call(self._token(), "video.get",
                         owner_id=owner, count=_PER_PAGE, offset=offset)
            ids += [int(v["id"]) for v in resp.get("items", []) if v.get("id")]
            total = int(resp.get("count", 0))
            offset += _PER_PAGE
        return ids

"""Telegram provider — MTProto via Telethon, with a real user session.

Bot API has no access to post views or reactions, which is why this speaks
MTProto instead: a signed-in user sees everything the Telegram apps show.

Setup, once:
  1. Get api_id / api_hash at https://my.telegram.org -> API development tools.
  2. Fill settings.json -> providers.telegram: api_id, api_hash, channel.
  3. Run `python telegram_login.py` from the project root in a real terminal
     and sign in with the phone number + the code Telegram sends. That writes
     tokens/telegram.session; the widget itself never prompts.

The session file grants full account access — it lives in tokens/ (gitignored)
next to the OAuth tokens and should be treated like a password. The widget
refuses to touch it until telegram_login.py has stamped `signed_in` into
tokens/telegram.json: the session file exists from the moment the login script
starts, and two clients on one SQLite session mid-sign-in can clobber each
other's auth key.

Metrics:
  * followers -> channels.GetFullChannel, one call, exact
  * views     -> `views` summed over every channel post
  * likes     -> reaction counts summed over every channel post

The history walk is the expensive part, so it is bounded two ways. The recent
tail — the last `_TAIL_IDS` message ids — is re-read in full on every pass, so
a fresh post's views stay live while they climb (they accrue mostly in the
first days). Everything older is summed once into a frozen total and not read
again, capping the walk at ~the tail length however long the channel's history
grows. Once a day the frozen part is recomputed from scratch to absorb
deletions and any late growth on it. Totals are cached for `views_refresh_min`
minutes between passes; followers stay live at `poll_interval`. On a
FloodWaitError longer than the auto-sleep threshold the last known numbers are
reused rather than blocking the poll thread — or a dash, if no pass finished.

A fresh client and event loop are opened inside every poll rather than kept
alive: polls can come from two different threads (the supervisor loop and the
tray's "Refresh now"), and binding one asyncio loop to either of them is
fragile. The MTProto handshake costs about a second at a 60s cadence.

Config (settings.json -> providers.telegram):
    "api_id" / "api_hash": from my.telegram.org
    "channel":             @username of the channel; a private channel needs
                           its numeric -100... id and must be resolved once by
                           telegram_login.py while signed in as a member
    "count_views":         default true; false skips the history walk entirely,
                           leaving views at 0 and likes as a dash
    "views_refresh_min":   default 15
    "proxy":               "" (default) follows the Windows system proxy when
                           one is enabled — some ISPs block MTProto's IPs
                           directly while the official apps work through that
                           proxy; "none" forces a direct connection; an
                           explicit "socks5://host:port" or "http://host:port"
                           wins over both
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .base import Metrics, Provider
from ..proxy import resolve_proxy

log = logging.getLogger("social.telegram")

# A client connects and disconnects on every poll; at INFO Telethon narrates
# each one — four log lines a minute, forever. Its warnings still matter.
logging.getLogger("telethon").setLevel(logging.WARNING)

_LOGIN_HINT = "run `python telegram_login.py` in a terminal to sign in"

_FULL_WALK_EVERY = 24 * 3600   # drift correction: full re-baseline cadence
_TAIL_IDS        = 500         # trailing message ids kept live (re-read each
                               # pass); older posts freeze — their views have
                               # long since settled


class TelegramProvider(Provider):
    name          = "telegram"
    label         = "Telegram"
    # Telegram's #229ED9. Chosen from the brand, not re-validated with the
    # palette pipeline the README describes — if it ever reads badly next to
    # TikTok's teal, this is the number to revisit.
    default_color = (34, 158, 217)

    # ── config / session ────────────────────────────────────────────────────
    def _session_path(self) -> str:
        """Telethon appends `.session` itself; lives next to the OAuth tokens."""
        return os.path.join(os.path.dirname(self.tokens.path), "telegram")

    def _cfg(self):
        return (str(self.config.get("api_id") or "").strip(),
                str(self.config.get("api_hash") or "").strip(),
                str(self.config.get("channel") or "").strip())

    def _entity(self):
        """What we hand Telethon: a numeric id as int, anything else as-is."""
        chan = self._cfg()[2]
        return int(chan) if chan.lstrip("-").isdigit() else chan

    # ── auth ────────────────────────────────────────────────────────────────
    def ensure_auth(self) -> bool:
        """No interactive flow here — the one-time sign-in happens in
        telegram_login.py, because the code prompt needs a console and the
        widget runs headless under pythonw."""
        api_id, api_hash, channel = self._cfg()
        if not (api_id and api_hash and channel):
            log.error("telegram: api_id/api_hash/channel not set in settings")
            return False
        if not os.path.exists(self._session_path() + ".session"):
            log.error("telegram: no session yet — %s", _LOGIN_HINT)
            return False
        # The marker is stamped by telegram_login.py from another process only
        # after sign-in truly finished; the session file alone proves nothing —
        # it exists (unauthorised) from the moment the login script starts, and
        # opening it while the script is mid-sign-in corrupts the session.
        self.tokens.reload()
        if not self.tokens.extra.get("signed_in"):
            log.error("telegram: sign-in not finished — %s", _LOGIN_HINT)
            return False
        return True

    # ── fetch ───────────────────────────────────────────────────────────────
    def fetch(self) -> Metrics:
        try:
            import telethon  # noqa: F401 — only Telegram breaks if missing
        except ImportError:
            log.error("telegram: telethon is not installed — pip install telethon")
            return Metrics(ok=False, error="telethon not installed")
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._fetch())
        finally:
            # Telethon's auto-reconnect spawns an untracked task
            # (MTProtoSender._start_reconnect) that no disconnect path cancels;
            # a connection blip mid-fetch can leave it pending, and closing the
            # loop then strands it with its socket. Cancel and drain first.
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _fetch(self) -> Metrics:
        from telethon import TelegramClient, functions
        from telethon.errors import FloodWaitError

        api_id, api_hash, _ = self._cfg()
        try:
            # Re-resolved every poll: the registry read is cheap, and the user
            # toggling their proxy tool shouldn't require a widget restart.
            proxy = resolve_proxy(self.config.get("proxy", ""))
        except ValueError as exc:
            log.error("telegram: %s", exc)
            return Metrics(ok=False, error="bad proxy setting")
        client = TelegramClient(self._session_path(), int(api_id), api_hash,
                                proxy=proxy, flood_sleep_threshold=15)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log.error("telegram: session not authorised — %s", _LOGIN_HINT)
                return Metrics(ok=False, error="not authorised")

            try:
                full = await client(functions.channels.GetFullChannelRequest(
                    self._entity()))
                followers = int(full.full_chat.participants_count or 0)
                if followers != self.tokens.extra.get("followers"):
                    self.tokens.set_extra("followers", followers)
            except FloodWaitError as exc:
                # Longer than the auto-sleep threshold — don't stall the poll
                # thread, reuse the last known number and let the next poll try.
                cached = self.tokens.extra.get("followers")
                log.warning("telegram: flood wait %ss on GetFullChannel, "
                            "reusing cached followers", exc.seconds)
                if cached is None:
                    return Metrics(ok=False, error=f"flood wait {exc.seconds}s")
                followers = int(cached)

            views, likes = await self._totals(client)
            return Metrics(followers=followers, views=views, likes=likes)
        finally:
            await client.disconnect()

    async def _totals(self, client) -> tuple:
        """(views, likes) across every post, re-read at most every
        `views_refresh_min` minutes and cached in the token file between runs.

        The recent tail (the last `_TAIL_IDS` message ids) is re-read in full
        on every pass, so a fresh post's views stay live while they climb.
        Older posts, whose counts have long since settled, are summed once into
        a frozen total and not read again — capping the walk at ~the tail
        length no matter how long the history is. Once a day the frozen part is
        recomputed from scratch to absorb deletions and late growth on it.
        """
        from telethon.errors import FloodWaitError

        if not self.config.get("count_views", True):
            return 0, None
        extra  = self.tokens.extra
        walked = "totals_at" in extra   # has any pass ever finished?
        cached = ((int(extra.get("views_total", 0)),
                   int(extra.get("likes_total", 0)))
                  if walked else (0, None))   # dash, not a plausible zero
        every  = int(self.config.get("views_refresh_min", 15)) * 60
        if walked and time.time() < extra.get("totals_at", 0) + every:
            return cached

        floor    = int(extra.get("frozen_below", 0))
        frozen_v = int(extra.get("frozen_views", 0))
        frozen_l = int(extra.get("frozen_likes", 0))
        # Re-read everything on the first pass and once a day, so deletions and
        # any late growth on now-frozen posts are absorbed.
        rebaseline = (not walked
                      or time.time() > extra.get("frozen_at", 0) + _FULL_WALK_EVERY)
        if rebaseline:
            floor = frozen_v = frozen_l = 0

        new_floor = floor
        tail_v = tail_l = add_v = add_l = 0
        first  = True
        try:
            # wait_time=0 keeps the bounded live tail snappy under the poll
            # lock; the daily rebaseline is an unbounded re-walk (floor=0), so
            # leave Telethon's default 1s/100-post pacing on it to stay
            # flood-safe. Floods are also caught below.
            async for msg in client.iter_messages(
                    self._entity(), min_id=floor,
                    wait_time=0 if not rebaseline else None):
                if first:                 # newest first — this is the top id
                    new_floor = max(floor, msg.id - _TAIL_IDS)
                    first = False
                v = int(getattr(msg, "views", None) or 0)
                reactions = getattr(msg, "reactions", None)
                l = (sum(int(r.count) for r in reactions.results)
                     if reactions and reactions.results else 0)
                if msg.id <= new_floor:   # slid out of the live tail → freeze
                    add_v += v
                    add_l += l
                else:
                    tail_v += v
                    tail_l += l
        except FloodWaitError as exc:
            log.warning("telegram: flood wait %ss during history walk, "
                        "reusing cached totals", exc.seconds)
            if not walked:
                raise                      # no cache yet → dash, not a solid 0
            if rebaseline:                 # an aborted re-walk backs off a day
                self.tokens.set_extra("frozen_at", time.time())
            return cached
        except Exception as exc:
            # Any other transient walk failure (connection reset, RPC/server
            # error, timeout) is just as secondary as a flood — don't blank the
            # follower count over it either.
            log.warning("telegram: history walk failed (%s), "
                        "reusing cached totals", exc)
            if not walked:
                raise
            if rebaseline:
                self.tokens.set_extra("frozen_at", time.time())
            return cached

        frozen_v += add_v
        frozen_l += add_l
        views = frozen_v + tail_v
        likes = frozen_l + tail_l
        # One write: the id boundary and its sums must never be observed apart
        # on disk (a crash between separate writes would silently under- or
        # over-count until the next daily rebaseline).
        persist = {"frozen_below": new_floor,
                   "frozen_views": frozen_v, "frozen_likes": frozen_l,
                   "views_total":  views,    "likes_total":  likes,
                   "totals_at":    time.time()}
        if rebaseline:
            persist["frozen_at"] = time.time()
        self.tokens.update_extra(persist)
        return views, likes

"""DNS-over-HTTPS fallback for hosts the system resolver refuses.

Some ISPs block a service at the name level: their resolver answers "no such
name" for graph.instagram.com while the servers themselves stay reachable —
connect to the right address with the right SNI and the API answers as usual
(seen 2026-09-28: the provider's DNS refused the name, 1.1.1.1 and 8.8.8.8
resolved it, and a request to those addresses got a normal reply). This
module gives `requests` a way through. The system resolver is asked first,
exactly as before; only when it has no answer is the name looked up over
HTTPS at a public resolver and the TCP connection made to that address. TLS
is still negotiated and verified against the real hostname — the swap
happens below it — so nothing about the security of the request changes.

    http = doh.session(setting)              # a requests.Session
    http.get("https://graph.instagram.com/v25.0/me", ...)

`setting` (a provider's "doh" option):
    ""              fallback on: system DNS, then DoH when it refuses the name
    "off" / "none"  a plain requests.Session — never resolve over HTTPS
    "https://..."   your own resolver(s), space- or comma-separated; each must
                    speak the DNS JSON API (?name=&type=), as 1.1.1.1's
                    /dns-query and 8.8.8.8's /resolve do

The built-in resolvers are addressed by IP, so reaching them needs no DNS.
Answers are cached for their TTL (bounded to 1–15 minutes), and a refusal by
the system resolver is remembered for ten minutes — it can take ten seconds
to arrive, once per new connection — so a walk of a few dozen requests costs
one lookup and no waiting. Nothing is cached for hosts the system resolver
does answer, and it gets asked again every ten minutes: within that long of
the block lifting, the fallback stands down.

No relative imports on purpose, like proxy.py: providers import this as
`from .. import doh`.
"""

from __future__ import annotations

import logging
import socket
import time
from socket import timeout as SocketTimeout

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (ConnectTimeoutError, NameResolutionError,
                                NewConnectionError)
from urllib3.util import connection as _conn

log = logging.getLogger("social.doh")

# Public resolvers speaking the DNS JSON API, by address — no DNS needed to
# reach them. Cloudflare wants the accept header; Google ignores it.
RESOLVERS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve")

_TTL_MIN, _TTL_MAX = 60, 900
_RECHECK = 600                          # how long a system-resolver refusal is believed
_A = 1                                  # DNS record type A in the JSON answer

_cache: dict = {}                       # host -> (expires_at, [addresses])
_refused: dict = {}                     # host -> (recheck_at, the gaierror)
_announced: set = set()                 # hosts whose fallback was logged already


def resolvers_for(setting: str) -> tuple | None:
    """Settings value -> resolver URLs to fall back to, or None for no
    fallback at all. Raises ValueError on a value that is neither."""
    s = (setting or "").strip()
    if s.lower() in ("off", "none", "direct"):
        return None
    if not s:
        return RESOLVERS
    urls = tuple(u for u in s.replace(",", " ").split() if u)
    if not urls or not all(u.startswith("https://") for u in urls):
        raise ValueError(f"unparseable doh setting: {setting!r}")
    return urls


def _fetch(url: str, host: str) -> dict:
    """One DNS JSON query. Separate so tests can stand in for the network."""
    r = requests.get(url, params={"name": host, "type": "A"},
                     headers={"accept": "application/dns-json"}, timeout=10)
    r.raise_for_status()
    return r.json()


def resolve(host: str, resolvers: tuple) -> list:
    """Addresses for `host`: the system resolver's, or — when it refuses the
    name — a public resolver's, over HTTPS. Raises socket.gaierror when no
    one knows the name (the system resolver's own error, so callers see what
    they always saw).

    Two clocks. A refusal is believed for `_RECHECK` seconds: it can take ten
    seconds to arrive (every configured DNS server gets its turn), and paying
    that on each new connection would stall every poll — so the system
    resolver is asked again only that often, and the fallback stands down
    within that long of the block lifting. The addresses themselves are
    re-fetched over HTTPS per their own TTL, which is cheap."""
    now = time.time()
    memo = _refused.get(host)
    if not memo or memo[0] <= now:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            _refused.pop(host, None)
            return list(dict.fromkeys(i[4][0] for i in infos))   # ordered, unique
        except socket.gaierror as exc:
            memo = _refused[host] = (now + _RECHECK, exc)
    refused = memo[1]
    hit = _cache.get(host)
    if hit and hit[0] > now:
        return hit[1]
    for url in resolvers:
        try:
            body = _fetch(url, host)
        except Exception as exc:                 # unreachable, blocked, garbage
            log.debug("doh: %s failed for %s: %s", url, host, exc)
            continue
        answers = [a for a in body.get("Answer") or [] if a.get("type") == _A]
        if not answers:
            if body.get("Status") == 3:          # NXDOMAIN there too: it's real
                break
            continue
        addrs = [a["data"] for a in answers]
        ttl = min(max(min(int(a.get("TTL") or _TTL_MAX) for a in answers),
                      _TTL_MIN), _TTL_MAX)
        _cache[host] = (time.time() + ttl, addrs)
        if host not in _announced:
            _announced.add(host)
            log.warning("%s: the system resolver refuses the name (%s); "
                        "resolving over HTTPS via %s instead", host, refused, url)
        return addrs
    raise refused


class _Connection(HTTPSConnection):
    """HTTPSConnection whose TCP connect goes through `resolve`. Everything
    above the socket — SNI, certificate check against `self.host`, the
    request itself — is urllib3's own, untouched."""

    doh_resolvers: tuple = RESOLVERS       # set per session by a subclass

    def _new_conn(self) -> socket.socket:
        try:
            addrs = resolve(self._dns_host, self.doh_resolvers)
        except socket.gaierror as e:
            raise NameResolutionError(self.host, self, e) from e
        err: Exception | None = None
        for addr in addrs:
            try:
                return _conn.create_connection(
                    (addr, self.port), self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options)
            except SocketTimeout as e:
                err = ConnectTimeoutError(
                    self, f"Connection to {self.host} timed out. "
                          f"(connect timeout={self.timeout})")
                err.__cause__ = e
            except OSError as e:
                err = NewConnectionError(
                    self, f"Failed to establish a new connection: {e}")
                err.__cause__ = e
        assert err is not None
        raise err


class _Adapter(HTTPAdapter):
    """Mounts the DoH-aware connection class on the https pools."""

    def __init__(self, resolvers: tuple, **kw):
        self._resolvers = resolvers
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        conn_cls = type("DoHConnection", (_Connection,),
                        {"doh_resolvers": self._resolvers})
        pool_cls = type("DoHConnectionPool", (HTTPSConnectionPool,),
                        {"ConnectionCls": conn_cls})
        self.poolmanager.pool_classes_by_scheme = {
            "http": HTTPConnectionPool, "https": pool_cls}


def session(setting: str = "") -> requests.Session:
    """A requests.Session with the fallback mounted for https, or a plain one
    when `setting` turns it off. Raises ValueError on a bad setting."""
    s = requests.Session()
    resolvers = resolvers_for(setting)
    if resolvers:
        s.mount("https://", _Adapter(resolvers))
    return s

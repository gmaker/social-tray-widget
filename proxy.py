"""Proxy resolution for the Telegram provider.

Direct MTProto (TCP to 149.154.x.x / 91.108.x.x) is blocked by some ISPs while
the official apps keep working — they honour the Windows system proxy. This
module finds that proxy so Telethon can use it too.

`resolve_proxy` maps the `providers.telegram.proxy` setting to what Telethon's
`proxy=` parameter takes (a python-socks style dict):

    ""                                  -> the Windows system proxy, if one is
                                           enabled (WinINET registry); else direct
    "none"                              -> force direct even with a system proxy
    "socks5://[user:pass@]host:port"    -> explicit proxy; socks4:// and
    "http://host:port"                     http:// work too

No relative imports on purpose: telegram_login.py imports this as a top-level
module, providers/telegram.py as `from ..proxy import resolve_proxy`.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional


def _windows_system_proxy() -> Optional[dict]:
    try:
        import winreg
    except ImportError:          # not on Windows
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server,  _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    if not enabled or not server:
        return None
    # Plain "host:port" applies to every protocol. The per-protocol form is
    # "http=h:p;https=h:p;socks=h:p" — prefer socks (assume SOCKS5: that is
    # what the local proxy tools that register themselves here actually run),
    # then fall back to HTTP CONNECT.
    if "=" in server:
        entries = dict(part.split("=", 1)
                       for part in server.split(";") if "=" in part)
        for scheme, ptype in (("socks", "socks5"), ("https", "http"),
                              ("http", "http")):
            if entries.get(scheme):
                return _addr(entries[scheme], ptype)
        return None
    return _addr(server, "http")


def _addr(hostport: str, ptype: str) -> Optional[dict]:
    host, _, port = hostport.strip().rpartition(":")
    if not host or not port.isdigit():
        return None
    return {"proxy_type": ptype, "addr": host, "port": int(port)}


def resolve_proxy(setting: str) -> Optional[dict]:
    """Settings value -> Telethon proxy dict, or None for a direct connection.
    Raises ValueError on an explicit value that doesn't parse — a typo should
    surface in the log, not silently fall back to direct."""
    s = (setting or "").strip()
    if s.lower() in ("none", "off", "direct"):
        return None
    if not s:
        return _windows_system_proxy()
    u = urllib.parse.urlparse(s if "://" in s else "socks5://" + s)
    ptype = {"socks5": "socks5", "socks4": "socks4",
             "http": "http", "https": "http"}.get(u.scheme)
    if not ptype or not u.hostname or not u.port:
        raise ValueError(f"unparseable proxy setting: {s!r}")
    d = {"proxy_type": ptype, "addr": u.hostname, "port": u.port}
    if u.username:
        d["username"] = u.username
        d["password"] = u.password or ""
    return d

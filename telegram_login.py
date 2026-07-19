"""One-time Telegram sign-in for the telegram provider.

The widget runs headless under pythonw and can never ask for the login code,
so the interactive part lives here. Run it from a real terminal:

    python telegram_login.py

It reads api_id/api_hash from settings.json -> providers.telegram, prompts for
the phone number, the code Telegram sends (and the 2FA password if one is
set), then writes tokens/telegram.session. If `channel` is filled in, it is
resolved once too, which caches its access hash in the session — required for
private channels, harmless for public ones. After this the widget polls on its
own and this script is never needed again — unless the session is revoked
(Telegram -> Settings -> Devices), in which case run it once more.

While this script runs, the widget stands down: `signed_in` in
tokens/telegram.json is cleared before the client opens the session file and
stamped back only after sign-in finishes, because two clients sharing one
SQLite session mid-sign-in can clobber each other's auth key.

Run it with any Python 3.10+ — it re-launches itself inside the app's own
environment (.venv), creating it and installing requirements on first use, so
nothing ever needs to be installed into the Python that happened to start it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = os.path.join(HERE, "tokens", "telegram.json")
VENV = os.path.join(HERE, ".venv")


def _reexec_in_venv() -> None:
    """Hand over to .venv's python unless we are already it, bootstrapping the
    venv on first use. The child inherits the console, so the interactive
    phone/code prompts work as usual."""
    if os.path.normcase(os.path.normpath(sys.prefix)) == \
       os.path.normcase(os.path.normpath(VENV)):
        return
    venv_py = os.path.join(VENV, "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        print("Creating the app environment (.venv) — one-time, ~a minute...")
        import venv
        venv.create(VENV, with_pip=True)
        subprocess.check_call([venv_py, "-m", "pip", "install", "-q", "-r",
                               os.path.join(HERE, "requirements.txt")])
    sys.exit(subprocess.call([venv_py, os.path.abspath(__file__)]
                             + sys.argv[1:]))


def _set_signed_in(value: bool) -> None:
    """Flip `extra.signed_in` in tokens/telegram.json, keeping the rest of the
    file (cached totals etc.) intact. Same shape TokenStore reads."""
    try:
        with open(MARKER, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    data.setdefault("extra", {})["signed_in"] = value
    with open(MARKER, "w", encoding="utf-8") as f:
        json.dump(data, f)


def main():
    _reexec_in_venv()
    # Consoles on non-English Windows default to legacy code pages (cp1251,
    # cp866...) that can't encode much of what a channel title may contain
    # (emoji). Print '?' for those instead of dying mid-run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    try:
        with open(os.path.join(HERE, "settings.json"), encoding="utf-8") as f:
            cfg = (json.load(f).get("providers") or {}).get("telegram") or {}
    except FileNotFoundError:
        sys.exit("settings.json not found — copy settings.json.example and "
                 "fill providers.telegram first")

    api_id   = str(cfg.get("api_id") or "").strip()
    api_hash = str(cfg.get("api_hash") or "").strip()
    if not api_id or not api_hash:
        sys.exit("fill providers.telegram.api_id and api_hash in settings.json "
                 "first (from https://my.telegram.org -> API development tools)")

    try:
        from telethon.sync import TelegramClient
    except ImportError:
        sys.exit("telethon is not installed — pip install telethon")

    sys.path.insert(0, HERE)
    from proxy import resolve_proxy
    try:
        proxy = resolve_proxy(cfg.get("proxy", ""))
    except ValueError as exc:
        sys.exit(str(exc))
    if proxy:
        print(f"Using proxy {proxy['proxy_type']}://"
              f"{proxy['addr']}:{proxy['port']}")

    os.makedirs(os.path.join(HERE, "tokens"), exist_ok=True)
    session = os.path.join(HERE, "tokens", "telegram")

    _set_signed_in(False)   # widget stands down while we sign in

    # `with` runs the interactive start(): phone, code, 2FA password if any.
    with TelegramClient(session, int(api_id), api_hash, proxy=proxy) as client:
        me = client.get_me()
        print(f"Signed in as {me.first_name} (@{me.username or '—'}) — "
              f"session saved to tokens/telegram.session")
        chan = str(cfg.get("channel") or "").strip()
        if chan:
            if chan.lstrip("-").isdigit():
                # A bare -100... id resolves only from the entity cache, which
                # is empty on a session this run just created. Listing the
                # dialogs stores every chat's id + access hash in the session
                # file, after which the id resolves — here and in the widget.
                client.get_dialogs()
            try:
                entity = client.get_entity(
                    int(chan) if chan.lstrip("-").isdigit() else chan)
            except ValueError:
                _set_signed_in(True)   # the sign-in itself DID succeed
                sys.exit(f"could not resolve channel {chan!r} — sign-in "
                         "succeeded and the session was saved, but this "
                         "account must be able to see the channel; fix "
                         "`channel` in settings.json and run this once more")
            print(f"Channel resolved: {getattr(entity, 'title', chan)}")

    _set_signed_in(True)    # only now may the widget open the session
    print("Done — the widget can poll Telegram now.")


if __name__ == "__main__":
    main()

"""Entry point: wire settings -> providers -> widget, then run.

Run from the project root:  python -m social_widget
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading

from .providers.instagram import InstagramProvider
from .providers.telegram import TelegramProvider
from .providers.tiktok import TikTokProvider
from .providers.vk import VKClipsProvider, VKProvider, VKVideoProvider
from .providers.youtube import YouTubeProvider
from .settings import DIR, TOKEN_DIR, load_settings, save_settings
from .tokens import TokenStore
from .widget import SocialWidget

_LOG_FILE = os.path.join(DIR, "social_widget.log")

# Tokens travel in query strings, and requests quotes the whole URL in its
# connection errors — so a provider's key would land in the log with every
# network blip (it did: a VK service key and an Instagram token, dozens of
# times). Every record is scrubbed on its way to the file.
_SECRET_RE = re.compile(r"(access_token=)[^&\s'\"]+")


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record):
        return _SECRET_RE.sub(r"\1***", super().format(record))

# Provider classes in the order they appear in the popup table.
_PROVIDER_CLASSES = [TikTokProvider, YouTubeProvider, InstagramProvider,
                     TelegramProvider, VKProvider, VKVideoProvider,
                     VKClipsProvider]


def _setup_logging():
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(filename=_LOG_FILE, level=logging.INFO, format=fmt,
                        encoding="utf-8")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_ScrubbingFormatter(fmt))
    log = logging.getLogger("social")

    # Without these, an uncaught error in any thread silently kills the app.
    def _main_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("UNCAUGHT (main)", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "?"
        log.critical("UNCAUGHT (thread=%s)", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook       = _main_hook
    threading.excepthook = _thread_hook


def build_providers(settings: dict) -> list:
    pcfg = settings.setdefault("providers", {})
    providers = []
    for cls in _PROVIDER_CLASSES:
        cfg   = pcfg.setdefault(cls.name, {})
        store = TokenStore(os.path.join(TOKEN_DIR, f"{cls.name}.json"))
        providers.append(cls(cfg, store, lambda: save_settings(settings)))
    return providers


def main():
    _setup_logging()
    settings  = load_settings()
    providers = build_providers(settings)
    SocialWidget(settings, providers).run()


if __name__ == "__main__":
    main()

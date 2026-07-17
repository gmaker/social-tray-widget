"""Entry point: wire settings -> providers -> widget, then run.

Run from the project root:  python -m social_widget
"""

from __future__ import annotations

import logging
import os
import sys
import threading

from .providers.instagram import InstagramProvider
from .providers.tiktok import TikTokProvider
from .providers.youtube import YouTubeProvider
from .settings import DIR, TOKEN_DIR, load_settings, save_settings
from .tokens import TokenStore
from .widget import SocialWidget

_LOG_FILE = os.path.join(DIR, "social_widget.log")

# Provider classes in the order they appear in the popup table.
_PROVIDER_CLASSES = [TikTokProvider, YouTubeProvider, InstagramProvider]


def _setup_logging():
    logging.basicConfig(
        filename=_LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
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

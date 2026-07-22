"""Settings load/save. One JSON file next to this package, with a nested
`providers` section. Deep-merged over defaults so a partial file still works and
new keys appear automatically after an upgrade."""

from __future__ import annotations

import copy
import json
import os

DIR           = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(DIR, "settings.json")
TOKEN_DIR     = os.path.join(DIR, "tokens")

DEFAULTS: dict = {
    "poll_interval":   60,
    "sound_enabled":   True,
    "sound_volume":    1.0,
    "sound_followers": "snd/2.wav",      # relative to this package dir
    # Accents for the two tray icons and the popup's likes total. Every colour
    # in this file is validated against the popup's #141414 surface: inside the
    # OKLCH dark lightness band (0.48-0.67), above the chroma floor, over 3:1
    # contrast, and pairwise distinct under protanopia/deuteranopia.
    "color_subs":      [57, 135, 229],   # blue
    "color_views":     [25, 158, 112],   # aqua
    "color_likes":     [201, 133, 0],    # amber
    # Fold the VK Clips row into VK Video (one combined video number). Off by
    # default — the two rows show separately; toggle it from the tray menu.
    "merge_vkvideo_clips": False,
    "providers": {
        "tiktok": {
            "enabled":       False,
            "client_key":    "",
            "client_secret": "",
            "redirect_uri":  "http://localhost:8080/callback",
            # TikTok's other brand colour. Its red is too close to YouTube's to
            # sit in the same table, and this is darkened from #25F4EE, which is
            # far too light to read on #141414.
            "color":         [23, 168, 164],
            "count_views":   True,
        },
        "youtube": {
            "enabled":           False,
            "client_id":         "",
            "client_secret":     "",
            "redirect_uri":      "http://localhost:8080/callback",
            "color":             [255, 0, 0],
            "count_likes":       True,
            "likes_refresh_min": 15,   # the uploads walk is the pricey call
        },
        "instagram": {
            "enabled":           False,
            "setup_token":       "",   # 60-day token from the Meta app
                                       # dashboard; adopted on first run and
                                       # cleared from here
            # The violet end of Instagram's gradient — its pink reads as another
            # red next to YouTube.
            "color":             [164, 91, 214],
            "count_views":       True,
            "views_refresh_min": 15,   # paging over media is the expensive call
        },
        "telegram": {
            "enabled":           False,
            "api_id":            "",   # both from my.telegram.org ->
            "api_hash":          "",   # API development tools
            "channel":           "",   # @username of the channel
            "proxy":             "",   # "" = system proxy if enabled;
                                       # "none" = direct; or socks5://h:p
            # Telegram's #229ED9 — from the brand, not the palette pipeline.
            "color":             [34, 158, 217],
            "count_views":       True,
            "views_refresh_min": 15,   # the history walk is the expensive call
        },
        "vk": {
            "enabled":           False,
            "service_token":     "",   # service key of any VK ID app
            "group":             "",   # community screen name or numeric id
            "color":             [0, 119, 255],    # VK's brand blue
            "count_views":       True,
            "views_refresh_min": 15,   # the wall walk is the expensive call
        },
        "vkvideo": {
            "enabled":           False,
            "service_token":     "",   # may be the same key as providers.vk
            "group":             "",   # the channel's backing community
            # The violet end of VK Video's gradient; the blue end is VK's row.
            "color":             [122, 133, 255],
            "count_views":       True,
            "views_refresh_min": 15,   # the video walk is the expensive call
        },
        "vkclips": {
            "enabled":           False,
            "service_token":     "",   # the same key as vk / vkvideo
            "group":             "",   # the same community
            "color":             [230, 100, 160],   # VK Clips' magenta
            "count_views":       True,
            "views_refresh_min": 15,   # the clip scan is the expensive call
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    s = copy.deepcopy(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                s = _deep_merge(s, json.load(f))
        except Exception:
            pass
    return s


def save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass

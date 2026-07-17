"""SocialWidget — the tray + Tk shell that polls every provider and shows them.

It knows nothing about TikTok/YouTube/Instagram specifics; it is handed a list
of `Provider` objects and only ever calls `enabled` / `ensure_auth` / `fetch`.

Threading rule (inherited from the original widget, the thing that stopped the
silent C-level Tcl crashes): ALL Tk lives on the main thread. Worker threads
never touch Tk — they hand work to the UI thread through `_ui_q`, drained by
`root.after()`. The poll loop runs on its own daemon thread under a supervisor.

Tray: two summary icons — total followers and total views across all enabled
platforms. Popup: a per-platform table plus the two totals.
"""

from __future__ import annotations

import logging
import os
import queue
import random
import struct
import tempfile
import threading
import time
import wave
import winsound
from typing import Optional

import tkinter as tk

import pystray
from PIL import Image, ImageDraw, ImageFont

from .providers.base import Metrics
from .settings import DIR as PKG_DIR, save_settings
from .state import load_state, save_state

log = logging.getLogger("social.widget")

# Delta ink. Both clear 4.5:1 on #141414, the bar for text this small — the
# sibling tiktok widget's red (#c0503f) only reaches 3.91 and was not reused.
_DELTA_UP   = "#4a9d5b"
_DELTA_DOWN = "#f2645a"

# The counters a row shows, in column order; also the keys in state.json.
_METRICS = ("followers", "likes", "views")

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\ariblk.ttf",
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
]


def _rgb(c) -> str:
    return "#{:02x}{:02x}{:02x}".format(*c)


# ─────────────────────────────────────────────────────────────────────────────
# Sound playback with volume scaling (unchanged from the original widget).
# ─────────────────────────────────────────────────────────────────────────────
def _play_sound(path: str, volume: float) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        if volume >= 0.99:
            winsound.PlaySound(path, winsound.SND_FILENAME)
            return
        with wave.open(path) as wf:
            params = wf.getparams()
            raw    = wf.readframes(wf.getnframes())
        if params.sampwidth == 2:
            fmt     = f"<{len(raw) // 2}h"
            samples = list(struct.unpack(fmt, raw))
            samples = [max(-32768, min(32767, int(s * volume))) for s in samples]
            raw     = struct.pack(fmt, *samples)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_name = tmp.name
        tmp.close()
        with wave.open(tmp_name, "w") as wf:
            wf.setparams(params)
            wf.writeframes(raw)
        try:
            winsound.PlaySound(tmp_name, winsound.SND_FILENAME)
        finally:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Split-flap animated value (unchanged from the original widget).
# ─────────────────────────────────────────────────────────────────────────────
class _FlipValue(tk.Frame):
    _SHUFFLES = 7
    _FRAME_MS = 50
    _STAGGER  = 35

    def __init__(self, parent, color: str, bg: str = "#141414", on_click=None):
        super().__init__(parent, bg=bg)
        self._color = color
        self._bg    = bg
        self._lbls: list = []
        self._text  = ""
        # Held rather than bound once: _rebuild() throws every label away, so a
        # binding made from outside would survive only until the value changes.
        self._on_click = on_click
        if on_click:
            self.bind("<Button-1>", on_click)

    def set_value(self, text: str, animate: bool = True):
        if text == self._text:
            return
        old, self._text = self._text, text
        if not animate or not old or len(old) != len(text):
            self._rebuild(text)
            return
        self._animate(old, text)

    def _rebuild(self, text: str):
        for lbl in self._lbls:
            lbl.destroy()
        self._lbls = []
        for ch in text:
            # Every digit is its own label so the split-flap can shuffle them
            # independently; strip the default label padding/border or each one
            # sits in a few px of dead space and the number reads "1 9 0".
            lbl = tk.Label(self, text=ch, fg=self._color, bg=self._bg,
                           font=("Segoe UI", 13, "bold"),
                           padx=0, pady=0, bd=0, highlightthickness=0)
            if self._on_click:
                lbl.bind("<Button-1>", self._on_click)
            lbl.pack(side="left")
            self._lbls.append(lbl)

    def _animate(self, old: str, new: str):
        if len(self._lbls) != len(new):
            self._rebuild(old)
        for i, (lbl, oc, nc) in enumerate(zip(self._lbls, old, new)):
            if oc != nc:
                self._flip(lbl, nc, delay=i * self._STAGGER, shuffle=nc.isdigit())

    def _flip(self, lbl, target: str, delay: int, shuffle: bool):
        frames = ([random.choice("0123456789") for _ in range(self._SHUFFLES)]
                  if shuffle else [])
        frames.append(target)

        def _step(i: int):
            if i >= len(frames):
                return
            try:
                lbl.configure(text=frames[i])
                lbl.after(self._FRAME_MS, lambda: _step(i + 1))
            except tk.TclError:
                pass

        try:
            lbl.after(delay, lambda: _step(0))
        except tk.TclError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# The widget
# ─────────────────────────────────────────────────────────────────────────────
class SocialWidget:
    def __init__(self, settings: dict, providers: list):
        self.settings   = settings
        self.providers  = providers                       # all configured, in order
        self.metrics: dict = {p.name: Metrics(ok=False) for p in providers}
        self.running    = True
        self._poll_lock = threading.Lock()
        self._prev_subs: Optional[int] = None

        self.poll_interval = int(settings.get("poll_interval", 60))
        self.color_subs    = tuple(settings.get("color_subs",  [254, 44, 85]))
        self.color_views   = tuple(settings.get("color_views", [100, 210, 130]))
        self.color_likes   = tuple(settings.get("color_likes", [235, 170, 60]))
        self.sound_enabled = bool(settings.get("sound_enabled", True))
        self.sound_volume  = float(settings.get("sound_volume", 1.0))
        snd = str(settings.get("sound_followers", ""))
        self.sound_path = snd if os.path.isabs(snd) else os.path.normpath(os.path.join(PKG_DIR, snd))

        self._muted = False
        self._font  = self._load_font()

        self._tray_subs:  Optional[pystray.Icon] = None
        self._tray_views: Optional[pystray.Icon] = None

        # Tk lives on the main thread only; everything else marshals through this.
        self._root: Optional[tk.Tk] = None
        self._ui_q: queue.Queue = queue.Queue()
        self._popup_win: Optional[tk.Toplevel] = None
        self._popup_rows: dict   = {}   # name -> {metric: (flip, delta_label)}
        self._popup_totals: dict = {}   # metric -> (flip, delta_label)

        # What each counter read when the user last clicked the popup. The delta
        # beside a value is current - baseline, so it accumulates until the next
        # click; on disk so closing the widget for a day doesn't erase it.
        self._base: dict = load_state()

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _load_font() -> Optional[ImageFont.FreeTypeFont]:
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, 1)
                except Exception:
                    pass
        return None

    @staticmethod
    def _fmt(n: int) -> str:
        if n < 1000:
            return str(n)
        if n < 10_000:
            return f"{n / 1000:.1f}K"
        if n < 1_000_000:
            return f"{n // 1000}K"
        if n < 10_000_000:
            return f"{n / 1_000_000:.1f}M"
        return f"{n // 1_000_000}M"

    def _make_icon(self, text: str, color: tuple) -> Image.Image:
        sz  = 64
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, sz - 1, sz - 1], radius=10, fill=(35, 35, 35, 240))
        font_size = {1: 56, 2: 50, 3: 38, 4: 30}.get(len(text), 22)
        font = (self._font.font_variant(size=font_size)
                if self._font else ImageFont.load_default())
        bb = d.textbbox((0, 0), text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        d.text(((sz - tw) // 2 - bb[0], (sz - th) // 2 - bb[1]),
               text, fill=color + (255,), font=font)
        return img

    def _totals(self):
        subs  = sum(m.followers for m in self.metrics.values() if m.ok)
        views = sum(m.views     for m in self.metrics.values() if m.ok)
        # `likes` is optional per platform; sum whoever reports it, and stay None
        # when nobody does so the row shows a dash instead of a bogus zero.
        counted = [m.likes for m in self.metrics.values()
                   if m.ok and m.likes is not None]
        return subs, views, (sum(counted) if counted else None)

    def _trays(self):
        return [t for t in (self._tray_subs, self._tray_views) if t]

    # ── deltas ──────────────────────────────────────────────────────────────
    def _delta(self, name: str, metric: str):
        """How far a counter has moved since it was last acknowledged, or None
        when there is nothing meaningful to show."""
        m = self.metrics.get(name)
        if not m or not m.ok:
            return None
        value = getattr(m, metric, None)
        base  = (self._base.get(name) or {}).get(metric)
        if value is None or base is None:
            return None
        return value - base

    def _total_delta(self, metric: str):
        parts = [self._delta(p.name, metric) for p in self.providers]
        moved = [d for d in parts if d is not None]
        return sum(moved) if moved else None

    def _seed_baselines(self):
        """A counter seen for the first time becomes its own baseline —
        otherwise the first poll after a fresh install would report every
        follower you have ever had as just-gained."""
        changed = False
        for p in self.providers:
            m = self.metrics.get(p.name)
            if not m or not m.ok:
                continue
            base = self._base.setdefault(p.name, {})
            for metric in _METRICS:
                value = getattr(m, metric, None)
                if value is not None and base.get(metric) is None:
                    base[metric] = value
                    changed = True
        if changed:
            save_state(self._base)

    def _reset_baselines(self, *_):
        """Clicking the popup acknowledges the numbers: rebase every counter to
        now, so the deltas collapse to zero until fresh activity arrives."""
        for p in self.providers:
            m = self.metrics.get(p.name)
            if not m or not m.ok:
                continue      # keep the old baseline for a platform that's down
            self._base[p.name] = {metric: getattr(m, metric)
                                  for metric in _METRICS
                                  if getattr(m, metric, None) is not None}
        save_state(self._base)
        self._apply_popup_values(animate=False)

    @staticmethod
    def _fmt_delta(d):
        """(text, colour). Silent when nothing moved — a row of '(+0)' is noise."""
        if not d:
            return "", _DELTA_UP
        if d > 0:
            return f"(+{d:,})", _DELTA_UP
        return f"(-{-d:,})", _DELTA_DOWN

    # ── UI thread plumbing ──────────────────────────────────────────────────
    def _post(self, fn):
        self._ui_q.put(fn)

    def _pump_ui_queue(self):
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("ui task failed")
        except queue.Empty:
            pass
        if self._root is not None and self.running:
            try:
                self._root.after(50, self._pump_ui_queue)
            except Exception:
                log.exception("ui pump reschedule failed")

    # ── polling ───────────────────────────────────────────────────────────────
    def _poll_supervisor(self):
        while self.running:
            try:
                self._poll_loop()
            except Exception:
                log.exception("poll loop crashed; restarting in 5s")
            if not self.running:
                break
            for _ in range(5):
                if not self.running:
                    return
                time.sleep(1)

    def _poll_loop(self):
        while self.running:
            self._poll_once()
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def _poll_once(self):
        # Serialised: the supervisor loop and a manual "Refresh now" must not run
        # two interactive auths against the same localhost port at once.
        with self._poll_lock:
            for p in self.providers:
                if not self.running:
                    return
                if not p.enabled:
                    self.metrics[p.name] = Metrics(ok=False, error="disabled")
                    continue
                try:
                    if not p.ensure_auth():
                        self.metrics[p.name] = Metrics(ok=False, error="not authorised")
                        continue
                    self.metrics[p.name] = p.fetch()
                except Exception as exc:
                    log.exception("%s fetch failed", p.name)
                    self.metrics[p.name] = Metrics(ok=False, error=str(exc)[:60])
        self._seed_baselines()
        self._post(self._refresh_ui)

    # ── rendering (UI thread) ───────────────────────────────────────────────
    def _refresh_ui(self):
        # Likes live in the popup table only — the tray keeps its two icons.
        subs, views, _ = self._totals()
        if self._tray_subs:
            self._tray_subs.icon  = self._make_icon(self._fmt(subs), self.color_subs)
            self._tray_subs.title = f"Followers (total): {subs:,}"
        if self._tray_views:
            self._tray_views.icon  = self._make_icon(self._fmt(views), self.color_views)
            self._tray_views.title = f"Views (total): {views:,}"
        self._apply_popup_values()
        self._maybe_sound(subs)

    def _maybe_sound(self, subs: int):
        if (self._prev_subs is not None and subs > self._prev_subs
                and self.sound_enabled and not self._muted):
            threading.Thread(target=_play_sound,
                             args=(self.sound_path, self.sound_volume),
                             daemon=True).start()
        self._prev_subs = subs

    # ── popup ─────────────────────────────────────────────────────────────────
    def _show_popup(self):
        # Menu callbacks fire on pystray's thread — marshal to the UI thread.
        self._post(self._toggle_popup)

    def _toggle_popup(self):
        if self._popup_win is not None:
            try:
                self._popup_win.destroy()
            except Exception:
                pass
            self._popup_win, self._popup_rows = None, {}
            self._popup_totals = {}
            return
        if self._root is None:
            return
        self._build_popup()

    def _build_popup(self):
        win = tk.Toplevel(self._root)
        win.report_callback_exception = lambda *a: log.error("Tk callback error", exc_info=a)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#141414")

        border = tk.Frame(win, bg="#383838", padx=1, pady=1)
        border.pack(fill="both", expand=True)
        body = tk.Frame(border, bg="#141414", padx=14, pady=12)
        body.pack(fill="both", expand=True)

        def head(text, col):
            tk.Label(body, text=text, fg="#5a5a5a", bg="#141414",
                     font=("Segoe UI", 8)).grid(row=0, column=col, padx=(18, 0), sticky="e")

        head("FOLLOWERS", 1)
        head("LIKES", 2)
        head("VIEWS", 3)

        self._popup_rows = {}
        r = 1
        for p in self.providers:
            col = _rgb(p.color)
            tk.Label(body, text=p.label, fg=col, bg="#141414",
                     font=("Segoe UI", 10, "bold"), anchor="w").grid(
                row=r, column=0, sticky="w", pady=3)
            # Colour encodes the metric, not the platform: a column is one hue
            # top to bottom, TOTAL included. The platform name is what carries
            # identity — painting its numbers too made three saturated hues
            # fight across every row.
            self._popup_rows[p.name] = {
                metric: self._cell(body, _rgb(ink), r, i)
                for i, (metric, ink) in enumerate(self._metric_inks(), start=1)
            }
            r += 1

        tk.Frame(body, bg="#2a2a2a", height=1).grid(
            row=r, column=0, columnspan=4, sticky="ew", pady=(7, 7))
        r += 1

        tk.Label(body, text="TOTAL", fg="#cccccc", bg="#141414",
                 font=("Segoe UI", 10, "bold"), anchor="w").grid(row=r, column=0, sticky="w")
        self._popup_totals = {
            metric: self._cell(body, _rgb(ink), r, i)
            for i, (metric, ink) in enumerate(self._metric_inks(), start=1)
        }

        self._popup_win = win
        self._apply_popup_values(animate=False)
        self._position_popup()
        win.bind("<Escape>", lambda e: self._toggle_popup())
        # Clicking anywhere in the popup acknowledges every counter. Bind the
        # whole tree — a click lands on whichever label is under the cursor, not
        # on the window. Labels _FlipValue builds later carry their own binding.
        self._bind_reset(win)

    def _metric_inks(self):
        """Each counter and its column colour, in display order."""
        return zip(_METRICS, (self.color_subs, self.color_likes,
                              self.color_views))

    def _cell(self, parent, ink: str, row: int, column: int):
        """One value plus the delta that trails it, as a single grid cell."""
        wrap = tk.Frame(parent, bg="#141414")
        wrap.grid(row=row, column=column, sticky="e", padx=(18, 0))
        flip = _FlipValue(wrap, ink, on_click=self._reset_baselines)
        flip.pack(side="left")
        delta = tk.Label(wrap, text="", fg=_DELTA_UP, bg="#141414",
                         font=("Segoe UI", 8, "bold"))
        delta.pack(side="left", padx=(4, 0))
        return flip, delta

    def _bind_reset(self, widget):
        widget.bind("<Button-1>", self._reset_baselines)
        for child in widget.winfo_children():
            self._bind_reset(child)

    def _position_popup(self):
        # Re-fit the window to its content and re-anchor it to the bottom-right
        # corner. Called on every value update so the window grows when real
        # numbers replace the "—" placeholders instead of clipping the VIEWS
        # column off the right screen edge.
        win = self._popup_win
        if not win:
            return
        try:
            win.update_idletasks()
            w  = max(win.winfo_reqwidth(), 260)
            h  = win.winfo_reqheight()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            win.geometry(f"{w}x{h}+{sw - w - 10}+{sh - h - 52}")
        except Exception:
            log.exception("popup reposition failed")

    def _apply_popup_values(self, animate: bool = True):
        if not self._popup_win:
            return
        for name, cells in self._popup_rows.items():
            m = self.metrics.get(name) or Metrics(ok=False)
            for metric, (flip, delta) in cells.items():
                value = getattr(m, metric, None) if m.ok else None
                try:
                    flip.set_value(f"{value:,}" if value is not None else "—",
                                   animate)
                    self._show_delta(delta, self._delta(name, metric))
                except Exception:
                    log.exception("popup row update failed")

        subs, views, likes = self._totals()
        for metric, total in (("followers", subs), ("likes", likes),
                              ("views", views)):
            cell = self._popup_totals.get(metric)
            if not cell:
                continue
            flip, delta = cell
            try:
                flip.set_value(f"{total:,}" if total is not None else "—",
                               animate)
                self._show_delta(delta, self._total_delta(metric))
            except Exception:
                log.exception("popup totals update failed")
        # Once, after every cell is settled: deltas appear and disappear, so the
        # window has to refit or a fresh "(+296)" is clipped off the right edge.
        self._position_popup()

    def _show_delta(self, label, d):
        text, colour = self._fmt_delta(d)
        label.configure(text=text, fg=colour)

    # ── menu ────────────────────────────────────────────────────────────────
    def _make_toggle(self, p):
        def _t(icon, item):
            p.config["enabled"] = not p.config.get("enabled", False)
            save_settings(self.settings)
            for t in self._trays():
                t.update_menu()
            threading.Thread(target=self._poll_once, daemon=True).start()
        return _t

    def _build_menu(self):
        prov_items = [
            pystray.MenuItem(p.label, self._make_toggle(p),
                             checked=(lambda pr: lambda item: bool(pr.config.get("enabled")))(p))
            for p in self.providers
        ]

        def on_mute(icon, _):
            self._muted = not self._muted
            for t in self._trays():
                t.update_menu()

        def on_refresh(icon, _):
            threading.Thread(target=self._poll_once, daemon=True).start()

        def on_exit(icon, _):
            self.running = False
            for t in self._trays():
                t.stop()
            self._post(lambda: self._root.quit() if self._root else None)

        return pystray.Menu(
            pystray.MenuItem("Details", lambda i, it: self._show_popup(),
                             default=True, visible=False),
            pystray.MenuItem("Platforms", pystray.Menu(*prov_items)),
            pystray.MenuItem(lambda _: "Sound: OFF" if self._muted else "Sound: ON",
                             on_mute, checked=lambda _: not self._muted),
            pystray.MenuItem("Refresh now", on_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit),
        )

    # ── entry point ───────────────────────────────────────────────────────────
    def run(self):
        menu = self._build_menu()
        self._tray_subs  = pystray.Icon("social_followers",
                                        self._make_icon("...", self.color_subs),
                                        "Followers: loading...", menu)
        self._tray_views = pystray.Icon("social_views",
                                        self._make_icon("...", self.color_views),
                                        "Views: loading...", menu)

        threading.Thread(target=self._poll_supervisor, daemon=True, name="poll").start()

        self._tray_subs.run_detached()
        self._tray_views.run_detached()

        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *a: log.error("Tk callback error", exc_info=a)
        self._root = root

        log.info("social widget started")
        self._pump_ui_queue()
        try:
            root.mainloop()
        except Exception:
            log.exception("Tk main loop crashed")
            raise
        finally:
            self.running = False
            for t in self._trays():
                try:
                    t.stop()
                except Exception:
                    pass
            log.info("social widget stopped")

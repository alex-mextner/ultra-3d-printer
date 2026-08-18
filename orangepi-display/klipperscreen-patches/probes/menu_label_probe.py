#!/usr/bin/env python3
"""Offscreen label-wrap audit for KlipperScreen menus on this 320x240 panel.

Builds the SAME widgets menu.py builds -- real KlippyGtk.Button (so real
img_scale/scale/format_label with WrapMode.WORD_CHAR), real AutoGrid, real
base.css + theme style.css, real 320x240 geometry -- inside a
Gtk.OffscreenWindow. The window is never mapped, so nothing appears on the
physical panel while a print is running.

Reports the actual per-LINE texts Pango produced, so "Z Calibrate" wrapping
cleanly at the space and THEN char-breaking "Calibrate" on line 2 is visible
(a fits/doesn't boolean would hide it). Also dumps a PNG of each rendered
menu via OffscreenWindow.get_pixbuf().

usage: menu_label_probe.py <outdir> <theme> <menus.json>
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.expanduser("~/KlipperScreen"))

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from ks_includes.config import KlipperScreenConfig  # noqa: E402
from ks_includes.KlippyGtk import KlippyGtk, find_widget  # noqa: E402
from ks_includes.widgets.autogrid import AutoGrid  # noqa: E402

KSDIR = os.path.expanduser("~/KlipperScreen")
WIDTH, HEIGHT = 320, 240
OUTDIR = sys.argv[1]
THEME = sys.argv[2] if len(sys.argv) > 2 else "big-font-light"


class StubScreen:
    """Just enough of KlipperScreen for KlipperScreenConfig + KlippyGtk.

    config.py only ever stores screen.<name> as a *callback* it never calls
    during construction, so a no-op for every attribute is faithful here.
    """

    def __init__(self):
        self._config = None
        self.width = WIDTH
        self.height = HEIGHT
        self.vertical_mode = False
        self.theme = THEME

    def __getattr__(self, name):
        return _Nop()


class _Nop:
    """Callable that also answers any attribute with another _Nop."""

    def __call__(self, *a, **k):
        return None

    def __getattr__(self, name):
        return _Nop()


def load_styles(gtk_helper):
    with open(os.path.join(KSDIR, "styles", "base.conf")) as f:
        style_options = json.load(f)
    gtk_helper.color_list = style_options["graph_colors"]
    base_css = pathlib.Path(os.path.join(KSDIR, "styles", "base.css")).read_text()
    base_css = base_css.replace("KS_FONT_SIZE", f"{gtk_helper.font_size}")
    theme_css_path = os.path.join(KSDIR, "styles", THEME, "style.css")
    theme_css = pathlib.Path(theme_css_path).read_text() if os.path.exists(theme_css_path) else ""
    provider = Gtk.CssProvider()
    provider.load_from_data((base_css + theme_css).encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def label_lines(label):
    layout = label.get_layout()
    text = layout.get_text()
    out = []
    it = layout.get_iter()
    while True:
        line = it.get_line_readonly()
        start = line.start_index
        out.append(text[start : start + line.length])
        if not it.next_line():
            break
    return out, layout.is_ellipsized()


def classify(lines, ellipsized):
    if ellipsized:
        return "ELLIPSIZED"
    if len(lines) == 1:
        return "ok"
    joined = "".join(lines)
    pos = 0
    for ln in lines[:-1]:
        pos += len(ln)
        if 0 < pos < len(joined) and joined[pos - 1] != " " and joined[pos] != " ":
            return "MID-WORD"
    return "wraps-at-space"


def render(gtk_helper, title, entries, columns, scale, png, layout="grid"):
    """entries: list of (key, label, icon). Renders them in a `columns`-wide grid.

    layout="box" reproduces splash_screen.py, whose action row is a
    homogeneous Gtk.Box across the full content width -- NOT an AutoGrid.
    That distinction matters: AutoGrid folds 4 items into 2x2, the Box keeps
    them 1x4, which is half the width per button.
    """
    buttons = []
    for i, (key, name, icon) in enumerate(entries):
        b = gtk_helper.Button(icon, name, f"color{i % 4 + 1}", scale=scale)
        buttons.append((key, name, b))

    if layout == "box":
        grid = Gtk.Box(hexpand=True, vexpand=False, homogeneous=True)
        for _, _, b in buttons:
            grid.add(b)
    else:
        grid = AutoGrid([b for _, _, b in buttons], columns, False, False)
    scroll = gtk_helper.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.add(grid)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
    content.get_style_context().add_class("content")
    content.add(scroll)

    action_bar = Gtk.Box(spacing=5, orientation=Gtk.Orientation.VERTICAL)
    action_bar.get_style_context().add_class("action_bar")
    action_bar.set_size_request(gtk_helper.action_bar_width, gtk_helper.action_bar_height)
    action_bar.set_hexpand(False)
    action_bar.set_vexpand(True)

    titlebar = Gtk.Box(spacing=5, valign=Gtk.Align.CENTER)
    titlebar.get_style_context().add_class("title_bar")
    titlebar.add(Gtk.Label(label=title, hexpand=True, halign=Gtk.Align.CENTER))

    main_grid = Gtk.Grid()
    main_grid.get_style_context().add_class("main_grid")
    main_grid.attach(titlebar, 1, 0, 1, 1)
    main_grid.attach(content, 1, 1, 1, 1)
    main_grid.attach(action_bar, 0, 0, 1, 2)

    win = Gtk.OffscreenWindow()
    win.set_default_size(WIDTH, HEIGHT)
    win.set_size_request(WIDTH, HEIGHT)
    win.add(main_grid)
    win.show_all()
    for _ in range(40):
        while Gtk.events_pending():
            Gtk.main_iteration()

    results = []
    for key, name, b in buttons:
        lbl = find_widget(b, Gtk.Label)
        lines, ell = label_lines(lbl)
        results.append(
            {
                "key": key,
                "name": name,
                "btn_w": b.get_allocation().width,
                "lbl_w": lbl.get_allocation().width,
                "lines": lines,
                "verdict": classify(lines, ell),
            }
        )

    if png:
        pb = win.get_pixbuf()
        if pb is not None:
            pb.savev(os.path.join(OUTDIR, png), "png", [], [])
    win.destroy()
    return results


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    screen = StubScreen()
    config = KlipperScreenConfig(
        os.path.expanduser("~/printer_data/config/KlipperScreen.conf"), screen
    )
    screen._config = config
    gtk_helper = KlippyGtk(screen)
    load_styles(gtk_helper)
    s = Gtk.Settings.get_default()
    s.set_property("gtk-theme-name", "Adwaita")
    s.set_property("gtk-application-prefer-dark-theme", False)

    shortcut = config.get_main_config().get("side_shortcut_target", fallback="notifications")
    print(f"# theme={THEME} font_size={gtk_helper.font_size:.2f} img_scale={gtk_helper.img_scale:.1f}")
    print(f"# action_bar_width={gtk_helper.action_bar_width} content_width={gtk_helper.content_width}")
    print(f"# side_shortcut_target={shortcut!r}\n")

    with open(sys.argv[3] if len(sys.argv) > 3 else "menus.json") as f:
        plan = json.load(f)

    worst = {}
    for m in plan:
        entries = [(e["key"], e["name"], e["icon"]) for e in m["entries"]]
        res = render(
            gtk_helper,
            m["title"],
            entries,
            m["columns"],
            m.get("scale"),
            m.get("png"),
            m.get("layout", "grid"),
        )
        print(f"=== {m['id']}  ({len(entries)} buttons, {m['columns']} columns) ===")
        for r in res:
            mark = {"ok": "     ", "wraps-at-space": "  ~  ", "MID-WORD": " !!! ", "ELLIPSIZED": " !!! "}[
                r["verdict"]
            ]
            print(
                f"{mark}{r['name']!r:24} btn={r['btn_w']:3} lbl={r['lbl_w']:3} "
                f"{r['verdict']:14} {r['lines']}"
            )
            rank = {"ok": 0, "wraps-at-space": 1, "MID-WORD": 2, "ELLIPSIZED": 3}
            prev = worst.get(r["name"])
            if prev is None or rank[r["verdict"]] > rank[prev[0]]:
                worst[r["name"]] = (r["verdict"], m["id"], r["lines"])
        print()

    print("=== WORST VERDICT PER LABEL ===")
    for name, (v, where, lines) in sorted(worst.items(), key=lambda kv: kv[1][0]):
        if v != "ok":
            print(f"  {name!r:24} {v:14} in {where:16} {lines}")


main()

#!/usr/bin/env python3
"""Measure the natural pixel width of button-label text, in the real theme font.

A mid-word char break happens when a SINGLE WORD is wider than the label's
allocated width -- wrapping at spaces is free. So the complete test for the
"Temper-ature" defect class over every label in the tree is: measure each
word, compare against the per-column label width measured live by
menu_label_probe.py.

Reads one label string per line on stdin, prints:
    <widest word px>  <whole string px>  <label>
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.expanduser("~/KlipperScreen"))

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from ks_includes.config import KlipperScreenConfig  # noqa: E402
from ks_includes.KlippyGtk import KlippyGtk, find_widget  # noqa: E402

KSDIR = os.path.expanduser("~/KlipperScreen")
WIDTH, HEIGHT = 320, 240
THEME = os.environ.get("KS_THEME", "big-font-light")


class _Nop:
    def __call__(self, *a, **k):
        return None

    def __getattr__(self, n):
        return _Nop()


class StubScreen:
    def __init__(self):
        self._config = None
        self.width, self.height = WIDTH, HEIGHT
        self.vertical_mode = False
        self.theme = THEME

    def __getattr__(self, n):
        return _Nop()


screen = StubScreen()
config = KlipperScreenConfig(os.path.expanduser("~/printer_data/config/KlipperScreen.conf"), screen)
screen._config = config
gtk_helper = KlippyGtk(screen)

with open(os.path.join(KSDIR, "styles", "base.conf")) as f:
    gtk_helper.color_list = json.load(f)["graph_colors"]
base_css = pathlib.Path(os.path.join(KSDIR, "styles", "base.css")).read_text()
base_css = base_css.replace("KS_FONT_SIZE", f"{gtk_helper.font_size}")
tp = os.path.join(KSDIR, "styles", THEME, "style.css")
theme_css = pathlib.Path(tp).read_text() if os.path.exists(tp) else ""
prov = Gtk.CssProvider()
prov.load_from_data((base_css + theme_css).encode())
Gtk.StyleContext.add_provider_for_screen(
    Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
)
s = Gtk.Settings.get_default()
s.set_property("gtk-theme-name", "Adwaita")
s.set_property("gtk-application-prefer-dark-theme", False)

labels = [ln.rstrip("\n") for ln in sys.stdin if ln.strip()]

win = Gtk.OffscreenWindow()
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
box.get_style_context().add_class("content")
win.add(box)
made = []
for text in labels:
    b = gtk_helper.Button("move", text, "color1")
    box.add(b)
    made.append((text, b))
win.show_all()
while Gtk.events_pending():
    Gtk.main_iteration()

print(f"# theme={THEME}  (widest-word px, whole-string px)")
for text, b in made:
    lbl = find_widget(b, Gtk.Label)
    whole = lbl.get_preferred_width()[1]
    widest = 0
    widest_word = ""
    for word in text.split():
        lbl.set_label(word)
        w = lbl.get_preferred_width()[1]
        if w > widest:
            widest, widest_word = w, word
    lbl.set_label(text)
    print(f"{widest:4}  {whole:4}  {text!r}   widest word {widest_word!r}")

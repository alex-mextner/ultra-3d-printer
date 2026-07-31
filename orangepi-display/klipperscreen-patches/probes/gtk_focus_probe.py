#!/usr/bin/env python3
"""Prove the exact GTK3 focus semantics behind the dead-arrow-keys bug.

Replicates KlipperScreen's action bar in miniature: a persistent Button that
is focused by the user and then made insensitive by update_action_bar() when
the panel depth drops back to 1. Answers three questions with measurements,
not reasoning:

  Q1 What does Gtk.Window.get_focus() return after the FOCUSED widget is made
     insensitive?  (dead widget, or None?)
  Q2 Does the widget still report get_mapped() == True at that point?  That is
     the ONLY thing screen.py's resume-from-idle guard checks.
  Q3 Does Gtk.Window.set_focus(<that widget>) actually take effect?

Read-only with respect to KlipperScreen: builds its own throwaway window.
"""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

win = Gtk.Window()
box = Gtk.Box()
# "back" stands in for base_panel.control["back"] -- a persistent action-bar
# button that outlives every panel and is only ever toggled sensitive.
back = Gtk.Button(label="back")
other = Gtk.Button(label="other")
box.add(back)
box.add(other)
win.add(box)
win.show_all()

while Gtk.events_pending():
    Gtk.main_iteration()


def state(w):
    return (
        f"mapped={w.get_mapped()} sensitive={w.is_sensitive()} "
        f"can_focus={w.get_can_focus()} toplevel_is_win={w.get_toplevel() is win}"
    )


def focus_label():
    f = win.get_focus()
    return "None" if f is None else f"{type(f).__name__}({f.get_label()})"


print(f"gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")

# --- step 1: user arrows onto the on-screen back button ---------------------
win.set_focus(back)
while Gtk.events_pending():
    Gtk.main_iteration()
print(f"1. after set_focus(back)          : focus={focus_label()}")
assert win.get_focus() is back, "precondition failed: could not focus a sensitive button"

# --- step 2: Enter -> back() -> _menu_go_back() -> attach_panel() ->
#     add_content() -> update_action_bar() -> set_control_sensitive(False)
back.set_sensitive(False)
while Gtk.events_pending():
    Gtk.main_iteration()
print(f"2. after back.set_sensitive(False): focus={focus_label()}   <-- Q1")
print(f"   the button itself              : {state(back)}   <-- Q2")

# --- step 3: every later arrow press hits screen.py's resume branch, which
#     checks ONLY get_mapped() and then claims handled=True.
guard_passes = back.get_mapped()
print(f"3. resume guard get_mapped()      : {guard_passes}  (guard would proceed)")
win.set_focus(back)
while Gtk.events_pending():
    Gtk.main_iteration()
print(f"   after set_focus(back) again    : focus={focus_label()}   <-- Q3")

took = win.get_focus() is back
print()
print(f"VERDICT: guard_passes={guard_passes} set_focus_took_effect={took}")
if guard_passes and not took:
    print("VERDICT: CONFIRMED -- guard passes, set_focus silently does nothing,")
    print("         so handled=True is claimed while focus stays None forever.")
else:
    print("VERDICT: NOT confirmed -- hypothesis is wrong, investigate further.")

# --- step 4: control -- does re-sensitising restore focusability? -----------
back.set_sensitive(True)
win.set_focus(back)
while Gtk.events_pending():
    Gtk.main_iteration()
print(f"4. control, re-sensitised         : focus={focus_label()} (expect back)")

win.destroy()

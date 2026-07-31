#!/usr/bin/env python3
"""Verify the FIX logic for the dead-arrow-keys bug against the real GTK.

Models KlipperScreen's layout: an action bar with a persistent "back" button
(insensitive at panel depth 1) plus a panel content area with focusable rows.
Runs the exact predicates the patched screen.py uses, so the patch is checked
against GTK's actual behaviour and not against reasoning about it.
"""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

win = Gtk.Window()
root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
action_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
back = Gtk.Button(label="back")
home = Gtk.Button(label="home")
action_bar.add(back)
action_bar.add(home)
# content == base_panel.content, the box panels get attached into
content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
rows = [Gtk.Button(label=f"row{i}") for i in range(3)]
for r in rows:
    content.add(r)
root.add(action_bar)
root.add(content)
win.add(root)
win.show_all()


def pump():
    while Gtk.events_pending():
        Gtk.main_iteration()


pump()


def can_take_focus(widget):
    """Exact copy of the patched screen.py's _can_take_focus()."""
    if widget is None:
        return False
    try:
        return bool(
            widget.get_mapped()
            and widget.is_sensitive()
            and widget.get_can_focus()
            and widget.get_toplevel() is win
        )
    except Exception:
        return False


def focus_into_content(keyval_name="Down"):
    """Exact copy of the patched screen.py's _focus_into_current_panel()."""
    pressed = {
        "Up": Gtk.DirectionType.UP,
        "Down": Gtk.DirectionType.DOWN,
        "Left": Gtk.DirectionType.LEFT,
        "Right": Gtk.DirectionType.RIGHT,
    }.get(keyval_name, Gtk.DirectionType.DOWN)
    for direction in (pressed, Gtk.DirectionType.TAB_FORWARD):
        if content.child_focus(direction) and win.get_focus() is not None:
            return True
    return False


def flabel():
    f = win.get_focus()
    return "None" if f is None else f"{type(f).__name__}({f.get_label()})"


fails = []

# --- reproduce the wedge ----------------------------------------------------
win.set_focus(back)
pump()
last_focus_widget = win.get_focus()          # what _on_set_focus() caches
back.set_sensitive(False)                    # update_action_bar() at depth 1
pump()
print(f"after back goes insensitive : focus={flabel()}  last_focus_widget=back")

# --- OLD guard --------------------------------------------------------------
old_guard = back.get_mapped()
win.set_focus(last_focus_widget)
pump()
old_handled = True                           # old code claimed it always
print(f"OLD guard get_mapped()={old_guard} -> set_focus -> focus={flabel()}, "
      f"handled={old_handled}  => KEY EATEN, focus still None")
if not (old_guard and win.get_focus() is None and old_handled):
    fails.append("old-guard reproduction did not wedge as expected")

# --- NEW guard --------------------------------------------------------------
widget = last_focus_widget
new_guard = can_take_focus(widget)
print(f"NEW _can_take_focus(back)    = {new_guard}  (expect False -> target dropped)")
if new_guard:
    fails.append("_can_take_focus wrongly accepted the insensitive button")

if not new_guard:
    widget = None
handled = False
if widget is not None:
    win.set_focus(widget)
    pump()
    handled = win.get_focus() is not None
if not handled:
    handled = focus_into_content("Down")
    pump()
print(f"NEW fallback focus-into-panel: focus={flabel()}, handled={handled}  (expect a row)")
if not handled or win.get_focus() not in rows:
    fails.append("focus_into_current_panel did not land inside the panel content")

# --- arrows must keep working from there ------------------------------------
before = win.get_focus()
win.child_focus(Gtk.DirectionType.DOWN)
pump()
print(f"then Down                    : {flabel()}  (expect a different row)")
if win.get_focus() is before:
    fails.append("directional navigation did not move after the fix")

# --- control: a still-valid widget must STILL be restored --------------------
win.set_focus(rows[1])
pump()
cached = win.get_focus()
win.set_focus(None)
pump()
print(f"control, valid cached widget : _can_take_focus(row1)={can_take_focus(cached)} "
      f"(expect True)")
if not can_take_focus(cached):
    fails.append("regression: a perfectly valid cached widget would now be dropped")
win.set_focus(cached)
pump()
if win.get_focus() is not cached:
    fails.append("regression: valid widget was not restored")

# --- control: an unparented widget must be rejected -------------------------
content.remove(rows[2])
pump()
print(f"control, unparented widget   : _can_take_focus(row2)={can_take_focus(rows[2])} "
      f"(expect False)")
if can_take_focus(rows[2]):
    fails.append("_can_take_focus accepted a detached widget")

print()
print("RESULT: ALL CHECKS PASSED" if not fails else f"RESULT: FAILURES: {fails}")
win.destroy()

#!/usr/bin/env python3
"""Does focus established via content.child_focus() still navigate afterwards?

The fix probe flagged "Down did not move" after the focus-into-panel fallback.
That is only a real defect if focus established the ORDINARY way navigates
fine in the same harness. So run both side by side:

  A. focus set with win.set_focus(row0)          <- the known-good baseline
  B. focus set with content.child_focus(DOWN)    <- what the patch does

and drive each with the same two mechanisms GTK itself uses for an arrow key
("move-focus" signal, then widget child_focus on the toplevel). If A and B
behave identically, the flagged failure is a harness artifact, not a
regression introduced by the patch.
"""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


def build():
    win = Gtk.Window()
    root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    bar.add(Gtk.Button(label="back"))
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    rows = [Gtk.Button(label=f"row{i}") for i in range(3)]
    for r in rows:
        content.add(r)
    root.add(bar)
    root.add(content)
    win.add(root)
    win.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration()
    return win, content, rows


def pump():
    while Gtk.events_pending():
        Gtk.main_iteration()


def flabel(win):
    f = win.get_focus()
    return "None" if f is None else f.get_label()


for mode in ("A: set_focus(row0)", "B: content.child_focus(DOWN)"):
    win, content, rows = build()
    if mode.startswith("A"):
        win.set_focus(rows[0])
    else:
        content.child_focus(Gtk.DirectionType.DOWN)
    pump()
    start = flabel(win)

    win.emit("move-focus", Gtk.DirectionType.DOWN)
    pump()
    after_signal = flabel(win)

    win.child_focus(Gtk.DirectionType.DOWN)
    pump()
    after_child_focus = flabel(win)

    print(f"{mode:32} start={start:5} after move-focus={after_signal:5} "
          f"after child_focus={after_child_focus:5}")
    win.destroy()

print()
print("If A and B match, the earlier 'did not move' is a harness artifact:")
print("both establish an equally navigable focus state.")

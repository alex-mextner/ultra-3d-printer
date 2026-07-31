# QR code rendering for KlipperScreen panels.
# Added 2026-07-31 for the Network panel: reading a dotted quad off a 2.2"
# 320x240 SPI panel and retyping it into a phone is miserable, so the printer's
# web UI URL is also drawn as a QR code the user can simply scan.
#
# Encoder: segno -- pure Python, no C extension, no build dependencies, 76 kB
# wheel. That matters here: this runs on an Orange Pi One (H3, 512 MB RAM, no
# swap headroom) where anything that needs a compiler at install time is a
# non-starter. Declared in scripts/KlipperScreen-requirements.txt so a future
# KlipperScreen update reinstalls it instead of silently breaking this panel.
#
# COLOUR POLICY -- deliberate, do not "fix" for the dark theme: the symbol is
# always BLACK modules on a WHITE field with a full 4-module quiet zone, no
# matter which KlipperScreen theme is active. Phone scanners assume
# dark-on-light, and an inverted symbol with a themed (i.e. missing) quiet zone
# is the most common reason a QR that looks fine on screen refuses to decode.
# Only the symbol's own bounding box is white; the panel around it stays themed.
import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

try:
    import segno
except ImportError:  # pragma: no cover - the panel must survive this
    # The Network panel is the user's only on-device way to find the IP, so a
    # missing encoder degrades to "no QR", never to a traceback.
    segno = None
    logging.warning("segno is not installed, QR codes will not be rendered")

# ISO/IEC 18004 quiet zone. Never trade this away for a bigger symbol: a
# truncated quiet zone kills scanning far more often than a small module does.
QUIET_ZONE = 4
# Floor for the widget's size request, NOT the size it draws at. The drawn
# module size is computed from the real allocation (see draw_qr), so given room
# this grows to 4 px/module = 132x132 for http://192.168.11.160/ (22 bytes ->
# version 2, 25x25 modules, 33x33 with the quiet zone) and falls back to 3
# px/module = 99x99 only if the allocation ever drops under 132 px -- that
# fallback is exercised offscreen at 100x100, not on this panel. Asking for 132 outright made
# the Network panel overflow its 320x240 viewport by 10 px and the SCREEN EDGE
# clipped the bottom of the quiet zone -- measured 2026-07-31, symbol at
# top=118 with only 120 of 132 rows on screen.
MIN_MODULE_PX = 3


def web_ui_url(ip, port=80):
    """URL a phone should open to reach this printer's web interface.

    Mainsail answers over plain HTTP on port 80 -- measured 2026-07-31 from the
    workstation on the same LAN (which is exactly what a phone does):
    `curl -o /dev/null -w '%{http_code}' http://192.168.11.160/` -> 200.
    Built from the live address on every refresh so a new DHCP lease is picked
    up rather than baked in. Returns None for the "?" no-address sentinel that
    SdbusNm.get_ip_for_interface() returns, so callers can hide the widget.
    """
    if not ip or ip == "?":
        return None
    if ":" in ip:  # IPv6 literal needs brackets in a URL
        ip = f"[{ip}]"
    return f"http://{ip}/" if port == 80 else f"http://{ip}:{port}/"


def qr_matrix(data, error="l"):
    """Encode data and return the symbol as a list of rows of 0/1, no border.

    Asks for ECC level L because every step up in error correction costs
    modules, i.e. physical module size on a 132 px square. Note segno's
    boost_error is on by default, so when the payload leaves room it silently
    upgrades: http://192.168.11.160/ is 22 bytes and version 2 holds 26 at
    level M, so the symbol actually produced is 25x25 at M -- same size, more
    robustness. Verified by decoding the rendered symbol (format bits -> M,
    mask 5). Returns None (never raises) if segno is missing or encoding fails.
    """
    if segno is None or not data:
        return None
    try:
        qr = segno.make(data, error=error, micro=False)
        return [list(row) for row in qr.matrix]
    except Exception:
        logging.exception(f"Failed to encode QR code for {data}")
        return None


def draw_qr(ctx, matrix, width, height, border=QUIET_ZONE):
    """Draw matrix centred inside a width x height box of the cairo context.

    Returns (module_px, origin_x, origin_y, symbol_px) or None if it could not
    be drawn. The module size is floored to a WHOLE number of pixels and the
    origin to whole pixels too: a fractional module makes cairo antialias every
    module edge, and a blurry 4 px module on a 320x240 SPI panel is
    unscannable. Better a slightly smaller crisp symbol than a fuzzy big one.
    """
    if not matrix:
        return None
    # Module count comes from the matrix, not a constant: a longer URL (or a
    # hostname instead of an IP) silently bumps the version, e.g. v3 = 29+8.
    modules = len(matrix) + 2 * border
    module_px = int(min(width, height)) // modules
    if module_px < 1:
        return None
    symbol_px = module_px * modules
    origin_x = int(width - symbol_px) // 2
    origin_y = int(height - symbol_px) // 2

    # White field first: this IS the quiet zone, it must be painted, not just
    # left as whatever the theme background happens to be.
    ctx.set_source_rgb(1, 1, 1)
    ctx.rectangle(origin_x, origin_y, symbol_px, symbol_px)
    ctx.fill()

    ctx.set_source_rgb(0, 0, 0)
    for row_i, row in enumerate(matrix):
        col = 0
        while col < len(row):
            if not row[col]:
                col += 1
                continue
            run = 1  # coalesce horizontal runs, fewer rectangles for cairo
            while col + run < len(row) and row[col + run]:
                run += 1
            ctx.rectangle(
                origin_x + (border + col) * module_px,
                origin_y + (border + row_i) * module_px,
                run * module_px,
                module_px,
            )
            col += run
    ctx.fill()
    return module_px, origin_x, origin_y, symbol_px


class QRCodeWidget(Gtk.DrawingArea):
    """DrawingArea that renders a QR code for whatever string it is given."""

    def __init__(self, data=None, min_module_px=MIN_MODULE_PX, border=QUIET_ZONE):
        super().__init__()
        self._border = border
        self._min_module_px = min_module_px
        self._data = None
        self._matrix = None
        # Deliberately no halign/valign CENTER here: centring a GTK widget caps
        # its allocation at its size request, which would pin the symbol to the
        # 3 px/module floor forever. Let it fill, and let draw_qr centre the
        # symbol inside whatever it is given.
        self.connect("draw", self.on_draw)
        self.set_data(data)

    def set_data(self, data):
        """Point the widget at a new URL. Returns True if anything changed.

        The Network panel refreshes every 5 s; re-encoding on every tick would
        burn H3 cycles for nothing, so the matrix is cached and only rebuilt
        when the string actually changes (new DHCP lease, interface switch).
        """
        if data == self._data:
            return False
        self._data = data
        self._matrix = qr_matrix(data)
        if self._matrix:
            # Minimum only: set vexpand on this widget and the drawn symbol
            # takes whatever whole-module size the allocation allows.
            side = (len(self._matrix) + 2 * self._border) * self._min_module_px
            self.set_size_request(side, side)
        else:
            self.set_size_request(-1, -1)
        self.queue_draw()
        return True

    def has_code(self):
        return self._matrix is not None

    def on_draw(self, _widget, ctx):
        allocation = self.get_allocation()
        draw_qr(ctx, self._matrix, allocation.width, allocation.height, self._border)
        return False

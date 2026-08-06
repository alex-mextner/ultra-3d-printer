#!/usr/bin/env python3
# ===========================================================================
# smoke-siren-daemon.py - GPIO siren pulser that lives OUTSIDE klippy.
# ===========================================================================
# Added 2026-08-06. Full writeup, install status and test log:
# docs/printer-status.md, section "Демон-сирена на Orange Pi, независимо от
# klippy". Alarm contract and hardware background: printer-configs/
# smoke-alarm.cfg (read the CONTRACT and RACE sections before touching this)
# and orangepi-display/klipperscreen-patches/smoke-alarm-banner.patch (the
# KlipperScreen side of the same contract, kept in sync with this file).
#
# WHY THIS EXISTS, i.e. why pulsing the siren from inside a Klipper macro is
# not enough:
#   1. Klipper's shutdown state whitelists exactly eight commands
#      (M110/M112/M115/RESTART/FIRMWARE_RESTART/ECHO/STATUS/HELP -
#      klippy/gcode.py). Once a real SMOKE_ALARM shutdown happens,
#      [output_pin siren] is stuck at its static shutdown_value=1 forever -
#      no macro, no delayed_gcode, nothing running inside klippy can ever
#      toggle it again. A STATIC tone is all klippy can ever produce after
#      the one event this whole feature exists for.
#   2. If klippy hangs or crashes for a reason that has NOTHING to do with
#      smoke (this happened once already in this project, from a one-line
#      config bug), the alarm dies with it - there is no fallback, because
#      the entire mechanism lives inside klippy's own process.
#
# Moonraker is a SEPARATE process from klippy and keeps answering
# /printer/info and /printer/objects/query even while klippy itself is
# wedged in "shutdown" or "error" (klippy/webhooks.py _handle_query has no
# ready-gate - verified against the source in this project, see
# printer-configs/smoke-alarm.cfg). So this daemon polls Moonraker only,
# never klippy directly, and drives its own GPIO pin on its own timing -
# independent of klippy's process health and of klippy's gcode whitelist.
#
# TWO-SIGNAL ALARM CONTRACT (copy of printer-configs/smoke-alarm.cfg /
# smoke-alarm-banner.patch - the three places must agree, don't drift):
#
#   alarm active  <=>  output_pin siren .value > 0   (klippy alive - bench
#                       tests via SMOKE_TEST_SIREN/SET_PIN, or the few ms
#                       inside SMOKE_ALARM before the shutdown wins the race)
#                       OR
#                       klippy state in ("shutdown", "error")
#                       AND state_message CONTAINS "SMOKE ALARM"
#                       (substring, NOT startswith - the real string has a
#                       "Shutdown due to " prefix and a boilerplate tail, see
#                       the CONTRACT section of smoke-alarm.cfg for the proof)
#
# WHAT GPIO110 IS NOT: it is not the buzzer's own pin. The buzzer sits on the
# RAMPS board (D44/PL5, driven by klippy via [output_pin siren]) and stays
# there - printer-configs/smoke-alarm.cfg is untouched by this daemon. This
# is a SECOND, independent pin on the Orange Pi host itself (physical header
# pin 12 = PD14 = sysfs gpio 96+14 = 110), which the user will wire to the
# same siren transistor BY HAND at some later point ("когда руки дойдут").
# As of 2026-08-06 it is not physically connected to anything - confirmed
# unclaimed via `gpioinfo` in an earlier session (not re-checked in this one;
# see docs/printer-status.md, "Свободные GPIO на Orange Pi", for that check).
#
# GPIO ACCESS METHOD: raw sysfs (/sys/class/gpio/...), not libgpiod. Checked
# 2026-08-06: `python3 -c "import gpiod"` fails on this box (module not
# installed) for both the `ultra` and `root` users, and this project's
# convention for small fixed-purpose daemons is stdlib-only, no new pip
# dependencies on a 512MB board. Sysfs is present and was verified live
# (export/direction/value/unexport all worked as root).
#
# RUNS AS ROOT. Checked 2026-08-06: `/sys/class/gpio/export` is mode
# `--w------- root root` and `ultra` is not in any group that gets access to
# it (dialout/gpio groups exist on this box but neither owns the gpio
# sysfs class). Adding a udev rule to hand gpio110 to `ultra` was considered
# and rejected: it is more moving parts (a rule file, a reload, a pin-number
# match) for a single fixed pin used by exactly one daemon, so the systemd
# unit simply runs this as root instead - see smoke-siren.service.
#
# NO CONFIG FILE, NO TUNING FLAGS beyond --gpio. Fixed poll interval, fixed
# pulse cadence. This is a small dedicated daemon for one board - keep it
# that way, per project convention (see CLAUDE.md).
# ===========================================================================
import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request

MOONRAKER_BASE = "http://localhost:7125"  # localhost only - this runs ON the
                                           # Orange Pi itself, never the LAN IP
POLL_INTERVAL_S = 1.0        # how often Moonraker is asked, while quiet AND while alarming
PULSE_HALF_PERIOD_S = 0.3    # ~300ms on / 300ms off while alarming
HTTP_TIMEOUT_S = 3.0
SHUTDOWN_MARKER = "SMOKE ALARM"  # must match printer-configs/smoke-alarm.cfg
                                  # _SMOKE_ESTOP - see the CONTRACT note there
                                  # before ever changing this string.
GPIO_SYSFS = "/sys/class/gpio"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s smoke-siren-daemon: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("smoke-siren-daemon")


class Gpio:
    """Thin sysfs wrapper for exactly one output pin. See the module
    docstring for why sysfs and not libgpiod."""

    def __init__(self, num):
        self.num = num
        self.path = f"{GPIO_SYSFS}/gpio{num}"
        self._high = None  # unknown until first set()

    def export(self):
        if not os.path.isdir(self.path):
            with open(f"{GPIO_SYSFS}/export", "w") as f:
                f.write(str(self.num))
            # sysfs creates gpioN/* asynchronously after export; give it a
            # moment rather than racing the direction write below.
            for _ in range(50):
                if os.path.isdir(self.path):
                    break
                time.sleep(0.02)
            else:
                raise OSError(f"gpio{self.num}: export did not create {self.path}")
        with open(f"{self.path}/direction", "w") as f:
            f.write("out")

    def set(self, high):
        if self._high == high:
            return  # sysfs write is cheap, but no reason to do it every tick
        with open(f"{self.path}/value", "w") as f:
            f.write("1" if high else "0")
        self._high = high

    def toggle(self):
        self.set(not self._high)

    # Deliberately NO unexport() call anywhere in this file. Unexporting
    # releases the pin back to the pinctrl default, which is INPUT / high-Z -
    # not driven low. This repo already spent 30 lines proving why that
    # matters for exactly this circuit (docs/printer-status.md, "Вариант A
    # vs B" / smoke-alarm.cfg Option A/B): a bare MOSFET gate with no
    # pulldown resistor HOLDS ITS CHARGE, so high-Z is not silence, it is
    # "whatever it last was, for seconds to minutes." Once the siren wire is
    # moved to this pin, `systemctl stop` unexporting would defeat the exact
    # guarantee the SIGTERM handler exists to provide. Leaving the pin
    # exported-and-low also closes the high-Z window Restart=always would
    # otherwise reopen on every restart cycle. export() already no-ops
    # correctly when the directory exists, so this costs nothing on restart.


def moonraker_get(path):
    """GET a Moonraker endpoint and return the parsed JSON body, or None on
    any failure (connection refused, timeout, malformed response). Never
    raises - callers treat None exactly like "no signal from this source"."""
    try:
        with urllib.request.urlopen(f"{MOONRAKER_BASE}{path}", timeout=HTTP_TIMEOUT_S) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None


_moonraker_was_reachable = True  # assume reachable at startup, so a clean
                                  # boot doesn't log a spurious "recovered"


def _note_moonraker_reachable(reachable):
    global _moonraker_was_reachable
    if reachable == _moonraker_was_reachable:
        return
    _moonraker_was_reachable = reachable
    if reachable:
        log.info("Moonraker reachable again")
    else:
        # Stated plainly, per the task this daemon was built for: a
        # sustained Moonraker/network outage means THIS daemon loses its
        # ability to alert too. That is not a new weakness - the whole
        # alarm system (buzzer included) already depends on the Orange Pi
        # host staying up - this comment just refuses to hide it.
        log.warning("Moonraker unreachable - pin held LOW, will keep retrying")


def alarm_source():
    """Returns "shutdown", "pin", or None - the two-signal contract from the
    module docstring. Logs and returns None on any Moonraker failure."""
    info = moonraker_get("/printer/info")
    if info is None:
        _note_moonraker_reachable(False)
        return None
    result = info.get("result") or {}
    state = result.get("state")
    message = result.get("state_message") or ""
    if state in ("shutdown", "error") and SHUTDOWN_MARKER in message:
        _note_moonraker_reachable(True)
        return "shutdown"

    pin_resp = moonraker_get("/printer/objects/query?output_pin%20siren")
    if pin_resp is None:
        _note_moonraker_reachable(False)
        return None
    _note_moonraker_reachable(True)
    try:
        value = pin_resp["result"]["status"]["output_pin siren"]["value"]
        if value is not None and float(value) > 0:
            return "pin"
    except (KeyError, TypeError, ValueError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Pulse a GPIO pin while the Klipper smoke alarm is active, "
        "polling Moonraker directly (see the header comment for why)."
    )
    parser.add_argument(
        "--gpio", type=int, default=110,
        help="sysfs GPIO number to pulse (default 110 = physical pin 12 = PD14)",
    )
    args = parser.parse_args()

    gpio = Gpio(args.gpio)
    try:
        gpio.export()
    except OSError as exc:
        log.error(
            f"Cannot export gpio{args.gpio}: {exc}. This daemon needs root - "
            "see User= in smoke-siren.service."
        )
        sys.exit(1)
    gpio.set(False)
    log.info(f"Started, watching Moonraker at {MOONRAKER_BASE}, pulsing gpio{args.gpio}")

    running = True

    def handle_stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    latched_source = None  # for edge-triggered logging only, not part of the contract
    last_poll = 0.0

    try:
        while running:
            now = time.monotonic()
            if now - last_poll >= POLL_INTERVAL_S:
                last_poll = now
                source = alarm_source()
                if source != latched_source:
                    if source is not None:
                        log.warning(f"ALARM ACTIVE (source={source}) - pulsing gpio{args.gpio}")
                    else:
                        log.info("Alarm source quiet - pin held LOW")
                    latched_source = source

            if latched_source is not None:
                gpio.toggle()
                time.sleep(PULSE_HALF_PERIOD_S)
            else:
                gpio.set(False)
                time.sleep(POLL_INTERVAL_S)
    finally:
        # Runs on SIGTERM/SIGINT and on any unhandled exception alike -
        # systemctl stop/restart must never leave the buzzer stuck on. Pin
        # is left EXPORTED (see the comment on Gpio, above the class body)
        # so it stays actively driven low rather than reverting to high-Z.
        gpio.set(False)
        log.info("Stopped, pin low")


if __name__ == "__main__":
    main()

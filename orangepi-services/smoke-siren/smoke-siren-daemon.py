#!/usr/bin/env python3
# ===========================================================================
# smoke-siren-daemon.py - GPIO siren pulser AND RGB status-light switcher,
# both living OUTSIDE klippy. Two jobs, one small daemon, same reasons for
# staying outside klippy apply to both - see "RGB STATUS LIGHT" section
# below for why RGB joined this file instead of getting its own.
# ===========================================================================
# Added 2026-08-06 (siren). RGB switching added 2026-08-07. Full writeup,
# install status and test log:
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
# NO CONFIG FILE, NO TUNING FLAGS beyond --gpio/--gpio-r/--gpio-g/--gpio-b.
# Fixed poll interval, fixed pulse cadence. This is a small dedicated daemon
# for one board - keep it that way, per project convention (see CLAUDE.md).
#
# ===========================================================================
# RGB STATUS LIGHT - added 2026-08-07, level-shifted through a KSP42(TA)
# ===========================================================================
# WHY THIS LIVES HERE AND NOT IN A THIRD FILE: same two reasons the siren
# does (module docstring above) - Moonraker answers even when klippy is
# wedged, and this is one small daemon per board, not one per feature. Full
# MOSFET verdict (2x IRFZ44N + 1x IRL640), strip spec/current budget, and the
# one-shot lifecycle event model (RGB_OFF/RGB_PREPARING/RGB_PRINTING) all
# still live in printer-configs/rgb-status.cfg's header - read that first,
# this section only covers what changed 2026-08-07 and the Orange-Pi side.
#
# THE ARCHITECTURE, IN ONE SENTENCE: rgb-status.cfg's three [output_pin]
# sections (rgb_request_r/g/b, AUX-4 D23/D25/D27) are PURE VIRTUAL REQUEST
# FLAGS (same pattern as siren_armed in smoke-alarm.cfg - nothing physically
# wired to those AVR pins at all); Klipper's only job for RGB is to set them
# via SET_PIN at the lifecycle events rgb-status.cfg documents. THIS daemon
# polls those flags over Moonraker (rgb_requested(), same moonraker_get()
# plumbing as alarm_source) once per second and mirrors them onto the real
# MOSFET gates, which it drives from three Orange Pi GPIO pins through an
# inverting KSP42(TA) pre-driver stage - see "WHY A LEVEL SHIFTER" below -
# except during a fire alarm, when it overrides them entirely (see below).
# Consequence worth stating plainly: there is up to ~1s of latency between a
# lifecycle macro call (e.g. RGB_PRINTING) and the physical strip actually
# changing color - the same POLL_INTERVAL_S already used for the siren, not
# a new number, but a new place it applies.
#
# WHY A LEVEL SHIFTER: Orange Pi GPIO is 3.3V logic. IRFZ44N's VGS(th) is
# 2.0V min / 4.0V max (Infineon PD-94787B) - 3.3V is BELOW the worst-case
# threshold, i.e. not guaranteed to turn the MOSFET on at all, let alone
# fully enhance it. (IRL640's VGS(th) max is 2.0V - Vishay S21-1046-Rev.D -
# so 3.3V would actually work for that one channel alone, but driving one
# channel one way and two another for no real benefit is exactly the kind
# of asymmetry not worth having; all three go through the same stage.)
# KSP42(TA) (NPN, TO-92, 300V/500mA - same part already used for both
# cooling-fan BJT swaps and the siren-buzzer driver tonight, on hand, wildly
# overspec for this role) sits between each GPIO and its MOSFET gate as an
# inverting pre-driver, so the gate always sees a clean 5V pull-up instead
# of the raw 3.3V rail:
#
#   +5V (Orange Pi's own 40-pin header 5V pin, physical pin 2 or 4)
#      |
#     [ 10k pull-up ]
#      |
#      +------------------------- GATE of power MOSFET (IRFZ44N/IRL640)
#      |
#    COLLECTOR of KSP42(TA)
#      |
#    BASE --[ 10k base resistor ]-- GPIO pin (Orange Pi, 3.3V, THIS daemon)
#      |
#    EMITTER -- GND (common with RAMPS and the Orange Pi)
#
# Logic is INVERTED: GPIO HIGH -> KSP42(TA) saturates -> pulls the MOSFET
# gate LOW -> MOSFET OFF (channel dark). GPIO LOW -> KSP42(TA) is off -> the
# 10k pull-up charges the gate to +5V -> MOSFET ON with full margin (channel
# lit). This is why the Gpio class below grows an active_low flag and a
# set_on() method (see its docstring) - "on" and "GPIO high" are opposites
# for these three pins, and that inversion has to be impossible to miss.
#
# RESISTOR MATH - both values chosen, not copied, using numbers already
# established in this project tonight (electronics-fan.cfg's KSP42 table:
# hFE >= 25 min @ Ic=1mA per the Fairchild/onsemi ON Characteristics table;
# VBE(sat) = 0.9V max @ Ic=20mA, used here as the conservative B-E drop
# rather than the generic 0.7V rule of thumb, same choice electronics-fan.cfg
# made and for the same reason):
#   Collector current (through the 10k pull-up, KSP42 saturated):
#     Ic = (5V - Vce_sat) / 10k =~ (5V - 0.5V) / 10k =~ 0.45mA
#     (Vce_sat = 0.5V max @ Ic=20mA/Ib=2mA, same datasheet table - almost
#     certainly an over-estimate at this much lower current, so 0.45mA is
#     itself a conservative/pessimistic number, not an optimistic one.)
#   Base current (Orange Pi GPIO HIGH = 3.3V, through the 10k base resistor):
#     Ib = (3.3V - 0.9V) / 10k = 2.4V / 10k = 0.24mA
#   Forced beta = Ic/Ib =~ 0.45mA / 0.24mA =~ 1.9
#   Compare to the datasheet's OWN saturation test condition of forced
#   beta = 10 (Ic/Ib=10, i.e. the manufacturer only guarantees Vce_sat at a
#   10x overdrive) - this design runs at forced beta ~1.9, more than 5x
#   HARDER driven than the datasheet's own saturation guarantee. That holds
#   regardless of where the true hFE actually sits (25 is only the
#   PUBLISHED floor at Ic=1mA - below that test point, real hFE for a
#   small-signal BJT can droop lower still, same caveat electronics-fan.cfg
#   raised about drooping ABOVE its characterized range; here the
#   uncharacterized direction is below 1mA). Forced beta 1.9 saturates hard
#   even against an hFE far below 25 - there is no realistic silicon
#   small-signal transistor with hFE < 2, so this margin is not
#   theoretical.
#   Gate charge time constant: tau = R_pullup * Ciss. IRFZ44N Ciss 1470pF ->
#     tau =~ 14.7us. IRL640 Ciss 1800pF max -> tau =~ 18us. Both numbers
#     already established in rgb-status.cfg's header for the ORIGINAL
#     (direct-drive) design; the pull-up resistor plays the role the RAMPS
#     pin's own ~40R source impedance used to play there. Microsecond-scale
#     against lifecycle-event-rate switching (at most a few times per
#     print) - no series gate resistor needed, same conclusion as before,
#     different reason.
#   10k/10k was the pair suggested when this circuit was designed by hand on
#   paper tonight; the math above confirms it rather than replacing it.
#
# 🔴 BOOT-DEFAULT WARNING - read before assuming "undriven = dark":
# An NPN base with nothing driving it (Orange Pi mid-boot, before this
# daemon has exported/claimed the GPIO pins, OR this daemon crashed/was
# killed some way that skipped the finally: block) floats near 0V - the
# KSP42(TA) stays OFF, its collector does NOT pull the gate down, and the
# 10k pull-up charges the MOSFET gate to +5V on its own. Undriven means
# CHANNEL ON, not channel off. This is the OPPOSITE of every other
# output_pin default in this project (value: 0 at startup, dark until
# commanded otherwise) and the opposite of what "inverted logic, GPIO LOW =
# on" might suggest at a glance. Concretely: with the 12V strip rail live
# and the Orange Pi powered down or still booting, the worst case is the
# FULL STRIP AT WHITE (~1.26A, see rgb-status.cfg's current budget),
# unattended, for as long as that gap lasts - Orange Pi boot is tens of
# seconds, far longer than the AVR bootloader gap the original (pre-shifter)
# design accepted as "cosmetic, half a second." This is a real consequence
# of the circuit topology the user chose tonight, not smoothed over as
# cosmetic - it is written here deliberately so it cannot be waved away as
# equivalent to the old AVR-reset gap, because it is not.
#   NOT A FIX: a naive gate-to-GND pulldown resistor. The MOSFET gate sees
#   BOTH the 10k pull-up (to 5V, always present) and any pulldown added in
#   parallel - a pulldown of the same 10k would form a divider and settle
#   the gate at 2.5V, BELOW IRFZ44N's 4.0V worst-case VGS(th) - that does
#   not fix the boot-on problem, it just adds a second failure mode
#   (partial/uncertain enhancement) without removing the first. A real fix
#   would need a much weaker pulldown relative to the pull-up (e.g. a stiff
#   1k pull-up with a weak 100k pulldown, recomputed against the KSP42's Ic
#   at the new pull-up value) - NOT implemented tonight, left as a note for
#   whoever revisits this if the boot-on window turns out to matter in
#   practice. The mitigation that IS in place: this daemon exports and
#   drives all three RGB pins to OFF within its first second of running
#   (see main()), and its finally: block does the same on any exit path -
#   see the no-unexport comment on the Gpio class, which now matters MORE
#   here than it did for the siren alone, because for an active_low pin
#   unexporting (reverting to high-Z) means full brightness, not silence.
#
# GPIO PINS - verified LIVE 2026-08-07 via `gpioinfo` on gpiochip0 (the
# sunxi H3 pinctrl chip - NOT gpiochip1, which is a separate 32-line chip
# with its own overlapping line numbers; scoping to gpiochip0 mattered,
# an unscoped grep across both chips' output gave false collisions during
# this check). Bank arithmetic (base = bank_index*32, A=0/B=32/C=64/D=96/
# E=128/F=160/G=192) is the same convention smoke-siren-daemon.py's own
# --gpio default already documents for gpio110=PD14.
#   RED   -> sysfs gpio21 = PA21   (line 21 on gpiochip0: `input`, unclaimed)
#   GREEN -> sysfs gpio19 = PA19   (line 19: `input`, unclaimed)
#   BLUE  -> sysfs gpio18 = PA18   (line 18: `input`, unclaimed)
# For comparison, the existing siren pin gpio110/PD14 showed
# `output consumer=sysfs` in the same dump (this daemon's own export) -
# confirms the scoping/arithmetic against a pin already known-good.
# Physical 40-pin-header contact numbers (as opposed to the sysfs line
# numbers above, which are the ones actually verified live this session):
# printer-status.md's "Свободные GPIO на Orange Pi" line already lists
# these three as PA18/PA19/PA21 = header pins 28/27/26, consistent with the
# independently-verified anchor table in the same document (its "Итоговая
# распиновка" section, cross-checked against PC0/PC1/PC2/PC3/PC4/PC7/PA6/
# PA7/PA8/PA9/PA10/PA20 - all of which match the standard official Orange Pi
# One layout). That cross-check is corroboration, not a fresh independent
# derivation done THIS session - an attempt to re-read the physical layout
# straight off docs/orange-pi-one-pinout.png this session produced an
# inconsistent row mapping and was DISCARDED rather than trusted, on the
# standing project rule that a confidently-wrong pin claim has already cost
# real time twice (PDN_UART mixup, X-MIN-for-a-flag proposal). Treat the
# sysfs numbers (18/19/21) as the verified fact; treat "physical pins
# 28/27/26" as inherited/corroborated, worth a multimeter check against the
# actual board before soldering, same as any pin claim in this project.
# Physically, these three sit as a compact cluster spanning two rows of the
# 40-pin header (not a single straight ribbon run like AUX-4) - still
# convenient for three adjacent dupont leads, just not literally in a line.
#
# WHAT GPIO18/19/21 ARE NOT: PD14 (gpio110, the siren pin) was independently
# confirmed unclaimed and physically unconnected before this project reused
# it; PA18/PA19/PA21 get the same treatment here - confirmed unclaimed via
# gpioinfo, not yet physically wired to anything (no strip bought, no
# KSP42(TA)s soldered for this role, no MOSFETs mounted - see rgb-status.cfg
# and printer-status.md basket C9 for the full blocker list).
#
# FIRE ALARM OVERRIDE - reuses alarm_source() UNCHANGED, does not touch
# rgb_request_r's old shutdown_value mechanism at all. When alarm_source()
# is not None, RED blinks at the exact same PULSE_HALF_PERIOD_S cadence as
# the siren (same loop iteration drives both), GREEN and BLUE are forced
# off, and the REQUEST-flag mirroring is skipped entirely - fire alarm wins
# over whatever color was requested, unconditionally, for as long as
# alarm_source() stays non-None. This inherits every property alarm_source()
# already has (proven live 2026-08-06: independent of klippy hanging,
# independent of the shutdown-command whitelist, degrades safely to "pin
# held/left at its last known state" on a Moonraker outage) for free,
# because it IS alarm_source() - no new detection logic was written.
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
    docstring for why sysfs and not libgpiod.

    active_low: this pin drives an inverting KSP42(TA) pre-driver stage
    (see the RGB STATUS LIGHT section of the module docstring) - GPIO HIGH
    saturates the BJT and pulls the downstream MOSFET gate LOW (channel
    OFF); GPIO LOW lets the BJT's own collector pull-up charge the gate to
    +5V (channel ON). set_on() is the ONLY method that should be used on an
    active_low pin - it does the inversion, so a future reader (or a future
    edit to this file) cannot accidentally call the raw set()/toggle() and
    light the strip backwards. set()/toggle() stay raw and physical,
    unaware of active_low - that is what the siren pin (active_low=False,
    the default) still uses, completely unchanged from before RGB existed."""

    def __init__(self, num, active_low=False):
        self.num = num
        self.active_low = active_low
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
        """Raw physical write: True drives the sysfs pin HIGH, full stop.
        Bypasses active_low entirely - see set_on() for the logic-level
        version an active_low pin (the three RGB pins) should actually use."""
        if self._high == high:
            return  # sysfs write is cheap, but no reason to do it every tick
        with open(f"{self.path}/value", "w") as f:
            f.write("1" if high else "0")
        self._high = high

    def set_on(self, on):
        """Logic-level write: True turns the downstream device ON,
        honoring this pin's active_low wiring (flips the physical write for
        active_low pins, passes through unchanged for active_low=False like
        the siren). ALWAYS use this, never set(), for an active_low pin -
        see the class docstring for why the inversion has to live in one
        obvious place."""
        self.set((not on) if self.active_low else on)

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
    #
    # This matters MORE for the three RGB pins than it ever did for the
    # siren alone: they are active_low, so high-Z (unexported/undriven) does
    # NOT mean dark - see the BOOT-DEFAULT WARNING in the module docstring.
    # unexport()ing on stop would trade "held explicitly off" for "floating,
    # channel probably on" - the opposite of safe. Never add it here.


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


def rgb_requested():
    """Poll the three REQUEST flags Klipper sets via RGB_OFF/RGB_PREPARING/
    RGB_PRINTING (printer-configs/rgb-status.cfg) - output_pin
    rgb_request_r/g/b on AUX-4 D23/D25/D27, PURE VIRTUAL FLAGS with nothing
    physically wired to those AVR pins (same pattern as siren_armed).
    Returns {"r": bool, "g": bool, "b": bool}, or None on ANY failure -
    including "rgb-status.cfg isn't included in printer.cfg yet", which is
    the state as of 2026-08-07. Confirmed LIVE against this exact printer:
    querying an output_pin Klipper doesn't know about returns HTTP 200 with
    an EMPTY {} for that object's status (not an HTTP error, does not
    poison the other objects in the same query) - so this hits the
    KeyError branch below and returns None, same as a real Moonraker
    outage would. The caller keeps the last known values in that case
    (initialized to all-False = off, matching every other output_pin's
    startup default in this project) - so running this daemon before
    rgb-status.cfg is deployed is inert and safe, not a crash risk."""
    resp = moonraker_get(
        "/printer/objects/query?output_pin%20rgb_request_r"
        "&output_pin%20rgb_request_g&output_pin%20rgb_request_b"
    )
    if resp is None:
        return None
    try:
        status = resp["result"]["status"]
        return {
            "r": float(status["output_pin rgb_request_r"]["value"]) > 0,
            "g": float(status["output_pin rgb_request_g"]["value"]) > 0,
            "b": float(status["output_pin rgb_request_b"]["value"]) > 0,
        }
    except (KeyError, TypeError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Pulse a GPIO pin while the Klipper smoke alarm is active, "
        "polling Moonraker directly (see the header comment for why)."
    )
    parser.add_argument(
        "--gpio", type=int, default=110,
        help="sysfs GPIO number to pulse for the siren (default 110 = "
        "physical pin 12 = PD14)",
    )
    parser.add_argument(
        "--gpio-r", type=int, default=21,
        help="sysfs GPIO number for the RGB red channel, active_low through "
        "a KSP42(TA) level shifter (default 21 = PA21, physical pin 26)",
    )
    parser.add_argument(
        "--gpio-g", type=int, default=19,
        help="sysfs GPIO number for the RGB green channel, active_low "
        "(default 19 = PA19, physical pin 27)",
    )
    parser.add_argument(
        "--gpio-b", type=int, default=18,
        help="sysfs GPIO number for the RGB blue channel, active_low "
        "(default 18 = PA18, physical pin 28)",
    )
    args = parser.parse_args()

    gpio_siren = Gpio(args.gpio)
    gpio_r = Gpio(args.gpio_r, active_low=True)
    gpio_g = Gpio(args.gpio_g, active_low=True)
    gpio_b = Gpio(args.gpio_b, active_low=True)

    for gpio, label in (
        (gpio_siren, "siren"), (gpio_r, "rgb_r"), (gpio_g, "rgb_g"), (gpio_b, "rgb_b"),
    ):
        try:
            gpio.export()
        except OSError as exc:
            log.error(
                f"Cannot export gpio{gpio.num} ({label}): {exc}. This daemon "
                "needs root - see User= in smoke-siren.service."
            )
            sys.exit(1)

    gpio_siren.set(False)
    gpio_r.set_on(False)
    gpio_g.set_on(False)
    gpio_b.set_on(False)
    log.info(
        f"Started, watching Moonraker at {MOONRAKER_BASE}, pulsing gpio{args.gpio} "
        f"(siren), mirroring RGB requests onto gpio{args.gpio_r}/{args.gpio_g}/"
        f"{args.gpio_b} (R/G/B, active_low)"
    )

    running = True

    def handle_stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    latched_source = None  # for edge-triggered logging only, not part of the contract
    requested = {"r": False, "g": False, "b": False}  # last known REQUEST
    # flags from rgb-status.cfg, mirrored onto the real channels while quiet.
    # Starts all-off, matching every output_pin's value:0 startup default in
    # this project - stays all-off until rgb_requested() first succeeds.
    red_blink_on = False  # blink phase for RED during a fire alarm
    last_poll = 0.0

    try:
        while running:
            now = time.monotonic()
            if now - last_poll >= POLL_INTERVAL_S:
                last_poll = now
                source = alarm_source()
                if source != latched_source:
                    if source is not None:
                        log.warning(
                            f"ALARM ACTIVE (source={source}) - pulsing gpio{args.gpio}, "
                            "RGB red blinking, green/blue forced off"
                        )
                    else:
                        log.info("Alarm source quiet - pin held LOW, RGB back to requested state")
                    latched_source = source

                if latched_source is None:
                    # Only refresh the REQUEST mirror while not alarming -
                    # about to be overridden anyway during an alarm, no
                    # point spending an extra Moonraker round trip on it.
                    new_requested = rgb_requested()
                    if new_requested is not None:
                        requested = new_requested

            if latched_source is not None:
                gpio_siren.toggle()
                red_blink_on = not red_blink_on
                gpio_r.set_on(red_blink_on)
                gpio_g.set_on(False)
                gpio_b.set_on(False)
                time.sleep(PULSE_HALF_PERIOD_S)
            else:
                gpio_siren.set(False)
                gpio_r.set_on(requested["r"])
                gpio_g.set_on(requested["g"])
                gpio_b.set_on(requested["b"])
                time.sleep(POLL_INTERVAL_S)
    finally:
        # Runs on SIGTERM/SIGINT and on any unhandled exception alike -
        # systemctl stop/restart must never leave the buzzer stuck on OR
        # the strip stuck lit (see the BOOT-DEFAULT WARNING in the module
        # docstring for why "stuck lit" is the RGB failure mode to guard
        # against here, not "stuck dark"). All four pins are left EXPORTED
        # (see the comment on Gpio, above the class body) so they stay
        # actively driven rather than reverting to high-Z.
        gpio_siren.set(False)
        gpio_r.set_on(False)
        gpio_g.set_on(False)
        gpio_b.set_on(False)
        log.info("Stopped, siren pin low, RGB channels off")


if __name__ == "__main__":
    main()

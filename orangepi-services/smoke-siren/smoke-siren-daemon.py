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
# THE ARCHITECTURE, IN ONE SENTENCE: rgb-status.cfg's [led rgb_strip] object
# (Klipper's stock PWM-LED handler, AUX-4 dummy pins D23/D25/D27) is a real
# Klipper object with DUMMY PINS - nothing physically wired to those AVR
# pins at all (same "unconnected on purpose" idea as the output_pin flags
# this replaced 2026-08-07, and as siren_armed in smoke-alarm.cfg); Klipper's
# only job for RGB is to hold and report a requested color via SET_LED, sent
# either by the lifecycle macros rgb-status.cfg documents or by Mainsail's
# native color-picker widget (both call the same SET_LED command - one
# object, one path, not two). THIS daemon polls that color over Moonraker
# (rgb_requested(), same moonraker_get() plumbing as alarm_source) once per
# second, and drives each channel through REAL software PWM (see the
# "SOFTWARE PWM" section below) rather than a fixed on/off threshold, so the
# picker's fractional brightness actually reaches the physical strip as
# time-averaged brightness - except during a fire alarm, when it overrides
# them entirely (see below). Consequence worth stating plainly: there is up
# to ~1s of latency between a color/brightness change (lifecycle macro call
# or a Mainsail picker click/drag) and the PWM thread's TARGET duty updating
# - the same POLL_INTERVAL_S already used for the siren, not a new number,
# but a new place it applies. The PWM thread itself then reaches that new
# target within at most one PWM period (5ms at the chosen frequency - see
# below), which is imperceptible next to the 1s poll latency that dominates.
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
# The PWM layer (RgbPwm, below) NEVER writes the raw GPIO level itself - it
# only ever calls set_on(), the same inversion-aware method the alarm-blink
# code already used pre-PWM - so the active_low math lives in exactly one
# place regardless of whether a channel is being driven steady or PWM'd.
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
#     against the ORIGINAL lifecycle-event-rate switching (at most a few
#     times per print) - no series gate resistor needed there. Now that PWM
#     toggles these gates up to 200x/second (see SOFTWARE PWM below), this
#     same tau is still >100x smaller than the ~5ms PWM period, so it stays
#     negligible - the gate fully charges/discharges in about 15-18us
#     against a period two and a half orders of magnitude longer; this was
#     re-checked specifically for the PWM case, not assumed to still hold
#     just because it held for the slower original use.
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
#   🔴 PWM ADDS A SECOND, NARROWER FAILURE MODE - read before assuming the
#   above is the only way a channel can end up "wrong":
#   The paragraph above covers a pin that was NEVER driven at all (true
#   floating, no prior export/write). PWM introduces a second, genuinely
#   different case: once this daemon HAS exported a pin and started
#   actively PWM-toggling it, a process death that skips the finally: block
#   (SIGKILL, an OOM-kill, a segfault - anything that doesn't run normal
#   Python exception unwinding) freezes that pin at WHATEVER physical level
#   its last sysfs write happened to leave it at. For a channel sitting at
#   duty 0.0 or 1.0 (steady off, or steady on - the only values the
#   lifecycle macros RGB_OFF/RGB_PREPARING/RGB_PRINTING ever request) this
#   is no different from before PWM existed: one steady value, frozen at
#   exactly that value, fully deterministic. But for a channel actively
#   sitting at an intermediate duty (0<duty<1 - only reachable by hand,
#   dragging Mainsail's brightness slider mid-color) the pin is being
#   toggled up to PWM_FREQUENCY_HZ times a second, so a crash lands at some
#   essentially arbitrary point in that channel's on/off cycle - the
#   channel could freeze ON or OFF, NOT predictably "full brightness" like
#   the true-floating case above. Scope this narrowly rather than let it
#   sound worse than it is: it (a) can only happen to a channel that has
#   already been successfully exported at least once - any crash before
#   that point is still the fully-deterministic full-ON floating case
#   above, unchanged by PWM; and (b) can only affect a channel actively
#   mid-dim at the moment of death - this status light spends the
#   overwhelming majority of its time at the lifecycle macros' steady 0/1
#   values, where this new mode simply does not apply. Worst realistic case
#   is one channel (not all three) stuck at an unpredictable on/off
#   snapshot, not the three-channel full-white worst case above.
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
# confirms the scoping/arithmetic against a pin already known-good. These
# same three lines now also carry PWM (see SOFTWARE PWM below) - no pin
# reassignment was needed for that, the circuit and pin choice are
# unchanged, only the drive WAVEFORM on them changed.
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
# rgb_strip's color_data or shutdown behavior at all. When alarm_source()
# is not None, RED blinks at the exact same PULSE_HALF_PERIOD_S cadence as
# the siren (same loop iteration drives both - this did NOT change when PWM
# was added, see below), GREEN and BLUE are forced off, and the PWM thread
# is told (via RgbPwm.set_alarm(True)) to stop touching gpio_r/g/b entirely
# for as long as the override is active - fire alarm wins over whatever
# color/brightness was requested, unconditionally. This inherits every
# property alarm_source() already has (proven live 2026-08-06: independent
# of klippy hanging, independent of the shutdown-command whitelist,
# degrades safely to "pin held/left at its last known state" on a Moonraker
# outage) for free, because it IS alarm_source() - no new detection logic
# was written.
#   HOW THE HANDOFF IS RACE-FREE: gpio_r/g/b's underlying sysfs writes are
#   guarded by one lock (RgbPwm._lock), acquired individually around EVERY
#   write either side makes - not held for a whole PWM period, and not held
#   for the whole alarm-blink loop either. The PWM thread checks the alarm
#   flag before EACH of the up to six writes it might make in a period, so
#   a fire-alarm transition landing mid-period aborts that period's
#   remaining writes within roughly one write's latency (tens of
#   microseconds - see SOFTWARE PWM measurements below), not up to a whole
#   period later. Worst-case bound either direction (alarm turning on, or
#   releasing back to PWM) is about one PWM period, ~5ms at the chosen
#   200Hz - see RgbPwm's docstring for the exact mechanism. ~5ms is far
#   below anything perceptible on a status light, and two orders of
#   magnitude smaller than the alarm's own up-to-1s DETECTION latency
#   (alarm_source() is only polled once a second, unchanged by this work) -
#   so it is not the bottleneck for how fast the override becomes visible.
#   The siren pin itself (gpio110) is written directly by the main loop the
#   entire time, exactly as before PWM existed - it was never touched by
#   the PWM thread and has zero lock contention of any kind.
# ===========================================================================
# SOFTWARE PWM FOR REAL DIMMING - added 2026-08-07, same evening as the
# [led]/color-picker work above, in response to: the picker's brightness
# slider did nothing physically (any value above the old 0.5 threshold
# snapped to full-on, below snapped to full-off) - confirmed live tonight
# cycling red/green/blue/yellow/white/off correctly, but not dimming.
# ===========================================================================
# WHY SOFTWARE, NOT HARDWARE: checked live tonight, `ls /sys/class/pwm/` on
# this Orange Pi is empty - no PWM chip/overlay is active, so there is no
# hardware timer this daemon could hand duty-cycle generation off to. Real
# dimming has to come from THIS process rapidly toggling each GPIO pin's
# sysfs value file at a target duty cycle and letting human eye/camera
# persistence average it into a brightness level - "software PWM" in the
# literal sense, not Klipper's (irrelevant here - see next paragraph) and
# not a kernel PWM driver.
#
# WHETHER KLIPPER'S [led] `hardware_pwm` FLAG MATTERS HERE: checked against
# the actual source on this printer (~/klipper/klippy/extras/led.py,
# class PrinterPWMLED / LEDHelper, 2026-08-07). `hardware_pwm` (config
# default False; there is no config key literally named `pwm`) is passed
# straight into `mcu_pin.setup_cycle_time(cycle_time, hardware_pwm)` and
# affects ONLY how Klipper's firmware side would generate a PWM waveform on
# red_pin/green_pin/blue_pin - which are the dummy, physically-unconnected
# AUX-4 pins (see rgb-status.cfg). `get_status()` returns
# `{'color_data': self.led_state}` where `led_state` is the raw
# `(red,green,blue,white)` float tuple set by `cmd_SET_LED` - never
# quantized, rounded, or otherwise touched by `hardware_pwm` anywhere in
# this file. Conclusion, verified rather than assumed: `hardware_pwm`
# cannot affect what this daemon reads from Moonraker either way, because
# it only governs a PWM waveform on pins nothing is physically attached to.
# rgb-status.cfg's `[led rgb_strip]` section was left unchanged - no config
# edit was needed for real dimming to work, only this file and the daemon's
# reading of color_data as a continuous float instead of thresholding it.
#
# HOW THE FREQUENCY WAS CHOSEN - measured, not assumed, on THIS board:
#   Two throwaway scripts were run directly on the Orange Pi against a
#   confirmed-free, confirmed-UNWIRED scratch pin (gpio200/PG8, physical
#   header pin 32 - docs/printer-status.md, "Свободные GPIO на Orange Pi":
#   "PG8/PG9 (32/36) — по-прежнему свободны") - toggling it has zero
#   physical effect on anything, so this was safe to hammer without warning
#   the user (unlike anything touching gpio18/19/21, which ARE wired to the
#   live strip).
#   1) Raw sysfs write latency, N=3000 each, two strategies:
#        reopen-every-write (this file's ORIGINAL Gpio.set() - open()/
#        write()/close() every toggle): mean 161us, median 133us,
#        p95 276us, p99 377us, max 998us. CPU time per write: ~152us.
#        keep-fd-open (open the value file ONCE, then seek(0)+write+flush
#        per toggle - the standard fast sysfs-GPIO pattern): mean 19.4us,
#        median 18.7us, p95 21.0us, p99 28.3us, max 122us. CPU time per
#        write: ~18us.
#      ~8x faster and ~8x less CPU per write with the fd kept open - this
#      is why Gpio.export() below now opens the value file once and keeps
#      it, and Gpio.set() now does seek+write+flush instead of reopening.
#      This benefits the siren pin and the alarm-blink writes too (free
#      win, not PWM-specific), though neither of those was frequent enough
#      for the old cost to matter on its own.
#   2) time.sleep() overshoot/jitter at PWM-relevant target intervals
#      (N=1000 each, no GPIO involved, pure scheduler timing): at 5ms
#      targets, mean overshoot 106.6us, median 99.2us, p95 150.4us,
#      p99 192.5us, max (one outlier) 1140.9us. Broadly similar shape at
#      1/2/8.33/10ms targets (means 89-116us, occasional single-sample
#      spikes to 1-3ms, almost certainly other processes on this shared,
#      RAM-constrained board getting scheduled ahead of this one - Klipper/
#      Moonraker/KlipperScreen/crowsnest are all running at the same time,
#      see CLAUDE.md).
#   CHOSEN: PWM_FREQUENCY_HZ = 200 (period 5ms). Reasoning against the
#   measured numbers above, not against a number remembered from other
#   boards:
#     - Typical sleep jitter (~100us) is about 2% of a 5ms period - not
#       perceptible on a diffuse status light. The rare 1-3ms outlier spikes
#       are a MINORITY of samples (roughly 1 in 1000 in this data) and
#       affect at most a single ~5ms period out of many per second - far
#       below anything the eye integrates as flicker, unlike e.g. an audio
#       glitch where a single dropped sample is audible.
#     - Worst-case write load per period is 6 writes (all 3 channels at a
#       genuine intermediate duty: 3 "turn on" writes at period start + 3
#       scheduled "turn off" writes later in the period - see RgbPwm). At
#       the keep-fd-open p99 of ~28us each, 6 writes cost roughly
#       ~170us worst-case-ish, under 4% of a 5ms period - comfortable
#       margin, and this worst case only happens while multiple channels
#       are simultaneously mid-dim, not during normal lifecycle colors
#       (see RgbPwm's docstring for why 0/1 duty costs virtually nothing).
#     - 200Hz sits inside the "100Hz-1kHz, common practical range for LED
#       PWM dimming" band, on the more conservative (lower-frequency, more
#       jitter headroom) side of it rather than pushed to the edge of what
#       this board's write/sleep numbers could sustain - deliberately not
#       maximized, because there is no benefit to this status light from
#       going faster once flicker is already imperceptible, and every extra
#       Hz is pure added CPU/write-syscall load for no visible gain.
#     - This machine also has a webcam (rgb-status.cfg: "PRINTING (white)
#       ... illumination for the webcam"). A PWM frequency with a long
#       period relative to typical camera exposure time could in principle
#       alias into visible banding; 5ms is short relative to a 30fps
#       camera's usual >=8-10ms exposure at typical bench lighting, so
#       multiple PWM cycles should average out within one frame. This is
#       reasoning, not a webcam test performed tonight - flagged honestly
#       rather than claimed as verified.
#   NOT CHOSEN: something in the tens-of-Hz range (e.g. 60Hz) - would have
#   left less margin over the observed jitter tail without buying anything,
#   since even 200Hz already has comfortable headroom on this hardware.
#
# CPU IMPACT - measured, not just reasoned, without ever touching the live
# strip: the exact RgbPwm class below (imported and run, not reimplemented)
# was pointed at three more confirmed-free, confirmed-unwired scratch pins
# (gpio2/PA2, gpio3/PA3, gpio200/PG8 - "Свободные GPIO на Orange Pi" again)
# and run standalone for 8-second windows at three duty patterns, with this
# PROCESS's own CPU time read from /proc/self/stat (utime+stime ticks)
# before/after each window - measures the daemon's actual CPU consumption,
# not a proxy. Two rounds of this measurement, described in order because
# the first round changed the design (see IDLE CPU in RgbPwm's docstring):
#   ROUND 1 (always-loop-at-PWM_FREQUENCY_HZ design, since replaced):
#     worst case (0.5/0.3/0.7, maximizing writes/period): 11.50% of a core.
#     steady full-on (1.0/1.0/1.0, matches RGB_PRINTING):  3.00% of a core.
#     steady off     (0.0/0.0/0.0, matches RGB_OFF):        4.25% of a core.
#   This surfaced something the raw write-cost argument alone had missed:
#   Gpio.set()'s cache does make the SYSCALL count near-zero for a steady
#   channel (that part of the original reasoning held up), but the FIXED
#   per-period Python bookkeeping this thread paid 200 times a second
#   REGARDLESS of duty (lock acquisitions, a dict copy of the duty
#   snapshot, monotonic() calls, constructing/sorting the usually-empty
#   off-edge list) does not disappear just because there's nothing to
#   write - that bookkeeping, not the writes, is why steady-state cost a
#   real 3-4% instead of ~0%. Left in this comment with the numbers, not
#   hedged as "reasoned only", because the first attempt at this estimate
#   undersold it and this project has already been asked once tonight not
#   to repeat that pattern.
#   ROUND 2, after adding the event-driven idle block described in RgbPwm's
#   IDLE CPU section (current design): re-measured the same three
#   scenarios, same harness:
#     steady full-on: 0.00% of a core. steady off: 0.00% of a core.
#     worst case: 11.50% in one run, 18.25% in a later rerun (1.46s CPU /
#       8s wall both times the 18.25% figure came up - not a fluke of a
#       single sample) - the difference tracks a `load average` that had
#       risen from a quiet board to 2.33 with "2 users" logged in between
#       the two measurements, almost certainly a second concurrent SSH
#       session active on this same board during this work, not a change
#       in this code. Reported as a RANGE (roughly 11-18% of one core in
#       the worst case, under real observed system-load variance) rather
#       than a single misleadingly-precise number.
#   Net effect of the fix: the common case (any steady color, including
#   RGB_OFF - which is where this light spends most of its life) dropped
#   from a real, continuously-paid 3-4% to measured 0.00%, matching the
#   idle-baseline (PWM thread not running at all) figure exactly. The
#   worst case (actively dragging multiple channels through intermediate
#   values at once) is unchanged in kind - still bounded, still only paid
#   while someone is actively at the Mainsail slider, not a steady
#   operating point - and its exact percentage moves with whatever else is
#   running on the board at the time, which the range above states rather
#   than hides.
# In absolute terms, even the higher end of that range (18.25% of ONE of
# this board's four cores) is at most a few percent of total system
# capacity in the pathological case of three channels being dragged
# simultaneously - and the pre-PWM version of this thread's work (a plain
# 1Hz poll) cost effectively 0%, so the worst case remains a real,
# honestly-reported increase, not a free lunch, even though it stays
# comfortably inside what a 512MB/4-core board run alongside Klipper/
# Moonraker/KlipperScreen/crowsnest can absorb. Re-run scripts equivalent
# to this session's rgb_pwm_cpu_test.py (not kept in this repo - ad hoc,
# scratch-pin-only, safe to recreate) if PWM_FREQUENCY_HZ or the idle
# design ever change and this number needs rechecking.
#
# THREADING MODEL - one dedicated thread (RgbPwm) drives all three channels
# together, NOT one thread per channel. See RgbPwm's own docstring for the
# full reasoning (GIL/scheduler cost of 3 independent sleep loops vs 1;
# synchronized period boundaries avoiding inter-channel drift). The existing
# 1Hz main loop is now reduced, for RGB, to: poll Moonraker, and hand the
# PWM thread a new TARGET duty (pwm.set_duty(...)) or a new alarm-override
# state (pwm.set_alarm(...)) - it no longer touches gpio_r/g/b directly
# except during a fire alarm, where it still does, unchanged from before.
#
# HONESTY ABOUT PRECISION: this is Python + sysfs + a non-realtime Linux
# kernel doing the toggling, not a hardware PWM peripheral or even a
# microcontroller bit-banging in a tight uninterruptible loop. The jitter
# numbers above are real and were left in this comment on purpose - this
# design trades lab-grade duty-cycle accuracy for "good enough that a human
# cannot see the difference on a diffuse status light," which is the actual
# requirement here, not a claim that this is a precision PWM source suitable
# for, say, driving a motor or measuring anything.
# ===========================================================================
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

MOONRAKER_BASE = "http://localhost:7125"  # localhost only - this runs ON the
                                           # Orange Pi itself, never the LAN IP
POLL_INTERVAL_S = 1.0        # how often Moonraker is asked, while quiet AND while alarming
PULSE_HALF_PERIOD_S = 0.3    # ~300ms on / 300ms off while alarming
HTTP_TIMEOUT_S = 3.0
PWM_FREQUENCY_HZ = 200       # see "SOFTWARE PWM FOR REAL DIMMING" above for
                              # the full measured basis (sysfs write latency
                              # + time.sleep() jitter on THIS board) - not a
                              # number carried over from general knowledge of
                              # other Linux boards.
PWM_PERIOD_S = 1.0 / PWM_FREQUENCY_HZ  # 0.005s = 5ms
DUTY_SNAP_EPSILON = 0.01     # a requested duty within 1% of 0.0/1.0 (a
                              # difference no human can see) snaps to exactly
                              # 0.0/1.0 - keeps the "steady color costs one
                              # write total, not one per period" property
                              # (see RgbPwm docstring) even if the picker's
                              # slider lands at, say, 0.995 instead of a
                              # clean 1.0.
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


def _snap_duty(value):
    """Clip to [0.0, 1.0] and snap near-boundary values to exactly 0.0/1.0
    (see DUTY_SNAP_EPSILON). Shared by RgbPwm.set_duty() - kept as a plain
    function rather than a method since it has no state, just arithmetic."""
    value = max(0.0, min(1.0, float(value)))
    if value < DUTY_SNAP_EPSILON:
        return 0.0
    if value > 1.0 - DUTY_SNAP_EPSILON:
        return 1.0
    return value


class Gpio:
    """Thin sysfs wrapper for exactly one output pin. See the module
    docstring for why sysfs and not libgpiod.

    Keeps the sysfs value file open for the life of the process (opened once
    in export(), never closed until the process exits) instead of
    open()/write()/close() on every toggle - measured on this board
    (SOFTWARE PWM section above) at ~8x lower latency and ~8x lower CPU per
    write. This matters for every pin now, not just the PWM'd ones - it was
    a straightforward win with no downside, since this file already commits
    to never unexporting (see the comment below the class), so the fd's
    lifetime already matches the pin's intended "stay exported and driven"
    lifetime.

    active_low: this pin drives an inverting KSP42(TA) pre-driver stage
    (see the RGB STATUS LIGHT section of the module docstring) - GPIO HIGH
    saturates the BJT and pulls the downstream MOSFET gate LOW (channel
    OFF); GPIO LOW lets the BJT's own collector pull-up charge the gate to
    +5V (channel ON). set_on() is the ONLY method that should be used on an
    active_low pin - it does the inversion, so a future reader (or a future
    edit to this file) cannot accidentally call the raw set()/toggle() and
    light the strip backwards. set()/toggle() stay raw and physical,
    unaware of active_low - that is what the siren pin (active_low=False,
    the default) still uses, completely unchanged from before RGB existed.
    RgbPwm (below) drives the RGB pins EXCLUSIVELY through set_on() too -
    the PWM math never touches raw GPIO levels, so the inversion still
    lives in exactly one place even under PWM."""

    def __init__(self, num, active_low=False):
        self.num = num
        self.active_low = active_low
        self.path = f"{GPIO_SYSFS}/gpio{num}"
        self._high = None  # unknown until first set()
        self._fd = None    # opened once in export(), kept open - see above

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
        if self._fd is None:
            self._fd = open(f"{self.path}/value", "w")

    def set(self, high):
        """Raw physical write: True drives the sysfs pin HIGH, full stop.
        Bypasses active_low entirely - see set_on() for the logic-level
        version an active_low pin (the three RGB pins) should actually use.
        Uses the persistent fd opened in export() - seek(0)+write+flush,
        not open()/write()/close() (see class docstring for the measured
        cost difference)."""
        if self._high == high:
            return  # sysfs write is cheap now, but still no reason to do it
                     # every tick - this also caps a PWM channel steady at
                     # duty 0.0/1.0 to about one real write, ever.
        self._fd.seek(0)
        self._fd.write("1" if high else "0")
        self._fd.flush()
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
    # The persistent value-fd added for PWM doesn't change any of this: it
    # is closed automatically by the kernel when the process exits (crash or
    # clean), same as any other fd - the PIN stays exported and at its last
    # driven electrical level regardless, which is the property this
    # comment is actually about.
    #
    # This matters MORE for the three RGB pins than it ever did for the
    # siren alone: they are active_low, so high-Z (unexported/undriven) does
    # NOT mean dark - see the BOOT-DEFAULT WARNING in the module docstring.
    # unexport()ing on stop would trade "held explicitly off" for "floating,
    # channel probably on" - the opposite of safe. Never add it here.


class RgbPwm:
    """Runs software PWM for the RGB channels from ONE dedicated thread,
    decoupled from the 1Hz Moonraker-poll main loop. See the module
    docstring's "SOFTWARE PWM FOR REAL DIMMING" section for the frequency
    choice, the measured write-latency/jitter numbers it's based on, and
    the CPU estimate.

    ONE THREAD FOR ALL THREE CHANNELS, not one thread per channel - chosen
    for two concrete reasons, not just "simpler":
      1. GIL/scheduler cost: one Python thread doing event-driven
         sleep-until-next-edge scheduling costs meaningfully less context-
         switching and GIL handoff overhead on this 4-core-but-RAM-
         constrained board than three threads independently spinning their
         own sleep loops - fewer threads competing for the GIL to do
         fundamentally the same class of work.
      2. Correctness: a SHARED, synchronized period boundary means all
         three channels restart together every cycle. Three independent
         threads targeting the "same" nominal frequency would NOT stay in
         phase with each other over time - separate time.sleep() calls
         accumulate separate, uncorrelated jitter - which doesn't break
         per-channel brightness (each channel's own average is still
         correct) but adds needless complexity for zero benefit versus one
         shared clock.
      Per-channel PHASE doesn't need to be synchronized for correctness
      (the eye integrates each channel's OWN time-average independently,
      or a color camera's; there's no reason R/G/B need to transition at
      literally the same instant) - the synchronization here is chosen for
      simplicity and thread-count discipline, not because unsynchronized
      channels would look wrong.

    ALGORITHM - trailing-edge PWM, synchronized period: at the start of
    period N (t0 + N*PWM_PERIOD_S), every channel with duty>0 is switched ON
    (one write each); channels at a genuine intermediate duty (0<duty<1)
    additionally get ONE scheduled OFF write later in that same period, at
    period_start + duty*PWM_PERIOD_S; channels at duty>=1.0 simply never get
    an OFF write and stay continuously on. Combined with Gpio.set()'s
    existing "skip the write if state is unchanged" cache, this means a
    channel steadily at duty 0.0 or 1.0 costs about ONE real sysfs write,
    ever - not one per period. Concretely: the lifecycle macros
    (RGB_OFF/RGB_PREPARING/RGB_PRINTING) only ever request exactly 0.0 or
    1.0 per channel, so normal lifecycle operation costs the SAME near-zero
    overhead the old on/off-only design had. The new per-period toggling
    overhead is only ever paid while a channel is genuinely sitting at an
    intermediate duty - i.e. only while someone is actively using the
    Mainsail brightness slider on a non-pure hue.

    FIRE-ALARM INTERLOCK: exactly one of {this thread, the main loop's
    alarm-blink code} may write to a given RGB Gpio at any instant. Enforced
    by _lock, acquired individually around EVERY write either side makes -
    NOT held for a whole period and NOT held for the whole alarm-blink
    sleep loop, so worst-case contention between the two writers is about
    one write's duration (tens of microseconds - see the module docstring's
    measured numbers), not a whole PWM period or a whole 300ms blink half-
    cycle. set_alarm(True) is checked before EACH individual write this
    thread makes (via _write()) - so a fire-alarm transition landing
    mid-period aborts the REST of that period's writes within about one
    write's latency, then this thread goes idle (see IDLE_POLL_S) until the
    alarm clears. See the module docstring's FIRE ALARM OVERRIDE section
    for the full race-freedom argument and the ~5ms worst-case bound either
    direction.

    CRASH HANDLING - found on advisor review, not in the original design:
    an uncaught exception inside a Python thread does NOT propagate
    anywhere - it just kills that one thread silently, while the process
    (and systemd's view of it) stays alive. Pre-PWM, this same class of
    failure was self-healing by accident: the old quiet-branch code
    re-asserted gpio_r/g/b.set_on(requested[...]) every single second from
    the MAIN thread, so a transient write failure fixed itself next poll,
    and the main loop itself dying took the whole process down with it
    (systemd Restart=always then gets a clean process restart). Moving the
    writes into a separate thread quietly gave up both properties - a dead
    PWM thread now freezes the channels at their last state FOREVER with no
    symptom anywhere. _run() wraps the whole loop in try/except and sets
    _crashed on any exception; main()'s loop polls is_healthy() and exits
    (triggering the same Restart=always recovery) if it ever goes False -
    see main() for the check.

    IDLE CPU (added after advisor review: the original always-loop-at-
    PWM_FREQUENCY_HZ design measured 3-4% of a core even at duty 0/1 - see
    module docstring's CPU IMPACT numbers - which is 200 wakeups/second of
    pure bookkeeping producing zero writes once a color is steady, the
    resting state this light spends nearly all its time in, INCLUDING at
    RGB_OFF). Whenever every channel's duty is exactly 0.0 or 1.0 (no
    channel needs a mid-period OFF edge), this thread writes the steady
    state once and then BLOCKS on _wake instead of re-entering the tight
    period loop to do nothing STEADY_WAKE_TIMEOUT_S times a second.
    set_duty()/set_alarm() both signal _wake, so a real change is picked up
    immediately, not after a poll delay; the timeout is a safety net against
    a theoretically missed wakeup, not the normal wake path - main()
    already calls set_duty()/set_alarm() roughly once a second regardless
    of whether values changed, so in practice this thread naturally wakes
    at about that same ~1Hz cadence while steady, not on the timeout."""

    IDLE_POLL_S = 0.005  # while alarm-overridden, how often this thread
                          # rechecks for release (via _wake, with this as a
                          # timeout/safety-net, not a fixed sleep - see
                          # _run()) - bounds "how long can the strip sit at
                          # its last alarm-branch state before real PWM
                          # resumes driving it toward the requested duty" to
                          # about this long. Deliberately a separate
                          # constant from PWM_PERIOD_S (even though it
                          # happens to equal it at the current frequency) so
                          # a future change to PWM_FREQUENCY_HZ doesn't
                          # silently change this bound too without a reader
                          # noticing.
    STEADY_WAKE_TIMEOUT_S = 2.0  # safety-net timeout while every channel is
                                  # steady at duty 0/1 (see IDLE CPU above) -
                                  # normal wakeups are event-driven via
                                  # _wake and arrive far sooner than this in
                                  # practice (main() calls set_duty()/
                                  # set_alarm() roughly once a second either
                                  # way); this only bounds how long a
                                  # hypothetical missed wakeup could wedge
                                  # this thread for.

    def __init__(self, channels, period_s):
        self._channels = channels  # {"r": Gpio, "g": Gpio, "b": Gpio}
        self._period_s = period_s
        self._lock = threading.Lock()  # guards _duty, _alarm_active, AND
                                        # every individual write to any of
                                        # the three Gpio objects above - see
                                        # class docstring, FIRE-ALARM
                                        # INTERLOCK.
        self._duty = {name: 0.0 for name in channels}
        self._alarm_active = False
        self._stop_event = threading.Event()
        self._wake = threading.Event()  # signaled by set_duty()/set_alarm()
                                         # to interrupt an idle/steady block
                                         # early - see class docstring, IDLE
                                         # CPU.
        self._crashed = threading.Event()  # set by _run() if the loop ever
                                            # raises - see class docstring,
                                            # CRASH HANDLING.
        self._thread = threading.Thread(
            target=self._run, name="rgb-pwm", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        """Stop the PWM thread and wait for it to actually exit. Must be
        called BEFORE any direct off-writes to the RGB Gpio objects (e.g.
        in main()'s finally: block) - otherwise this thread could still be
        mid-write when the direct writes happen, defeating the same
        interlock the fire-alarm handoff relies on. Safe to call even if
        the thread already died on its own (is_healthy() is False) - the
        Event is already set / thread already finished, join() returns
        immediately."""
        self._stop_event.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    def is_healthy(self):
        """False if the PWM thread has died from an uncaught exception -
        see class docstring, CRASH HANDLING. main() polls this and exits
        (letting systemd's Restart=always recover with a fresh thread)
        rather than silently leaving the channels frozen forever."""
        return not self._crashed.is_set()

    def set_duty(self, r, g, b):
        """Update the TARGET duty (0.0-1.0 per channel = fraction of each
        period the channel should be logically ON). Takes effect at the
        start of the next period, at most PWM_PERIOD_S (5ms) after this
        call - not mid-period, so one call never produces a half-applied
        period. See _snap_duty() for the near-0/near-1 snapping."""
        with self._lock:
            self._duty["r"] = _snap_duty(r)
            self._duty["g"] = _snap_duty(g)
            self._duty["b"] = _snap_duty(b)
        self._wake.set()

    def set_alarm(self, active):
        """True: this thread stops touching gpio_r/g/b entirely (the main
        loop's alarm-blink code takes over, unchanged). False: PWM resumes,
        starting a FRESH period immediately (see _run()'s resync comment) -
        no attempt to "catch up" periods skipped while overridden."""
        with self._lock:
            self._alarm_active = active
        self._wake.set()

    def _write(self, name, on):
        """Write one channel's logical on/off state, UNLESS the alarm
        override is active right now, in which case this writes nothing and
        returns False so the caller aborts the rest of the current period
        immediately. See class docstring, FIRE-ALARM INTERLOCK."""
        with self._lock:
            if self._alarm_active:
                return False
            self._channels[name].set_on(on)
            return True

    def _run(self):
        """Thin wrapper: see class docstring, CRASH HANDLING, for why this
        exists separately from _run_loop()."""
        try:
            self._run_loop()
        except Exception:
            log.exception(
                "RGB PWM thread crashed - channels are now frozen at "
                "whatever they were last written to. Setting _crashed so "
                "main() notices and exits (systemd Restart=always will "
                "bring up a fresh thread)."
            )
            self._crashed.set()

    def _run_loop(self):
        period = self._period_s
        t0 = time.monotonic()
        while not self._stop_event.is_set():
            with self._lock:
                alarm = self._alarm_active
                duty = dict(self._duty)

            if alarm:
                self._wake.wait(self.IDLE_POLL_S)
                self._wake.clear()
                t0 = time.monotonic()  # resync on next non-alarm iteration -
                                        # deliberately NOT accumulating a
                                        # "how many periods did we miss"
                                        # backlog to catch up on; the very
                                        # next period just starts now.
                continue

            # See class docstring, IDLE CPU: if no channel needs a
            # mid-period OFF edge (every duty is exactly 0.0 or 1.0), this
            # period's start-of-period writes below are the ONLY writes it
            # needs - block afterwards instead of re-entering this loop at
            # PWM_FREQUENCY_HZ to do nothing.
            needs_pwm = any(0.0 < d < 1.0 for d in duty.values())

            period_start = t0
            now = time.monotonic()
            if now < period_start:
                time.sleep(period_start - now)

            # Start-of-period writes: ON for any channel with duty>0
            # (including duty>=1, which then gets no OFF write at all this
            # period - continuously on), OFF for duty<=0.
            aborted = False
            for name, d in duty.items():
                if not self._write(name, d > 0.0):
                    aborted = True
                    break

            if aborted:
                t0 = time.monotonic()
                continue

            if not needs_pwm:
                self._wake.wait(self.STEADY_WAKE_TIMEOUT_S)
                self._wake.clear()
                t0 = time.monotonic()
                continue

            # Scheduled OFF writes for genuine intermediate duties only,
            # processed in time order. (needs_pwm being True is exactly
            # the condition that this generator is non-empty.)
            offs = sorted(
                (period_start + d * period, name)
                for name, d in duty.items()
                if 0.0 < d < 1.0
            )
            for off_time, name in offs:
                now = time.monotonic()
                if now < off_time:
                    time.sleep(off_time - now)
                if not self._write(name, False):
                    aborted = True
                    break

            t0 = time.monotonic() if aborted else period_start + period


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
    """Poll the color Klipper's [led rgb_strip] object is holding
    (printer-configs/rgb-status.cfg) - set via SET_LED, either by RGB_OFF/
    RGB_PREPARING/RGB_PRINTING or by Mainsail's native color-picker widget
    on rgb_strip. red_pin/green_pin/blue_pin are DUMMY AVR pins (AUX-4
    D23/D25/D27), nothing physically wired to them (same pattern as
    siren_armed) - Klipper only holds and reports the requested color.
    color_data is fractional (0.0-1.0 per channel, klippy/extras/led.py's
    LEDHelper.get_status() returns {'color_data': [(r,g,b,w)]}, confirmed
    against source - never quantized or thresholded by Klipper itself, see
    the module docstring's SOFTWARE PWM section for the hardware_pwm check).
    Returns the RAW per-channel float (clipped to [0,1] defensively), for
    RgbPwm.set_duty() to turn into real PWM - this function does NOT
    threshold or snap it itself (see DUTY_SNAP_EPSILON / _snap_duty(),
    applied downstream in set_duty()). Returns {"r": float, "g": float,
    "b": float}, or None on ANY failure - including "rgb-status.cfg isn't
    included in printer.cfg yet" or "rgb_strip isn't defined". Confirmed
    LIVE against this exact printer: querying an object Klipper doesn't
    know about returns HTTP 200 with an EMPTY {} for that object's status
    (not an HTTP error, does not poison other objects in the same query) -
    so this hits the KeyError/IndexError branch below and returns None,
    same as a real Moonraker outage would. The caller keeps the last known
    values in that case (initialized to all-0.0 = off, matching every other
    output_pin's startup default in this project) - so running this daemon
    before rgb-status.cfg is deployed, or before rgb_strip has ever been
    set, is inert and safe, not a crash risk."""
    resp = moonraker_get("/printer/objects/query?led%20rgb_strip")
    if resp is None:
        return None
    try:
        color = resp["result"]["status"]["led rgb_strip"]["color_data"][0]
        red, green, blue = color[0], color[1], color[2]
        return {
            "r": max(0.0, min(1.0, float(red))),
            "g": max(0.0, min(1.0, float(green))),
            "b": max(0.0, min(1.0, float(blue))),
        }
    except (KeyError, IndexError, TypeError, ValueError):
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
        "a KSP42(TA) level shifter, software-PWM'd (default 21 = PA21, "
        "physical pin 26)",
    )
    parser.add_argument(
        "--gpio-g", type=int, default=19,
        help="sysfs GPIO number for the RGB green channel, active_low, "
        "software-PWM'd (default 19 = PA19, physical pin 27)",
    )
    parser.add_argument(
        "--gpio-b", type=int, default=18,
        help="sysfs GPIO number for the RGB blue channel, active_low, "
        "software-PWM'd (default 18 = PA18, physical pin 28)",
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

    pwm = RgbPwm({"r": gpio_r, "g": gpio_g, "b": gpio_b}, PWM_PERIOD_S)
    pwm.start()

    log.info(
        f"Started, watching Moonraker at {MOONRAKER_BASE}, pulsing gpio{args.gpio} "
        f"(siren), driving RGB via software PWM at {PWM_FREQUENCY_HZ}Hz onto "
        f"gpio{args.gpio_r}/{args.gpio_g}/{args.gpio_b} (R/G/B, active_low)"
    )

    running = True

    def handle_stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    latched_source = None  # for edge-triggered logging only, not part of the contract
    requested = {"r": 0.0, "g": 0.0, "b": 0.0}  # last known REQUESTED DUTY
    # per channel (0.0-1.0), from rgb-status.cfg's [led rgb_strip]. Starts
    # all-off, matching every output_pin's value:0 startup default in this
    # project - stays all-off until rgb_requested() first succeeds. Only
    # ever handed to pwm.set_duty() - the main loop no longer writes
    # gpio_r/g/b directly except during a fire alarm (see below).
    red_blink_on = False  # blink phase for RED during a fire alarm
    last_poll = 0.0

    try:
        while running:
            if not pwm.is_healthy():
                # See RgbPwm's CRASH HANDLING docstring section: an
                # uncaught exception inside the PWM thread does not
                # propagate here on its own - without this check, a dead
                # PWM thread would silently freeze the RGB channels forever
                # while this process and systemd both stay "healthy". Exit
                # instead, so Restart=always gets a clean process restart
                # and a fresh thread - matching what the pre-PWM design got
                # for free from the main loop re-asserting gpio_r/g/b every
                # second itself.
                log.error(
                    "RGB PWM thread is dead (see traceback above) - exiting "
                    "so systemd restarts this daemon with a fresh thread"
                )
                break

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
                        log.info("Alarm source quiet - pin held LOW, RGB PWM resuming requested brightness")
                    latched_source = source

                if latched_source is None:
                    # Only refresh the REQUEST mirror while not alarming -
                    # about to be overridden anyway during an alarm, no
                    # point spending an extra Moonraker round trip on it.
                    new_requested = rgb_requested()
                    if new_requested is not None:
                        requested = new_requested

            # Keep the PWM thread's alarm flag in lockstep with
            # latched_source EVERY iteration (not just on transition) -
            # cheap (one lock+bool set) and self-healing against any future
            # edit that changes the polling cadence above. See RgbPwm's
            # FIRE-ALARM INTERLOCK for why this is race-free against the
            # direct writes in the branch below.
            pwm.set_alarm(latched_source is not None)

            if latched_source is not None:
                gpio_siren.toggle()
                red_blink_on = not red_blink_on
                gpio_r.set_on(red_blink_on)
                gpio_g.set_on(False)
                gpio_b.set_on(False)
                time.sleep(PULSE_HALF_PERIOD_S)
            else:
                gpio_siren.set(False)
                pwm.set_duty(requested["r"], requested["g"], requested["b"])
                time.sleep(POLL_INTERVAL_S)
    finally:
        # Runs on SIGTERM/SIGINT, on any unhandled exception in THIS loop,
        # and on the deliberate `break` above when the PWM thread has died
        # (pwm.is_healthy() False) alike - systemctl stop/restart, or a
        # systemd-triggered restart after that break, must never leave the
        # buzzer stuck on OR
        # the strip stuck lit (see the BOOT-DEFAULT WARNING in the module
        # docstring for why "stuck lit" is the RGB failure mode to guard
        # against here, not "stuck dark"). pwm.stop() runs FIRST and joins
        # the PWM thread before any direct writes below, so there is no
        # window where both this code and a still-running PWM thread could
        # write to the same pin at once (same interlock principle as the
        # fire-alarm handoff, applied to shutdown). All four pins are then
        # left EXPORTED (see the comment on Gpio, above the class body) so
        # they stay actively driven rather than reverting to high-Z.
        pwm.stop()
        gpio_siren.set(False)
        gpio_r.set_on(False)
        gpio_g.set_on(False)
        gpio_b.set_on(False)
        log.info("Stopped, siren pin low, RGB channels off")


if __name__ == "__main__":
    main()

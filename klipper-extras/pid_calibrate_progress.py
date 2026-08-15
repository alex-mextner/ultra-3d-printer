# pid_calibrate_progress.py - passive observer exposing REAL progress of
# Klipper's native PID_CALIBRATE command, which otherwise reports nothing
# until it finishes.
#
# SOURCE OF TRUTH for everything this module assumes about PID_CALIBRATE's
# internals (read directly over SSH from this printer's own installed
# Klipper, 2026-08-15, klippy/extras/pid_calibrate.py and
# klippy/extras/heaters.py - re-read the real source if the installed
# Klipper has since been updated, do not trust this comment blindly):
#
#   PID_CALIBRATE swaps the heater's `control` object for a ControlAutoTune
#   instance (heaters.py Heater.set_control()) for the duration of the run,
#   then restores the original control object when done. ControlAutoTune
#   runs a relay/bang-bang oscillation: it drives the heater to full power
#   until temperature >= target, then OFF until temperature drops to
#   `target - TUNE_PID_DELTA` (TUNE_PID_DELTA = 5.0, a literal constant in
#   pid_calibrate.py), and repeats. EVERY time it flips phase it calls
#   `self.heater.alter_target(...)` immediately after recording one peak
#   (`self.check_peaks()`, which appends exactly one entry to `self.peaks`
#   per flip - confirmed by reading the flip branches in
#   ControlAutoTune.temperature_update(), both call check_peaks() then
#   alter_target() together, nothing else touches self.peaks). The run
#   ends when `len(self.peaks) >= 12` and the heater is not mid-heating-
#   phase (ControlAutoTune.check_busy()) - 12 is a literal constant in that
#   method, not configurable.
#
# NONE of {self.peaks, self.heating, self.peak, ControlAutoTune itself} are
# exposed anywhere external - no send_event() call exists anywhere in
# pid_calibrate.py or heaters.py for this (confirmed by grepping both
# files), and Heater.get_status() only ever publishes
# {temperature, target, power} - it does not say which control algorithm
# (PID / watermark / autotune) is currently active.
#
# TWO WAYS TO GET REAL PROGRESS DATA EXIST, weighed here on purpose:
#   (a) Monkey-patch ControlAutoTune (or Heater.set_control / the
#       PID_CALIBRATE command handler itself) to publish len(self.peaks)
#       directly. Gives EXACT numbers with zero inference, but couples
#       this module to ControlAutoTune's private attribute names and
#       control-flow, which carry no public-API stability guarantee and
#       could change on any Klipper update without warning. Rejected for
#       that reason - it is the opposite of the "passive observer of a
#       stable, intentional public event" design this project already
#       proved out for axis_travel_report (github.com/alex-mextner/
#       klipper-axis-travel-report), which hooks a real Klipper printer
#       event (homing:homing_move_end), not private internals.
#   (b) Poll the heater's own PUBLIC, stable `target` field
#       (printer[<heater>].target, the same field every UI already reads)
#       from a [delayed_gcode] timer, and recognise the oscillation
#       pattern itself: two target values exactly TUNE_PID_DELTA (5.0)
#       apart, alternating repeatedly. Because every flip is exactly one
#       peak (see above), COUNTING TARGET FLIPS COUNTS PEAKS EXACTLY, not
#       approximately - this gives the same precision as (a) while only
#       ever touching the one field every heater status query already
#       returns. Chosen here.
#
# HONEST LIMITS of the chosen design, stated plainly rather than hidden:
#   - This is pattern recognition, not a direct signal that PID_CALIBRATE
#     is running. A adversarial or coincidental sequence of manual
#     SET_HEATER_TEMPERATURE calls exactly 5.0C apart, alternating
#     repeatedly, would be misidentified as a calibration. In practice this
#     is not a realistic false-positive risk (nobody hand-types a dozen
#     alternating 5C-apart target changes), but it is a real, named
#     limitation, not an oversight.
#   - "peaks_target: 12" and "TUNE_PID_DELTA: 5.0" are literal constants
#     copied from the source cited above. If a future Klipper version
#     changes either constant, this module's progress FRACTION will read
#     wrong (still counting real flips correctly, just against a stale
#     denominator / wrong gap threshold) - re-check pid_calibrate.py's
#     TUNE_PID_DELTA and the `len(self.peaks) < 12` literal in
#     ControlAutoTune.check_busy() before trusting this file's constants
#     again after any Klipper update.
#   - The "finished" transition is inferred from the target stopping its
#     oscillation for IDLE_TIMEOUT_S seconds, not from a real completion
#     event (none exists to observe) - see cmd_ helpers below for the
#     precise reset rule.
#
# ZERO BEHAVIOUR CHANGE: this module never calls anything on the heater,
# the toolhead, or PID_CALIBRATE itself. It only reads
# printer[<heater>].target/.temperature (the same public status fields any
# existing UI polls) on its own timer, and optionally calls
# M117/SET_DISPLAY_TEXT - confirmed safe to call from a delayed_gcode timer
# by reading klippy/extras/display_status.py on this printer 2026-08-15:
# both cmd_M117 and cmd_SET_DISPLAY_TEXT are one-line `self.message = ...`
# assignments with no toolhead.register_lookahead_callback and no
# print_time/idle_timeout interaction at all - unlike SET_PIN/
# SET_FAN_SPEED/M106 (this project's own documented idle_timeout-
# corruption trap, CLAUDE.md), display_status's setters do not go through
# that code path. Never raises - wrapped in try/except throughout, same
# reasoning as axis_travel_report: a bug in a passive poller must never be
# able to disturb a real heater/print in progress.

import logging

TUNE_PID_DELTA = 5.0   # pid_calibrate.py TUNE_PID_DELTA - literal constant
PEAKS_TARGET = 12       # pid_calibrate.py ControlAutoTune.check_busy() - literal
POLL_INTERVAL_S = 1.0    # fast enough to not miss a flip; real half-cycles on
                         # this hardware run tens of seconds to low minutes
IDLE_TIMEOUT_S = 45.0    # no new flip for this long -> consider it over.
                         # Chosen as a multiple of POLL_INTERVAL_S with real
                         # margin above the fastest plausible half-cycle
                         # this project has actually measured (extruder
                         # PID_CALIBRATE TARGET=220 today took well under
                         # this per half-cycle) - not a measured constant
                         # itself, just a conservative guess. Re-tune once
                         # this module has been watched through a couple of
                         # real live runs.


class PidCalibrateProgress:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        # heater_name -> list of watched status; only heaters explicitly
        # listed are polled, so this module never has to guess which
        # objects exist on an arbitrary machine.
        heaters_str = config.get('heaters', 'extruder,heater_bed')
        self.watched = [h.strip() for h in heaters_str.split(',') if h.strip()]
        self.announce = config.getboolean('announce', True)
        self.state = {}   # heater_name -> dict, see _blank_state()
        for name in self.watched:
            self.state[name] = self._blank_state()
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _blank_state(self):
        return {
            'active': False,
            'target_temp': 0.0,
            'peaks_done': 0,
            'peaks_target': PEAKS_TARGET,
            'current_temp': 0.0,
            'elapsed_s': 0.0,
            '_start_time': None,
            '_last_flip_time': None,
            '_last_target': None,
        }

    def _handle_ready(self):
        # Deferred to klippy:ready so lookup_object() calls below are safe
        # (heater objects only exist once config load has finished).
        self.heaters_obj = self.printer.lookup_object('heaters', None)
        self.display = self.printer.lookup_object('display_status', None)
        reactor = self.reactor
        reactor.register_timer(self._poll, reactor.monotonic() + POLL_INTERVAL_S)

    def _poll(self, eventtime):
        try:
            self._poll_once(eventtime)
        except Exception:
            logging.exception(
                "pid_calibrate_progress: poll failed (non-fatal, this "
                "module is read-only and never touches the heater)")
        return eventtime + POLL_INTERVAL_S

    def _poll_once(self, eventtime):
        if self.heaters_obj is None:
            return
        for name in self.watched:
            st = self.state[name]
            try:
                heater = self.heaters_obj.lookup_heater(name)
            except Exception:
                continue
            status = heater.get_status(eventtime)
            target = status.get('target', 0.0)
            temp = status.get('temperature', 0.0)
            st['current_temp'] = temp

            last_target = st['_last_target']
            if (last_target is not None and target != last_target
                    and abs(abs(target - last_target) - TUNE_PID_DELTA) < 0.01):
                # A flip matching PID_CALIBRATE's exact oscillation
                # signature - see module header for why this equals one
                # real peak in ControlAutoTune's own bookkeeping.
                if not st['active']:
                    st['active'] = True
                    st['peaks_done'] = 0
                    st['_start_time'] = eventtime
                    # The calibrate target is whichever of the two values
                    # is higher (heating phase target); the low one is
                    # target-TUNE_PID_DELTA, not the real requested TARGET=.
                    st['target_temp'] = max(target, last_target)
                st['peaks_done'] += 1
                st['_last_flip_time'] = eventtime
                if self.announce and self.display is not None:
                    self._respond(
                        "PID калибровка %s: пик %d/%d, %.1fC (цель %.0fC)"
                        % (name, min(st['peaks_done'], PEAKS_TARGET),
                           PEAKS_TARGET, temp, st['target_temp']))
            st['_last_target'] = target

            if st['active']:
                st['elapsed_s'] = eventtime - st['_start_time']
                last_flip = st['_last_flip_time'] or st['_start_time']
                if eventtime - last_flip > IDLE_TIMEOUT_S:
                    # No flip for a while -> treat as finished (or
                    # abandoned). Genuine completion vs. a genuinely-stuck
                    # calibration cannot be told apart from outside - see
                    # module header's "honest limits" note.
                    if self.announce and self.display is not None:
                        self._respond(
                            "PID калибровка %s: завершена (%d/%d пиков, %.0fs)"
                            % (name, min(st['peaks_done'], PEAKS_TARGET),
                               PEAKS_TARGET, st['elapsed_s']))
                    self.state[name] = self._blank_state()
                    self.state[name]['current_temp'] = temp
                    self.state[name]['_last_target'] = target

    def _respond(self, msg):
        # display_status.py has no public setter - M117/SET_DISPLAY_TEXT
        # both just do `self.message = ...` directly (confirmed by reading
        # cmd_M117/cmd_SET_DISPLAY_TEXT in display_status.py on this
        # printer, 2026-08-15) - reusing the same live object and doing the
        # identical plain assignment reproduces that exactly, without
        # going through the gcode command parser at all.
        try:
            self.display.message = msg
        except Exception:
            logging.exception("pid_calibrate_progress: display update failed")

    def get_status(self, eventtime):
        out = {}
        for name, st in self.state.items():
            out[name] = {
                'active': st['active'],
                'target_temp': st['target_temp'],
                'peaks_done': min(st['peaks_done'], st['peaks_target']),
                'peaks_target': st['peaks_target'],
                'current_temp': st['current_temp'],
                'elapsed_s': round(st['elapsed_s'], 1),
            }
        return out


def load_config(config):
    return PidCalibrateProgress(config)

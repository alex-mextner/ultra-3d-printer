# axis_travel_report.py - passive observer of real homing travel distance,
# PLUS a MEASURE_HOME command that is the only way to actually get a
# meaningful number out of it for a from-the-far-limit measurement.
#
# PURPOSE: expose the physical distance a stepper travels during a homing
# move, before Klipper's own G28 code path (klippy/extras/homing.py) throws
# it away and rewrites the reported axis position to the config's
# position_endstop. Built 2026-08-08 so this printer's real, unmeasured
# axis lengths (Y/Z position_max) can be determined by pushing an axis to
# its far mechanical limit by hand and homing toward the switch, instead of
# a tape measure - see docs/printer-status.md for the "why".
#
# =========================================================================
# 2026-08-15 CORRECTION - the original design (SET_KINEMATIC_POSITION then
# plain G28) does NOT work, and cannot ever work. Caught on the first real
# test: measuring stepper_y (position_max 197, position_endstop 0) reported
# start=295.500mm - exactly 1.5 * 197. Not noise: that is Klipper's own
# internal safety-search-start formula, not our SET_KINEMATIC_POSITION value.
#
# Root cause, read directly from this printer's installed Klipper (SSH,
# 2026-08-15 - re-read the real source if the installed Klipper has since
# changed, do not trust this comment blindly):
#
#   klippy/kinematics/cartesian.py, CartKinematics.home_axis():
#     forcepos = list(homepos)   # homepos[axis] = hi.position_endstop
#     if hi.positive_dir:
#         forcepos[axis] -= 1.5 * (hi.position_endstop - position_min)
#     else:
#         forcepos[axis] += 1.5 * (position_max - hi.position_endstop)
#     homing_state.home_rails([rail], forcepos, homepos)
#
#   klippy/extras/homing.py, Homing._do_home_rails() -> _set_start_position():
#     self.toolhead.set_position(startpos, homing_axes=homing_axes)
#   This runs BEFORE the first HomingMove is even constructed, and
#   OVERWRITES whatever toolhead position existed before G28 ran (including
#   anything SET_KINEMATIC_POSITION had just set) with the synthesized
#   forcepos above. StepperPosition.__init__ (same file) then captures
#   start_cmd_pos AFTER this overwrite - so for a plain G28, start_cmd_pos
#   is ALWAYS "position_endstop +/- 1.5*(configured range)", a value
#   computed purely from config, carrying zero information about where the
#   axis physically was. This is deliberate on Klipper's part: it's what
#   lets G28 safely home an axis whose position is totally unknown, by
#   guaranteeing the search move is long enough to reach the switch
#   regardless of the (untrusted) starting point. It also means a plain G28
#   can never be used to measure a real physical distance from wherever a
#   human pushed the axis to - the "before" position it uses isn't real.
#
# THE FIX: bypass Homing._set_start_position() entirely by driving the
# search move through PrinterHoming.manual_home() (same file), which calls
# HomingMove.homing_move() DIRECTLY - the same primitive probe.py uses for
# PROBE_CALIBRATE-style moves. manual_home() never touches toolhead position
# before moving, so whatever SET_KINEMATIC_POSITION (or this module's own
# MEASURE_HOME) set immediately prior is exactly what StepperPosition
# captures as start_cmd_pos. See cmd_MEASURE_HOME below - this is now the
# ONLY supported way to get a real number out of this module. The old
# HOME_AND_MEASURE macro (printer-configs/axis-travel-measure.cfg) is kept
# only as a one-line wrapper around MEASURE_HOME, unchanged from the
# outside, fixed underneath.
#
# The passive homing:homing_move_end observer below (RESET_AXIS_TRAVEL_
# REPORT / AXIS_TRAVEL_REPORT / get_status) is UNCHANGED and was never
# itself buggy - it faithfully reports whatever start_cmd_pos Klipper's own
# StepperPosition computed. The bug was entirely in what fed it: a plain
# G28's start_cmd_pos is synthetic and physically meaningless, while
# manual_home()'s is real. If you ever see a plain G28 (not MEASURE_HOME)
# reported here, expect exactly "position_endstop +/- 1.5*range" and
# nothing else - that is correct behavior for this module, not a fluke.
# =========================================================================
#
# 2026-08-15 CORRECTION #3 - the anchor formula above ("position_max -
# margin" / "position_min + margin") was itself an invented number standing
# in for a real one, and it broke on the very first axis where it mattered.
# Caught live: MEASURE_HOME AXIS=Z failed with "No trigger on stepper_z
# after full movement" after a real, complete ~188.5mm search (38s at Z's
# 5mm/s homing_speed - matches position_max(190, still an unmeasured
# PLACEHOLDER at [stepper_z]) - margin(1) - position_endstop(0.5) exactly).
# Z's real travel is bigger than its own placeholder position_max - the
# exact situation this tool exists to fix - so anchoring the search bound
# to that same placeholder was circular: the number MEASURE_HOME is meant
# to discover was also the thing limiting how far it was willing to look
# for it.
#
# The user's fix, and it is correct: "должно работать аналогично хомингу!"
# (it should work the same way normal homing does). Supporting evidence -
# not just intuition: the very first-ever plain G28 Z on this machine,
# run earlier the same session with this SAME placeholder position_max=190,
# succeeded cleanly. Why: a plain G28's own forcepos search bound (see
# "CORRECTION #1" above) is FAR more generous than "configured range minus
# a 1mm margin" - re-confirmed fresh against the real installed Klipper
# (klippy/kinematics/cartesian.py, CartKinematics.home_axis(), unchanged
# since CORRECTION #1's citation):
#
#   position_min, position_max = rail.get_range()
#   hi = rail.get_homing_info()
#   homepos = [None, None, None, None]; homepos[axis] = hi.position_endstop
#   forcepos = list(homepos)
#   if hi.positive_dir:
#       forcepos[axis] -= 1.5 * (hi.position_endstop - position_min)
#   else:
#       forcepos[axis] += 1.5 * (position_max - hi.position_endstop)
#   homing_state.home_rails([rail], forcepos, homepos)
#
# For Z (positive_dir=False, position_endstop=0.5, position_max=190):
# forcepos = 0.5 + 1.5*(190-0.5) = 284.75 - which is EXACTLY the number
# this session independently observed in a raw toolhead-position query
# during that first successful G28 Z, confirming this formula is what
# actually ran and worked on this exact axis with this exact placeholder.
#
# So: MEASURE_HOME now computes its anchor with this IDENTICAL formula,
# not a smaller hand-picked one - it borrows G28's own already-proven
# search generosity instead of inventing a new, tighter number that has
# to be independently re-justified. No manual per-call override parameter
# was added for this: the whole point is that a human should not have to
# supply a distance the code can already compute safely by copying a
# formula this machine has already exercised successfully. (toolhead.
# set_position() - called here and by SET_KINEMATIC_POSITION, see
# klippy/extras/force_move.py cmd_SET_KINEMATIC_POSITION, re-read fresh -
# performs no position_min/max range validation of its own, so an anchor
# outside the configured range is exactly as legal here as it already is
# for G28's own forcepos; nothing new is being relied on.)
#
# What did NOT change: CORRECTION #2's fix (raw MCU step counts for
# travel_mm, never a re-derived commanded position) is completely
# independent of what numeric value the anchor holds - the anchor's ONLY
# job is giving the search move enough legal room to physically reach the
# switch, exactly like forcepos does for G28. Nothing about how the real
# trigger point gets captured or reported needed to change.
# =========================================================================
#
# stepper.mcu_to_commanded_position(mcu_pos) (klippy/stepper.py) converts an
# MCU step count to a commanded mm position - reusing Klipper's own
# conversion, not a reimplementation.
#
# HARD CAVEAT, inherent to open-loop steppers with no position feedback, NOT
# a gap in this module: MEASURE_HOME's result is only correct if nobody
# moves the axis by hand between finishing the manual reposition and
# calling MEASURE_HOME. Klipper has no way to know a by-hand move happened,
# and neither can this module, or any module - MEASURE_HOME's own internal
# SET_KINEMATIC_POSITION-equivalent step is what re-establishes a trusted
# reference at the moment it runs, not before.
#
# ZERO BEHAVIOR CHANGE to G28: the passive observer only reads
# hmove.stepper_positions in an event handler that fires after the real
# move has already physically completed - it never calls any
# toolhead/stepper/kinematics method that could affect G28's motion, speed,
# retract distance, or final reported position. MEASURE_HOME is a
# deliberately SEPARATE, explicitly-invoked command with its own physical
# motion (see below) - it never runs as a side effect of a normal G28.
#
# All passive recording work is wrapped in try/except Exception (see
# _on_homing_move_end) because printer.send_event() (klippy/klippy.py)
# does NOT catch handler exceptions - it is a plain
# "[cb(*params) for cb in handlers]". An unhandled exception escaping this
# module's passive handler would propagate up through HomingMove.
# homing_move() and could abort the retract/second-pass homing step for
# the axis being homed RIGHT NOW, including during a normal G28. On this
# machine G28 Z drives the bed UP toward the nozzle (see
# docs/printer-status.md), so the passive handler must never be the thing
# that turns a normal homing move into an aborted one. Hence: catch
# everything there, log, never raise.
#
# cmd_MEASURE_HOME is different: it is the active, explicitly-requested
# operation itself (like G28 or FORCE_MOVE), so letting a real failure
# (e.g. "No trigger on stepper_y after full movement") raise as a normal
# gcode command_error is CORRECT here, not a bug to swallow - that is how
# every other homing-family command in Klipper reports failure, and
# swallowing it would hide a real problem (axis never reached the switch)
# behind a fabricated-looking success.
#
# =========================================================================
# 2026-08-15 CORRECTION #2 - trigger_mm/travel_mm were STILL synthetic
# after the manual_home() fix above, caught the same way: two live
# MEASURE_HOME AXIS=Y runs, one after ~196mm of real carriage travel
# (~19.5s wall time at homing_speed 10), one with the carriage already
# sitting at the switch beforehand (0.34s wall time, negligible real
# travel possible). Both reported the IDENTICAL "start=196.000mm
# trigger=0.000mm travel=-196.000mm" - proof trigger_mm carried zero real
# information; it was silently just re-deriving hi.position_endstop no
# matter what actually happened physically.
#
# Root cause, re-read fresh from this printer's installed Klipper
# (2026-08-15, same caveat as above - re-read the real source if it has
# since changed):
#
#   klippy/extras/homing.py, HomingMove.homing_move(), non-probe branch
#   (probe_pos=False - what manual_home() passes here):
#     haltpos = trigpos = movepos
#     ...
#     self.toolhead.set_position(haltpos)   # <- BEFORE homing_move_end
#     ...
#     self.printer.send_event("homing:homing_move_end", self)
#
#   klippy/stepper.py, MCU_stepper.set_position() -> _set_mcu_position():
#     mcu_pos_dist = mcu_pos * self._step_dist
#     self._mcu_position_offset = mcu_pos_dist - self.get_commanded_position()
#   where mcu_pos = self.get_mcu_position() = the CURRENT real raw step
#   count at the moment set_position() is called.
#
# So: the instant a homing move's endstop triggers, Klipper's OWN final
# self.toolhead.set_position(haltpos) call - which runs on every ordinary
# non-probe homing move, ours included, and fires BEFORE our event handler
# ever sees anything - recalibrates _mcu_position_offset so that whatever
# raw step count the stepper is AT RIGHT THEN (which is, by construction,
# essentially sp.trig_pos itself, since the move just stopped there) maps
# to haltpos/movepos - which cmd_MEASURE_HOME sets to hi.position_endstop.
# Calling stepper.mcu_to_commanded_position(sp.trig_pos) from our handler
# AFTERWARD therefore returns ~hi.position_endstop by construction, for
# ANY real trig_pos - the real physical trigger location has already been
# thrown away by the time we read it, exactly the same class of bug as
# CORRECTION #1, just one layer deeper.
#
# THE FIX: never call mcu_to_commanded_position(trig_pos) after the fact.
# sp.start_pos and sp.trig_pos (klippy/extras/homing.py StepperPosition -
# stepper.get_mcu_position() / stepper.get_past_mcu_position()) are RAW
# MCU step counts, read straight from hardware/step-compress history at
# capture time - neither goes through any offset and neither is retroactively
# altered by a later set_position() call. Their difference, times the
# stepper's get_step_dist() (klippy/stepper.py: self._rotation_dist /
# self._steps_per_rotation, a fixed mm-per-step scalar that already carries
# the correct sign because mcu_pos itself is a signed step count - proven by
# mcu_to_commanded_position's own formula, mcu_pos * step_dist - offset,
# needing no separate sign flip), is the true physical distance traveled
# between this module's own reference point and the real endstop trigger -
# entirely independent of whatever Klipper's internal commanded-position
# bookkeeping does afterward. sp.start_cmd_pos (used for start_mm) is NOT
# affected by this bug and needed no change: StepperPosition.__init__ reads
# it via the SAME mcu_to_commanded_position() call, but that read happens at
# the very top of homing_move(), before this method's own set_position()
# calls have run even once - so it still uses the correct, not-yet-
# recalibrated offset (ours, from cmd_MEASURE_HOME's own toolhead.
# set_position(startpos, ...) call made just before manual_home() runs).
# =========================================================================

import logging

AXIS_NAMES = ['x', 'y', 'z']

class AxisTravelReport:
    def __init__(self, config):
        self.printer = config.get_printer()
        # stepper_name -> {'start_mm': float, 'trigger_mm': float,
        #                   'travel_mm': float, 'passes': int}
        self.reports = {}
        self.printer.register_event_handler(
            "homing:homing_move_end", self._on_homing_move_end)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command(
            'RESET_AXIS_TRAVEL_REPORT', self.cmd_RESET_AXIS_TRAVEL_REPORT,
            desc=self.cmd_RESET_AXIS_TRAVEL_REPORT_help)
        gcode.register_command(
            'AXIS_TRAVEL_REPORT', self.cmd_AXIS_TRAVEL_REPORT,
            desc=self.cmd_AXIS_TRAVEL_REPORT_help)
        gcode.register_command(
            'MEASURE_HOME', self.cmd_MEASURE_HOME,
            desc=self.cmd_MEASURE_HOME_help)

    def _on_homing_move_end(self, hmove):
        # MUST NOT raise - see module docstring "ZERO BEHAVIOR CHANGE".
        try:
            self._record(hmove)
        except Exception:
            logging.exception(
                "axis_travel_report: failed to record a homing pass "
                "(non-fatal - the homing move itself already completed "
                "unaffected by this)")

    def _record(self, hmove):
        for sp in hmove.stepper_positions:
            if sp.trig_pos is None or sp.start_cmd_pos is None:
                continue
            name = sp.stepper_name
            rec = self.reports.get(name)
            if rec is None:
                # First pass seen since the last reset: this is the only
                # trustworthy "before any motion in this move" reference,
                # so it is captured once and never overwritten by a later
                # (retract/re-approach) pass. For MEASURE_HOME there is
                # only ever one pass anyway (see cmd_MEASURE_HOME).
                # start_pos (raw MCU steps) is kept alongside start_mm
                # specifically so travel below can be computed from raw
                # step counts on every pass, not just re-derived from a
                # commanded-position conversion that a later pass's own
                # set_position() call would have already corrupted - see
                # "2026-08-15 CORRECTION #2" in the module docstring.
                rec = {'start_mm': sp.start_cmd_pos,
                       'start_pos': sp.start_pos, 'passes': 0}
                self.reports[name] = rec
            rec['passes'] += 1
            # Raw MCU step delta, NOT mcu_to_commanded_position(trig_pos) -
            # see "2026-08-15 CORRECTION #2" above for why that call is
            # unusable here (silently returns ~hi.position_endstop for any
            # real trig_pos, confirmed live). sp.trig_pos and rec['start_pos']
            # are both raw hardware step counts, immune to any set_position()
            # recalibration that happens later in the same homing_move().
            travel_mm = (sp.trig_pos - rec['start_pos']) * sp.stepper.get_step_dist()
            rec['trigger_mm'] = rec['start_mm'] + travel_mm
            rec['travel_mm'] = travel_mm

    cmd_RESET_AXIS_TRAVEL_REPORT_help = (
        "Clear recorded axis-travel data (all steppers, or one via "
        "STEPPER=<name>). MEASURE_HOME calls this itself for its own "
        "stepper before moving - only needed by hand when using plain G28")
    def cmd_RESET_AXIS_TRAVEL_REPORT(self, gcmd):
        stepper = gcmd.get('STEPPER', None)
        if stepper is None:
            self.reports.clear()
            gcmd.respond_info("axis_travel_report: cleared all steppers")
        else:
            self.reports.pop(stepper, None)
            gcmd.respond_info("axis_travel_report: cleared %s" % (stepper,))

    cmd_AXIS_TRAVEL_REPORT_help = (
        "Print recorded homing travel distance for each stepper since the "
        "last RESET_AXIS_TRAVEL_REPORT")
    def cmd_AXIS_TRAVEL_REPORT(self, gcmd):
        if not self.reports:
            gcmd.respond_info("axis_travel_report: no homing recorded yet "
                               "since startup/last reset")
            return
        for name, rec in sorted(self.reports.items()):
            gcmd.respond_info(
                "axis_travel_report %s: start=%.3fmm trigger=%.3fmm "
                "travel=%.3fmm (%d pass%s)"
                % (name, rec['start_mm'], rec['trigger_mm'],
                   rec['travel_mm'], rec['passes'],
                   '' if rec['passes'] == 1 else 'es'))

    cmd_MEASURE_HOME_help = (
        "MEASURE_HOME AXIS=<X|Y|Z> - home one axis from a real, known "
        "reference positioned exactly like a plain G28's own forcepos "
        "search-start (see module docstring, CORRECTION #3), and record "
        "the true physical travel to the switch. Unlike plain G28, this "
        "does not overwrite that reference before the move (see "
        "CORRECTION #1) - the reported number is real. Requires the axis "
        "to already be physically pushed by hand to its far mechanical "
        "limit before calling this.")
    def cmd_MEASURE_HOME(self, gcmd):
        axis_name = gcmd.get('AXIS').lower()
        if axis_name not in AXIS_NAMES:
            raise gcmd.error("MEASURE_HOME: AXIS must be one of X, Y, Z")
        axis = AXIS_NAMES.index(axis_name)

        toolhead = self.printer.lookup_object('toolhead')
        kin = toolhead.get_kinematics()
        rail = kin.rails[axis]
        hi = rail.get_homing_info()
        position_min, position_max = rail.get_range()

        # Anchor = G28's OWN forcepos formula (cartesian.py, CartKinematics.
        # home_axis(), quoted verbatim in CORRECTION #3 above) - not a
        # smaller hand-picked distance. This machine's [stepper_*]
        # position_min/position_max are themselves sometimes just
        # placeholders (that's the whole reason this tool exists), so
        # bounding the search to "configured range minus a margin" can
        # come up short before reaching the real switch - confirmed live on
        # Z. G28's 1.5x-past-the-endstop formula is already proven to work
        # on this exact axis with this exact placeholder (see CORRECTION
        # #3's 284.75 cross-check), so MEASURE_HOME borrows it exactly
        # rather than inventing its own, tighter number.
        # hi.positive_dir is Klipper's own already-computed direction
        # (klippy/stepper.py: inferred from position_endstop's side of the
        # range unless overridden) - True means homing searches toward
        # +coordinate (endstop on MAX, e.g. this machine's X), False means
        # it searches toward -coordinate (endstop on MIN, e.g. Y and Z on
        # this machine).
        if hi.positive_dir:
            anchor = hi.position_endstop - 1.5 * (hi.position_endstop
                                                    - position_min)
        else:
            anchor = hi.position_endstop + 1.5 * (position_max
                                                    - hi.position_endstop)

        # Establish the real reference: this IS what SET_KINEMATIC_POSITION
        # does under the hood (klippy/extras/force_move.py,
        # cmd_SET_KINEMATIC_POSITION) - calling toolhead.set_position()
        # directly here instead keeps the whole measurement atomic in one
        # gcode command, with no separate SET_KINEMATIC_POSITION step for
        # the caller to get wrong or forget.
        curpos = toolhead.get_position()
        startpos = list(curpos)
        startpos[axis] = anchor
        toolhead.set_position(startpos, homing_axes=axis_name)

        # Clear any stale report for this stepper so AXIS_TRAVEL_REPORT
        # below reflects only this measurement, not a leftover from an
        # earlier plain-G28 or previous MEASURE_HOME call.
        stepper_name = 'stepper_' + axis_name
        self.reports.pop(stepper_name, None)

        # Target position for the search: same position_endstop a plain
        # G28 would land on, other axes left at their current (just-set)
        # values - mirrors Homing._fill_coord()'s "None means keep
        # current" behavior, done explicitly since manual_home() (unlike
        # the G28 path) does not fill in Nones itself.
        movepos = list(toolhead.get_position())
        movepos[axis] = hi.position_endstop

        # The actual fix: manual_home() (klippy/extras/homing.py,
        # PrinterHoming.manual_home) calls HomingMove.homing_move()
        # directly - the same primitive probe.py's PROBE_CALIBRATE uses -
        # WITHOUT going through Homing._set_start_position()'s forcepos
        # overwrite. One pass only, at hi.speed (this axis's already-
        # cautious configured homing_speed, e.g. Y's 10 / Z's default 5 -
        # same margin this project has used for every other first-move on
        # this machine). Deliberately skips the coarse+retract two-pass
        # dance a plain G28 does: that refinement exists for print-quality
        # homing precision, not for a one-off measurement that gets a
        # several-mm safety margin applied to it afterward anyway (see
        # docs/printer-status.md for how X/Y position_max were set).
        # check_triggered=True (Klipper's own default for a real homing
        # move) means a failure to reach the switch raises a normal
        # command_error, exactly like a plain G28 would - not swallowed.
        homing = self.printer.lookup_object('homing')
        rail_endstops = rail.get_endstops()
        homing.manual_home(toolhead, rail_endstops, movepos, hi.speed,
                            probe_pos=False, triggered=True,
                            check_triggered=True)

        # homing:homing_move_end already fired during manual_home() above
        # and populated self.reports[stepper_name] via the normal passive
        # path - just print it.
        self.cmd_AXIS_TRAVEL_REPORT(gcmd)

    def get_status(self, eventtime):
        return {
            name: {
                'start_mm': rec['start_mm'],
                'trigger_mm': rec.get('trigger_mm'),
                'travel_mm': rec.get('travel_mm'),
                'passes': rec['passes'],
            }
            for name, rec in self.reports.items()
        }

def load_config(config):
    return AxisTravelReport(config)

# axis_travel_report.py - passive observer of real homing travel distance.
#
# PURPOSE: expose the physical distance a stepper travels during a homing
# move, before Klipper's own G28 code path (klippy/extras/homing.py) throws
# it away and rewrites the reported axis position to the config's
# position_endstop. Built 2026-08-08 so this printer's real, unmeasured
# axis lengths (Y/Z position_max) can be determined by pushing an axis to
# its far mechanical limit by hand and homing toward the switch, instead of
# a tape measure - see docs/printer-status.md for the "why".
#
# SOURCE OF TRUTH for this module's design: klippy/extras/homing.py on this
# printer's own installed Klipper (git rev 7046bd00ef5c30dec6febc724f8d22
# 967433c45c, read directly over SSH 2026-08-08 before writing any of this -
# do not trust this comment blindly if the installed Klipper has since been
# updated, re-read the real source):
#
#   - HomingMove.homing_move() fires event "homing:homing_move_end" (self)
#     AFTER toolhead.set_position() has already been called for this pass,
#     but the per-stepper StepperPosition objects it built
#     (self.stepper_positions) still hold the real numbers from BEFORE any
#     position_endstop remapping:
#       .start_cmd_pos - commanded position (mm) at the moment THIS PASS of
#                        homing_move() began (set in StepperPosition.__init__
#                        from stepper.get_mcu_position() / mcu_to_commanded_
#                        position(), i.e. Klipper's own tracked position,
#                        not anything this module computes independently)
#       .trig_pos       - raw MCU step position at the endstop's precise
#                        trigger time (StepperPosition.note_home_end(),
#                        captured via stepper.get_past_mcu_position())
#   - stepper.mcu_to_commanded_position(mcu_pos) (klippy/stepper.py:183)
#     converts an MCU step count to a commanded mm position - reusing
#     Klipper's own conversion, not a reimplementation.
#   - A typical G28 fires homing_move_end TWICE per rail when
#     homing_retract_dist is nonzero: once for the fast coarse pass, once for
#     the slow retract-and-reapproach precision pass
#     (Homing._do_home_rails, same file). This module keeps the FIRST pass's
#     start_cmd_pos (the true starting point before any homing motion began
#     in this G28) and always updates to the LATEST pass's trigger position
#     (the more precise of the two) - so the reported travel reflects the
#     full, full-precision distance across the whole G28, not just the short
#     final retract hop.
#
# HARD CAVEAT, inherent to open-loop steppers with no position feedback, NOT
# a gap in this module: the reported number is only correct if Klipper's own
# commanded position was accurate at the moment THIS pass began. If the axis
# was moved BY HAND (motor disabled, or forced against an enabled motor)
# between the last trustworthy reference and this G28, Klipper has no way to
# know it happened - and neither can this module, or any module. Fix:
# immediately after finishing a manual reposition and before running G28,
# run
#   SET_KINEMATIC_POSITION <AXIS>=<any legal value> SET_HOMED=<axis>
# The specific value does not matter and is not a measurement - it cancels
# out of the reported delta (travel_mm = trigger_mm - start_mm) - it only
# has to be a value Klipper's position_min/position_max will accept so the
# following homing move is allowed to run at all.
#
# ZERO BEHAVIOR CHANGE: this module ONLY reads hmove.stepper_positions in an
# event handler that fires after the real move has already physically
# completed (toolhead.set_position() already ran). It never calls any
# toolhead/stepper/kinematics method that could affect motion, speed,
# retract distance, or the final reported position - it is a read-only
# observer of data Klipper already computed for its own bookkeeping.
#
# All recording work is wrapped in try/except Exception (see
# _on_homing_move_end) because printer.send_event() (klippy/klippy.py:226-
# 227, confirmed by reading it on this printer) does NOT catch handler
# exceptions - it is a plain "[cb(*params) for cb in handlers]". An
# unhandled exception escaping this module's handler would propagate up
# through HomingMove.homing_move() and could abort the retract/second-pass
# homing step for the axis being homed RIGHT NOW. On this machine G28 Z
# drives the bed UP toward the nozzle (see docs/printer-status.md), so this
# module must never be the thing that turns a normal homing move into an
# aborted one. Hence: catch everything, log, never raise.

import logging

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
            trig_mm = sp.stepper.mcu_to_commanded_position(sp.trig_pos)
            rec = self.reports.get(name)
            if rec is None:
                # First pass seen since the last reset: this is the only
                # trustworthy "before any motion in this G28" reference,
                # so it is captured once and never overwritten by a later
                # (retract/re-approach) pass.
                rec = {'start_mm': sp.start_cmd_pos, 'passes': 0}
                self.reports[name] = rec
            rec['passes'] += 1
            rec['trigger_mm'] = trig_mm
            rec['travel_mm'] = trig_mm - rec['start_mm']

    cmd_RESET_AXIS_TRAVEL_REPORT_help = (
        "Clear recorded axis-travel data (all steppers, or one via "
        "STEPPER=<name>) before a fresh manual-reposition-then-home "
        "measurement")
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

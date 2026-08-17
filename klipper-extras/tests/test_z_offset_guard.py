# Standalone harness for z_offset_guard.py - runs in klippy-env on the printer,
# with NO Klipper runtime: the module imports only `logging`, so everything it
# touches is stubbed here with objects that reproduce the real ones' behaviour
# read out of this printer's own Klipper source.
#
# The point is not "the code runs". The point is the PROPERTY:
#   after any Z_OFFSET_APPLY_ENDSTOP the guard let through, the resulting
#   (position_endstop, position_min, position_max) triple must PASS the exact
#   validation in klippy/stepper.py GenericPrinterRail.__init__ that bricked
#   this machine twice - or the guard must have refused and written nothing.
# klipper_boot_check() below is that validation, transcribed line for line.

import sys
try:
    import configparser
except ImportError:
    import ConfigParser as configparser


# --------------------------------------------------------------------------
# klippy/stepper.py GenericPrinterRail.__init__, transcribed. Returns None if
# Klipper would boot, or the error string it would raise.
# --------------------------------------------------------------------------
def klipper_boot_check(position_endstop, position_min, position_max,
                       homing_positive_dir=None):
    if not (position_max > position_min):
        return "position_max must be above position_min"
    if position_endstop < position_min or position_endstop > position_max:
        return ("position_endstop in section 'stepper_z' must be between"
                " position_min and position_max")
    if homing_positive_dir is None:
        axis_len = position_max - position_min
        if position_endstop <= position_min + axis_len / 4.:
            homing_positive_dir = False
        elif position_endstop >= position_max - axis_len / 4.:
            homing_positive_dir = True
        else:
            return "Unable to infer homing_positive_dir in section 'stepper_z'"
        # NOTE: a True here is not a Klipper error, but on THIS machine it
        # means G28 Z would drive the bed away from the Z-MIN switch. The
        # harness flags it separately, see check_run().
        return ('DIRECTION_FLIPPED' if homing_positive_dir else None)
    if ((homing_positive_dir and position_endstop == position_min)
            or (not homing_positive_dir and position_endstop == position_max)):
        return "Invalid homing_positive_dir / position_endstop"
    return None


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------
class StubError(Exception):
    pass


class StubGCode:
    error = StubError

    def __init__(self):
        self.responses = []
        self.commands = {}

    def register_command(self, cmd, func, desc=None):
        if func is None:
            return self.commands.pop(cmd, None)
        self.commands[cmd] = func

    def respond_info(self, msg, log=True):
        self.responses.append(msg)


class StubConfigAutoSave:
    """configfile.ConfigAutoSave - the object that actually owns fileconfig."""

    def __init__(self):
        self.fileconfig = configparser.RawConfigParser(strict=False)
        self.save_config_pending = False
        self.status_save_pending = {}
        self.sets = []

    def set(self, section, option, value):
        if not self.fileconfig.has_section(section):
            self.fileconfig.add_section(section)
        svalue = str(value)
        self.fileconfig.set(section, option, svalue)
        pending = dict(self.status_save_pending)
        if section not in pending or pending[section] is None:
            pending[section] = {}
        else:
            pending[section] = dict(pending[section])
        pending[section][option] = svalue
        self.status_save_pending = pending
        self.save_config_pending = True
        self.sets.append((section, option, svalue))

    def get_status(self, eventtime):
        return {'save_config_pending': self.save_config_pending,
                'save_config_pending_items': self.status_save_pending}


class StubConfigFile:
    """configfile.PrinterConfig - THE OBJECT KLIPPY ACTUALLY REGISTERS as
    'configfile' (klippy.py: objects['configfile'] = PrinterConfig(self)).

    Modelled faithfully on purpose: it owns ConfigAutoSave as `self.autosave`
    and only DELEGATES set()/get_status() to it. It deliberately has NO
    `fileconfig` attribute - an earlier draft of the guard read
    `configfile.fileconfig`, which silently AttributeError'd into a dead
    SAVE_CONFIG backstop. A stub that was itself a ConfigAutoSave hid that
    completely, which is exactly why this one is not.
    """

    def __init__(self):
        self.autosave = StubConfigAutoSave()

    @property
    def sets(self):
        return self.autosave.sets

    def set(self, section, option, value):
        self.autosave.set(section, option, value)

    def get_status(self, eventtime):
        status = {'config': {}, 'settings': {}}
        status.update(self.autosave.get_status(eventtime))
        return status


class StubReactor:
    def monotonic(self):
        return 0.0


class StubPrinter:
    command_error = StubError

    def __init__(self, gcode, configfile):
        self.objects = {'gcode': gcode, 'configfile': configfile}
        self.event_handlers = {}
        self.reactor = StubReactor()

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default='__raise__'):
        if name in self.objects:
            return self.objects[name]
        if default == '__raise__':
            raise StubError("no object %s" % name)
        return default

    def register_event_handler(self, evt, cb):
        self.event_handlers.setdefault(evt, []).append(cb)


class StubSection:
    def __init__(self, name, values):
        self.name = name
        self.values = values

    def get_name(self):
        return self.name

    def get(self, option, default=None, note_valid=True):
        return self.values.get(option, default)

    def getfloat(self, option, default=None, note_valid=True, **kw):
        v = self.values.get(option)
        return default if v is None else float(v)

    def getboolean(self, option, default=None, note_valid=True):
        v = self.values.get(option)
        return default if v is None else bool(v)


class StubConfig(StubSection):
    error = StubError

    def __init__(self, printer, guard_opts, z_values):
        StubSection.__init__(self, 'z_offset_guard', guard_opts)
        self.printer = printer
        self.z_values = z_values

    def get_printer(self):
        return self.printer

    def has_section(self, name):
        return name == 'stepper_z'

    def getsection(self, name):
        return StubSection(name, self.z_values)

    def get_prefix_sections(self, prefix):
        return []


class StubGCodeCommand:
    error = StubError

    def __init__(self):
        self.responses = []

    def respond_info(self, msg, log=True):
        self.responses.append(msg)


class StubHomingOrigin:
    def __init__(self, z):
        self.z = z


class StubGCodeMove:
    def __init__(self, offset):
        self.offset = offset

    def get_status(self, eventtime=None):
        return {'homing_origin': StubHomingOrigin(self.offset)}


class StubManualProbe:
    """Stands in for klippy/extras/manual_probe.py ManualProbe.

    cmd_Z_OFFSET_APPLY_ENDSTOP below is transcribed from the real one on this
    printer (rev 7046bd00) - same arithmetic, same "%.3f", same
    configfile.set() - so what the harness measures is the guard wrapping the
    REAL write, not a convenient approximation of it.
    """

    def __init__(self, printer, z_position_endstop):
        self.printer = printer
        self.gcode = printer.lookup_object('gcode')
        self.gcode_move = printer.lookup_object('gcode_move')
        self.z_position_endstop = z_position_endstop
        self.z_endstop_config_name = 'stepper_z'

    def cmd_Z_OFFSET_APPLY_ENDSTOP(self, gcmd):
        offset = self.gcode_move.get_status()['homing_origin'].z
        configfile = self.printer.lookup_object('configfile')
        if offset == 0:
            self.gcode.respond_info("Nothing to do: Z Offset is 0")
        else:
            new_calibrate = self.z_position_endstop - offset
            self.gcode.respond_info(
                "%s: position_endstop: %.3f" % (self.z_endstop_config_name,
                                                new_calibrate))
            configfile.set(self.z_endstop_config_name, 'position_endstop',
                           "%.3f" % (new_calibrate,))


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
def build(z_values, guard_opts=None, offset=0.0):
    import z_offset_guard
    gcode = StubGCode()
    cf = StubConfigFile()
    printer = StubPrinter(gcode, cf)
    printer.objects['gcode_move'] = StubGCodeMove(offset)
    config = StubConfig(printer, guard_opts or {}, z_values)
    guard = z_offset_guard.ZOffsetGuard(config)
    mp = StubManualProbe(printer, z_values.get('position_endstop'))
    printer.objects['manual_probe'] = mp
    # What klippy:connect does for real: the stock handler is registered first,
    # the guard then takes it over.
    gcode.register_command('Z_OFFSET_APPLY_ENDSTOP',
                           mp.cmd_Z_OFFSET_APPLY_ENDSTOP)
    gcode.register_command('SAVE_CONFIG', lambda gcmd: None)
    for cb in printer.event_handlers.get('klippy:connect', []):
        cb()
    return guard, gcode, cf, printer


def effective(cf, z_values, option):
    fc = cf.autosave.fileconfig
    if fc.has_option('stepper_z', option):
        return float(fc.get('stepper_z', option))
    return float(z_values[option])


FAIL = []
CASES = []


def check_run(label, z_values, offset, guard_opts=None,
              expect=None, also_save_config=True):
    """Run the wrapped command, then assert the resulting config would boot."""
    guard, gcode, cf, printer = build(z_values, guard_opts, offset)
    refused = None
    try:
        gcode.commands['Z_OFFSET_APPLY_ENDSTOP'](StubGCodeCommand())
    except StubError as e:
        refused = str(e)
    if refused is None and also_save_config:
        try:
            guard.cmd_SAVE_CONFIG(StubGCodeCommand())
        except StubError as e:
            refused = "SAVE_CONFIG: " + str(e)

    es = effective(cf, z_values, 'position_endstop')
    mn = effective(cf, z_values, 'position_min')
    mx = effective(cf, z_values, 'position_max')
    boot = klipper_boot_check(es, mn, mx)

    outcome = 'refused' if refused is not None else 'applied'
    status = 'OK'
    detail = ''
    if refused is not None:
        # A refusal must leave a config that still boots. The guard is allowed
        # to have let the stock command queue position_endstop before the
        # SAVE_CONFIG backstop caught it, but SAVE_CONFIG then never runs, so
        # nothing reaches disk - model that by checking the ON-DISK values.
        es_disk = float(z_values['position_endstop'])
        mn_disk = float(z_values['position_min'])
        boot_disk = klipper_boot_check(es_disk, mn_disk, mx)
        if boot_disk is not None:
            status = 'FAIL'
            detail = "refusal left an unbootable on-disk config: %s" % boot_disk
    else:
        if boot is not None:
            status = 'FAIL'
            detail = "applied config would NOT boot: %s" % boot
        elif mn > es + 1e-9:
            status = 'FAIL'
            detail = "position_min %.3f above endstop %.3f" % (mn, es)
    if expect is not None and outcome != expect:
        status = 'FAIL'
        detail = (detail + " | expected %s, got %s" % (expect, outcome)).strip()
    CASES.append((status, label, outcome,
                  "endstop=%.3f min=%.3f max=%.3f" % (es, mn, mx), detail,
                  refused))
    if status == 'FAIL':
        FAIL.append(label)


BASE = {'position_endstop': -1.0, 'position_min': -1.0, 'position_max': 190.0}


def main():
    # --- the two real incidents, replayed -------------------------------
    check_run("2026-08-16: endstop 0.5, min 0, babystep +0.5 -> 0.000",
              {'position_endstop': 0.5, 'position_min': 0.0,
               'position_max': 190.0}, 0.5, expect='applied')
    check_run("2026-08-17: endstop 0.000, min 0, babystep +1.0 -> -1.000"
              " (THE BRICK)",
              {'position_endstop': 0.0, 'position_min': 0.0,
               'position_max': 190.0}, 1.0, expect='applied')

    # --- current machine state ------------------------------------------
    check_run("today: endstop -1.0 == min -1.0, babystep +0.2",
              dict(BASE), 0.2, expect='applied')
    check_run("today: babystep -0.3 (endstop moves UP, min must follow)",
              dict(BASE), -0.3, expect='applied')
    check_run("today: offset 0 -> stock says nothing to do",
              dict(BASE), 0.0, expect='applied')

    # --- refusals --------------------------------------------------------
    check_run("absurd babystep +20 -> below the -5 floor",
              dict(BASE), 20.0, expect='refused')
    check_run("babystep +4.5 from -1.0 -> -5.5, just past the floor",
              dict(BASE), 4.5, expect='refused')
    check_run("babystep +3.9 from -1.0 -> -4.9, just inside the floor",
              dict(BASE), 3.9, expect='applied')
    check_run("babystep -200 -> endstop 199 above position_max 190",
              dict(BASE), -200.0, expect='refused')
    # Klipper WOULD boot on this one (min tracks the endstop, so the
    # homing_positive_dir inference still says False) - it is refused because
    # the axis reference must not jump 180mm, which would put Z=0 outside the
    # reachable range. This case is why max_auto_raise exists; the harness
    # found the hole before the printer did.
    check_run("babystep -180 -> endstop 179: boots, but Z=0 unreachable",
              dict(BASE), -180.0, expect='refused')
    check_run("babystep -140 from slack config: top quarter, min stays put"
              " -> homing direction would flip",
              {'position_endstop': 0.5, 'position_min': -2.0,
               'position_max': 190.0}, -160.0, expect='refused')
    check_run("babystep -4.0 -> endstop 3.0, inside the +5 ceiling",
              dict(BASE), -4.0, expect='applied')
    check_run("babystep -6.5 -> endstop 5.5, past the +5 ceiling",
              dict(BASE), -6.5, expect='refused')

    # --- Z_ENDSTOP_CALIBRATE-shaped values reaching SAVE_CONFIG ----------
    # The real -4.900 paper-test result from 2026-08-15, arriving with no
    # Z_OFFSET_APPLY_ENDSTOP involved at all: only the SAVE_CONFIG backstop
    # can catch this one.
    for val, expect in ((-4.9, 'adjusted'), (-6.0, 'refused'), (0.02, 'ok')):
        guard, gcode, cf, printer = build(dict(BASE))
        cf.set('stepper_z', 'position_endstop', "%.3f" % val)
        refused = None
        try:
            guard.cmd_SAVE_CONFIG(StubGCodeCommand())
        except StubError as e:
            refused = str(e)
        es = effective(cf, BASE, 'position_endstop')
        mn = effective(cf, BASE, 'position_min')
        boot = klipper_boot_check(es, mn, 190.0)
        got = 'refused' if refused else (
            'adjusted' if any(o == 'position_min' for _, o, _ in cf.sets)
            else 'ok')
        status = 'OK' if got == expect else 'FAIL'
        if refused is None and boot is not None:
            status = 'FAIL'
        detail = '' if status == 'OK' else "expected %s, got %s, boot=%s" % (
            expect, got, boot)
        CASES.append((status, "SAVE_CONFIG backstop, bare endstop %.3f" % val,
                      got, "endstop=%.3f min=%.3f" % (es, mn), detail,
                      refused))
        if status == 'FAIL':
            FAIL.append("backstop %.3f" % val)

    # --- keep_equal off ---------------------------------------------------
    check_run("keep_equal: False, babystep -0.3 (min NOT dragged up)",
              dict(BASE), -0.3, guard_opts={'keep_equal': False},
              expect='applied')

    # --- deliberate slack: min below endstop, guard must not close it ----
    check_run("deliberate slack min=-2 endstop=0.5, babystep +0.2 (no touch)",
              {'position_endstop': 0.5, 'position_min': -2.0,
               'position_max': 190.0}, 0.2, expect='applied')

    # --- property sweep ---------------------------------------------------
    bad = 0
    total = 0
    for old in (-4.0, -1.0, 0.0, 0.5, 2.0):
        for mn0 in (-5.0, -1.0, 0.0):
            if old < mn0:
                continue           # not a bootable starting point
            for off in (-3.0, -1.0, -0.5, -0.05, 0.0, 0.05, 0.5, 1.0, 3.0,
                        6.0, 12.0):
                total += 1
                zv = {'position_endstop': old, 'position_min': mn0,
                      'position_max': 190.0}
                guard, gcode, cf, printer = build(zv, None, off)
                refused = False
                try:
                    gcode.commands['Z_OFFSET_APPLY_ENDSTOP'](
                        StubGCodeCommand())
                    guard.cmd_SAVE_CONFIG(StubGCodeCommand())
                except StubError:
                    refused = True
                if refused:
                    es, mn = old, mn0          # nothing reaches disk
                else:
                    es = effective(cf, zv, 'position_endstop')
                    mn = effective(cf, zv, 'position_min')
                err = klipper_boot_check(es, mn, 190.0)
                if err is not None:
                    bad += 1
                    print("SWEEP FAIL old=%s min=%s off=%s -> es=%s mn=%s: %s"
                          % (old, mn0, off, es, mn, err))
    CASES.append(('OK' if bad == 0 else 'FAIL',
                  "property sweep: every outcome boots (%d combinations)"
                  % total, "%d unbootable" % bad, '', '', None))
    if bad:
        FAIL.append("sweep")

    # --- the backstop must be ALIVE, not silently dead -------------------
    # Regression test for the bug the PrinterConfig-shaped stub exposes: the
    # guard must read the pending state through the object klippy actually
    # registers, which has no .fileconfig of its own.
    guard, gcode, cf, printer = build(dict(BASE))
    alive = guard.pending_readable and guard._pending_items() is not None
    cf.set('stepper_z', 'position_endstop', "-2.500")
    seen = guard._pending('position_endstop')
    ok = alive and seen == -2.5
    CASES.append(('OK' if ok else 'FAIL',
                  "backstop reads pending state via PrinterConfig (no"
                  " .fileconfig attribute)",
                  'alive' if ok else 'DEAD',
                  "pending_readable=%s, _pending()=%s" % (
                      guard.pending_readable, seen), '', None))
    if not ok:
        FAIL.append("backstop liveness")

    # --- Z_OFFSET_GUARD_DISCARD unsticks a refused SAVE_CONFIG -----------
    guard, gcode, cf, printer = build(dict(BASE))
    cf.set('extruder', 'pid_kp', "28.957")          # unrelated, must survive
    cf.set('stepper_z', 'position_endstop', "-9.000")   # past the floor
    first = None
    try:
        guard.cmd_SAVE_CONFIG(StubGCodeCommand())
    except StubError as e:
        first = str(e)
    gcode.commands['Z_OFFSET_GUARD_DISCARD'](StubGCodeCommand())
    second = None
    try:
        guard.cmd_SAVE_CONFIG(StubGCodeCommand())
    except StubError as e:
        second = str(e)
    pid_kept = cf.autosave.fileconfig.get('extruder', 'pid_kp') == "28.957"
    es_after = effective(cf, BASE, 'position_endstop')
    ok = (first is not None and second is None and pid_kept
          and abs(es_after - (-1.0)) < 1e-9)
    CASES.append(('OK' if ok else 'FAIL',
                  "Z_OFFSET_GUARD_DISCARD unsticks SAVE_CONFIG, PID survives",
                  'unstuck' if ok else 'BROKEN',
                  "endstop back to %.3f, pid_kp kept=%s" % (es_after,
                                                            pid_kept),
                  '', None))
    if not ok:
        FAIL.append("discard")

    # --- report -----------------------------------------------------------
    width = max(len(c[1]) for c in CASES)
    for status, label, outcome, values, detail, refused in CASES:
        print("[%s] %-*s  %-9s %s" % (status, width, label, outcome, values))
        if detail:
            print("      -> %s" % detail)
        if refused:
            first = refused.strip().split('\n')[0]
            print("      refusal: %s" % first)
    print()
    if FAIL:
        print("FAILED: %d" % len(FAIL))
        for f in FAIL:
            print("  - %s" % f)
        return 1
    print("ALL %d CHECKS PASSED" % len(CASES))
    return 0


if __name__ == '__main__':
    sys.exit(main())

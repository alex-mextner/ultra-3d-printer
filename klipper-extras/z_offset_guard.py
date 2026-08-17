# z_offset_guard.py - stop a Z offset save from writing a config that Klipper
# will then REFUSE TO BOOT.
#
# THE FAILURE THIS EXISTS TO KILL (happened twice on this machine, 2026-08-16
# and 2026-08-17): the user baby-steps Z during a print and presses Save.
# KlipperScreen/Mainsail run Z_OFFSET_APPLY_ENDSTOP, whose arithmetic is
#   new_position_endstop = old_position_endstop - homing_origin.z
# (klippy/extras/manual_probe.py, cmd_Z_OFFSET_APPLY_ENDSTOP - read on this
# printer, rev 7046bd00, 2026-08-17). The result goes into the #*# SAVE_CONFIG
# block verbatim, with NO range check of any kind. SAVE_CONFIG then restarts
# Klipper, and klippy/stepper.py GenericPrinterRail.__init__ does check it:
#
#     if (self.position_endstop < self.position_min
#         or self.position_endstop > self.position_max):
#         raise config.error(
#             "position_endstop in section '%s' must be between"
#             " position_min and position_max" % config.get_name())
#
# so the machine is dead on the next boot, after a completely correct user
# action. Nothing in stock Klipper closes that gap: manual_probe.py writes the
# number, stepper.py rejects it, and the two never talk.
#
# WHAT THIS MODULE DOES. It intercepts the write side instead of the reject
# side, at two points (both registered on "klippy:connect", because
# toolhead.py loads manual_probe AFTER all config sections - see toolhead.py's
# `modules = [... "manual_probe" ...]` list - so at __init__ time the command
# does not exist yet):
#
#   1. Z_OFFSET_APPLY_ENDSTOP - wrapped. Predicts the exact value the stock
#      command is about to write (same inputs, same "%.3f" rounding), decides,
#      and either refuses BEFORE the stock command runs, or lets it run and
#      then writes a matching position_min in the SAME save.
#   2. SAVE_CONFIG - wrapped as a backstop. This is the ONLY choke point every
#      writer of position_endstop must pass through, so it also covers
#      Z_ENDSTOP_CALIBRATE (which is not a hypothetical: a real paper test on
#      this machine returned -4.900 on 2026-08-15; saving that against
#      position_min = -1.0 would have bricked the boot exactly the same way)
#      and any hand-typed SET_GCODE_OFFSET + save sequence.
#
# THE INVARIANT IT MAINTAINS: position_min == position_endstop.
#
# That equality is NOT a loosening of the axis limit - it is the same zero
# overtravel the machine had when both were 0. After homing, the axis sits at
# position_endstop; if position_min equals it, there is no reachable travel
# below the switch at all. position_min: -2 against position_endstop: -1 would
# be a real millimetre of travel INTO THE NOZZLE (on this machine Z drives the
# BED UP toward the nozzle), which is why the guard never opens slack - it only
# ever moves position_min to exactly the new position_endstop.
# Klipper's own check is inclusive (`<` and `>`, not `<=`/`>=`), so equality is
# a legal, deliberate configuration, and the homing_positive_dir inference in
# stepper.py stays correct for a MIN-homed axis: with endstop == min,
# `position_endstop <= position_min + axis_len/4` is true, so it infers
# homing_positive_dir = False, which is what a Z-MIN endstop needs.
#
# DIRECTIONS ARE NOT SYMMETRIC, and this is the whole safety argument:
#   * endstop moves DOWN (the common case - a positive baby-step means "more
#     gap", and new = old - offset): the pair goes invalid and the machine
#     bricks. Physically this direction is the SAFE one (Z=0 ends up further
#     from the switch, i.e. a larger first-layer gap). The guard lowers
#     position_min to match. Bounded by max_auto_lower, see below.
#   * endstop moves UP (a negative baby-step, "less gap"): the pair stays
#     valid, so Klipper boots - but position_min is now BELOW position_endstop,
#     which silently opens exactly the overtravel toward the nozzle described
#     above. The guard raises position_min to match, but only if the two were
#     already equal (see keep_equal) - it will not silently close slack that
#     somebody deliberately configured. Bounded by max_auto_raise: dragging
#     position_min up far enough puts Z=0 outside the axis range entirely.
#   * endstop moves ABOVE position_max: REFUSED, never auto-fixed. The only
#     "fix" available would be raising position_max, and that extends the
#     reachable travel of the axis into territory nobody has measured. On this
#     machine position_max was measured (MEASURE_HOME, 195.675mm real travel,
#     rounded down to 190 on purpose) - inventing more of it from a baby-step
#     is not a repair, it is a different and worse bug. This case also means
#     the endstop is above the physical ceiling of the axis, which no offset
#     save should ever be able to produce; it means something else is wrong.
#
# WHAT IT DOES NOT DO: Z_OFFSET_APPLY_PROBE / PROBE_CALIBRATE are deliberately
# NOT wrapped. They write z_offset into [probe]/[bltouch], and that option is
# read as a plain `config.getfloat('z_offset')` with no bounds anywhere in
# klippy/extras/probe.py (checked on this printer 2026-08-17) - there is no
# value it can hold that stops Klipper from booting, so there is nothing here
# to guard. The only range error involving it (`horizontal_move_z can't be less
# than probe's z_offset`, probe.py) is raised at command time, on a machine
# that is already running. This module must simply not fall over when no probe
# exists at all - which is this machine's current state, [bltouch] is commented
# out since the CR-Touch burned out - and it does not, because it never looks
# a probe object up.
#
# SELF-HEALING AN ALREADY-DEAD MACHINE IS OUT OF SCOPE BY DESIGN. Once the
# config fails validation Klipper never reaches the point where extras are
# loaded, so no .py in klippy/extras can possibly run. See this repo's
# docs/printer-status.md for the recovery procedure and scripts/deploy.sh,
# which recognises the state and prints the exact command.

import logging

# Read at load and never re-read: the fallback values for the Z rail's bounds,
# straight out of the same config object Klipper itself validated at startup.
# Deliberately note_valid=False - this module is an observer of those options,
# not their owner, and must not claim them in Klipper's "unknown option" check.


def _lookup_z_section(config):
    # Mirrors manual_probe.lookup_z_endstop_config(), reimplemented locally
    # instead of imported so this module keeps working if a future Klipper
    # moves or renames that helper - it is a two-branch lookup, not a
    # behaviour worth coupling to.
    if config.has_section('stepper_z'):
        return config.getsection('stepper_z')
    for cconfig in config.get_prefix_sections('carriage '):
        carriage_name = cconfig.get_name().split()[-1].strip()
        if cconfig.get('axis', carriage_name, note_valid=False) == 'z':
            return cconfig
    return None


class ZOffsetGuard:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.configfile = self.printer.lookup_object('configfile')

        zconfig = _lookup_z_section(config)
        if zconfig is None:
            raise config.error(
                "z_offset_guard: no [stepper_z] (and no [carriage] with"
                " axis: z) in the config - nothing to guard")
        self.z_section = zconfig.get_name()
        self.cfg_endstop = zconfig.getfloat('position_endstop', None,
                                            note_valid=False)
        self.cfg_min = zconfig.getfloat('position_min', 0., note_valid=False)
        self.cfg_max = zconfig.getfloat('position_max', None, note_valid=False)
        # homing_positive_dir: taken explicitly if set, otherwise inferred the
        # same way stepper.py does. A MAX-homed Z axis would need the mirror
        # image of everything below (track position_max, refuse to widen
        # position_min), which is a different machine and a different argument
        # - so the guard reports and refuses instead of guessing.
        self.homing_positive_dir = zconfig.getboolean('homing_positive_dir',
                                                      None, note_valid=False)

        # How far below zero the guard is willing to chase position_endstop by
        # widening position_min. DEFAULT 5.0mm, and the number is a SANITY
        # bound, not a safety bound - safety comes from min == endstop, which
        # leaves zero travel below the switch at any value. What 5.0 buys:
        #   * the two real incidents on this machine moved the endstop by 0.5
        #     and 1.0mm, so normal use is nowhere near it;
        #   * a genuinely misplaced endstop switch is a couple of mm out at
        #     most - past that the flag/bracket needs moving, not the config
        #     stretching, and that is precisely the message the refusal gives;
        #   * position_endstop is by construction the Z coordinate at which the
        #     switch fires, i.e. a small number near 0 on any correctly built
        #     Z-MIN machine, so anchoring the floor at 0 - not at wherever the
        #     endstop has already drifted to - is what makes this bound
        #     actually bound anything. A per-save delta limit would not: ten
        #     0.5mm saves would walk the axis reference 5mm down one legal step
        #     at a time. The floor is absolute: -max_auto_lower.
        self.max_auto_lower = config.getfloat('max_auto_lower', 5.,
                                              above=0.)
        # The mirror bound, and it is not decoration - the test harness for
        # this module found its absence as a real hole. With keep_equal on,
        # position_min follows the endstop UPWARD too, and that direction has
        # no natural stop: an endstop of 179 with position_min dragged to 179
        # passes every check stepper.py makes (Klipper boots, and it still
        # infers homing_positive_dir=False because the two are equal), while
        # leaving an axis whose reachable range is 179..190 - Z=0 is no longer
        # a legal coordinate and no print can start. Not dangerous, since
        # min == endstop still means zero travel below the switch, but silently
        # accepting a 180mm jump in the axis reference is not a guard doing its
        # job. Same anchor and same argument as max_auto_lower: position_endstop
        # belongs near zero on a Z-MIN machine.
        self.max_auto_raise = config.getfloat('max_auto_raise',
                                              self.max_auto_lower, above=0.)
        # Keep position_min glued to position_endstop when the endstop moves
        # UP as well as down. Only ever applied when the two are ALREADY equal:
        # a config with deliberate slack below the endstop is somebody's
        # decision, and silently closing it would be as rude as silently
        # opening it.
        self.keep_equal = config.getboolean('keep_equal', True)
        self.verbose = config.getboolean('verbose', True)

        self.prev_apply_endstop = None
        self.prev_save_config = None
        self.last_action = 'none'
        self.last_message = ''

        self.pending_readable = True
        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.gcode.register_command(
            'Z_OFFSET_GUARD_STATUS', self.cmd_Z_OFFSET_GUARD_STATUS,
            desc=self.cmd_Z_OFFSET_GUARD_STATUS_help)
        self.gcode.register_command(
            'Z_OFFSET_GUARD_DISCARD', self.cmd_Z_OFFSET_GUARD_DISCARD,
            desc=self.cmd_Z_OFFSET_GUARD_DISCARD_help)

    # ------------------------------------------------------------------
    # Command interception
    # ------------------------------------------------------------------
    def _handle_connect(self):
        # register_command(cmd, None) unregisters and RETURNS the previous
        # handler - the documented way to wrap a builtin, used by
        # safe_z_home.py (G28) and gcode_macro.py (rename_existing) in stock
        # Klipper. For a non-traditional (extended) command the returned
        # handler is the wrapper that runs _get_extended_params() on its
        # argument; calling it again with an already-extended gcmd is
        # harmless, _get_extended_params re-parses the same raw parameter
        # string in place and is idempotent (gcode.py, read 2026-08-17).
        prev = self.gcode.register_command('Z_OFFSET_APPLY_ENDSTOP', None)
        if prev is None:
            # Not an error: manual_probe only registers this command when the
            # Z section actually has a position_endstop. Nothing to guard,
            # and nothing that can brick the boot through this path.
            logging.info("z_offset_guard: Z_OFFSET_APPLY_ENDSTOP is not"
                         " registered - not wrapping it")
        else:
            self.prev_apply_endstop = prev
            self.gcode.register_command(
                'Z_OFFSET_APPLY_ENDSTOP', self.cmd_Z_OFFSET_APPLY_ENDSTOP,
                desc="Adjust the z endstop_position (guarded by"
                     " [z_offset_guard])")
        # SELF-CHECK BEFORE CLAIMING TO GUARD ANYTHING. A guard that fails
        # silently is worse than no guard: it removes the caution without
        # replacing it. If neither route to the pending autosave state works on
        # this Klipper, say so loudly instead of pretending.
        self.pending_readable = True
        try:
            if self._pending_items() is None and \
                    self._autosave_fileconfig() is None:
                raise RuntimeError("no readable pending-autosave state")
        except Exception:
            self.pending_readable = False
            logging.exception(
                "z_offset_guard: cannot read the pending SAVE_CONFIG state on"
                " this Klipper - the SAVE_CONFIG backstop is DISABLED")
            self.gcode.respond_info(
                "z_offset_guard: НЕ ВИЖУ ожидающее состояние SAVE_CONFIG на"
                " этой версии Klipper — бэкстоп на SAVE_CONFIG ОТКЛЮЧЁН"
                " (обёртка Z_OFFSET_APPLY_ENDSTOP работает). Смотри"
                " klippy.log; вероятно поменялся configfile.py.")

        prev_save = self.gcode.register_command('SAVE_CONFIG', None)
        if prev_save is None:
            logging.warning("z_offset_guard: SAVE_CONFIG is not registered -"
                            " backstop disabled")
        else:
            self.prev_save_config = prev_save
            self.gcode.register_command(
                'SAVE_CONFIG', self.cmd_SAVE_CONFIG,
                desc="Overwrite config file and restart (guarded by"
                     " [z_offset_guard])")

    # ------------------------------------------------------------------
    # Effective (post-save) view of the three numbers
    # ------------------------------------------------------------------
    def _pending_items(self):
        # PRIMARY, and deliberately the public route. The object registered as
        # 'configfile' is configfile.PrinterConfig (klippy.py:
        # `self.objects['configfile'] = pconfig = configfile.PrinterConfig(self)`)
        # - NOT ConfigAutoSave. PrinterConfig only DELEGATES set() and
        # remove_section() to `self.autosave`; it has no `fileconfig` attribute
        # of its own. Reaching for configfile.fileconfig therefore raises
        # AttributeError, and an earlier draft of this module did exactly that
        # inside a try/except - which would have made the SAVE_CONFIG backstop
        # a silent no-op with no symptom at all. get_status() is the one thing
        # guaranteed to exist here: it is what Moonraker itself reads for
        # save_config_pending / save_config_pending_items.
        eventtime = self.printer.get_reactor().monotonic()
        status = self.configfile.get_status(eventtime)
        return status.get('save_config_pending_items')

    def _autosave_fileconfig(self):
        # FALLBACK only. The RawConfigParser holding the whole autosave block,
        # reached through PrinterConfig.autosave (and tolerating a future
        # Klipper that flattens the two classes back together).
        obj = getattr(self.configfile, 'autosave', self.configfile)
        return getattr(obj, 'fileconfig', None)

    def _pending(self, option):
        # The value the next SAVE_CONFIG would write for this option, or None
        # if nothing new is queued for it. Anything NOT queued this session is
        # already on disk, and therefore already in the values read at
        # __init__ - the on-disk #*# block is merged into the regular config by
        # configfile.load_main_config()'s append_fileconfig(), so
        # config.getsection('stepper_z').getfloat(...) saw it.
        try:
            items = self._pending_items()
            if items is not None:
                section = items.get(self.z_section)
                if section:
                    value = section.get(option)
                    if value is not None:
                        return float(value)
                return None
        except Exception:
            logging.exception("z_offset_guard: save_config_pending_items"
                              " unreadable, falling back to the autosave"
                              " fileconfig")
        try:
            fc = self._autosave_fileconfig()
            if fc is not None and fc.has_option(self.z_section, option):
                return float(fc.get(self.z_section, option))
        except Exception:
            logging.exception("z_offset_guard: could not read pending %s",
                              option)
        return None

    def _effective(self, option, fallback):
        pending = self._pending(option)
        if pending is not None:
            return pending
        return fallback

    def _cur_endstop(self):
        return self._effective('position_endstop', self.cfg_endstop)

    def _cur_min(self):
        return self._effective('position_min', self.cfg_min)

    def _cur_max(self):
        return self._effective('position_max', self.cfg_max)

    # ------------------------------------------------------------------
    # The decision itself - one place, used by both wrappers
    # ------------------------------------------------------------------
    def _plan(self, new_endstop):
        # Returns (action, new_min, message).
        #   action 'ok'     - the pair is valid and no config surgery is needed
        #   action 'adjust' - write position_min = new_min alongside
        #   action 'refuse' - do not let this value reach the config at all
        cur_min = self._cur_min()
        cur_max = self._cur_max()
        cur_endstop = self._cur_endstop()

        if cur_max is None:
            return ('refuse', None,
                    "[%s] не объявляет position_max — проверить границы"
                    " нечем, отказываюсь трогать конфиг." % (self.z_section,))

        # A MAX-homed Z is the mirror image of everything this module assumes.
        positive_dir = self.homing_positive_dir
        if positive_dir is None and cur_endstop is not None:
            axis_len = cur_max - cur_min
            if axis_len > 0. and cur_endstop >= cur_max - axis_len / 4.:
                positive_dir = True
            else:
                positive_dir = False
        if positive_dir:
            return ('refuse', None,
                    "ось Z хоумится к МАКСИМУМУ (homing_positive_dir=True) —"
                    " автоправка position_min для такой оси неверна, а править"
                    " position_max автоматически опасно (это расширение хода)."
                    " Правь конфиг руками.")

        if new_endstop > cur_max:
            return ('refuse', None,
                    "новый position_endstop %.3f ВЫШЕ position_max %.3f."
                    " Это не смещение офсета, это концевик выше физического"
                    " потолка оси. Автоправка position_max была бы опасна —"
                    " она расширяет ход оси в непромеренную зону (position_max"
                    " на этой машине измерен, а не выдуман). Отказываюсь;"
                    " разбирайся с причиной такого офсета."
                    % (new_endstop, cur_max))

        need_lower = new_endstop < cur_min - 1e-9
        already_equal = (cur_endstop is not None
                         and abs(cur_min - cur_endstop) < 1e-9)
        need_raise = (self.keep_equal and already_equal
                      and new_endstop > cur_min + 1e-9)

        if not need_lower and not need_raise:
            refusal = self._check_direction_flip(new_endstop, cur_min, cur_max)
            if refusal is not None:
                return ('refuse', None, refusal)
            return ('ok', None, '')

        if need_lower and new_endstop < -self.max_auto_lower - 1e-9:
            return ('refuse', None,
                    "новый position_endstop %.3f ниже предела самоправки"
                    " (-%.3f, параметр max_auto_lower в [z_offset_guard])."
                    " Столько накопленной поправки означает, что концевик"
                    " стоит физически не там — двигай сам концевик, а не"
                    " растягивай position_min. Ничего не записано, машина"
                    " останется запускаемой."
                    % (new_endstop, self.max_auto_lower))

        if need_raise and new_endstop > self.max_auto_raise + 1e-9:
            return ('refuse', None,
                    "новый position_endstop %.3f выше предела самоправки"
                    " вверх (+%.3f, параметр max_auto_raise в"
                    " [z_offset_guard]). position_min пошёл бы следом, и"
                    " достижимый диапазон оси стал бы %.3f..%.3f — Z=0 в него"
                    " уже не попадает, печатать стало бы нечем. Столько"
                    " поправки означает физически смещённый концевик, а не"
                    " офсет. Ничего не записано."
                    % (new_endstop, self.max_auto_raise, new_endstop,
                       cur_max))

        # position_max must stay STRICTLY above position_min (stepper.py uses
        # config.getfloat('position_max', above=self.position_min)), so a new
        # min equal to max would be a different unbootable config.
        if new_endstop >= cur_max - 1e-9:
            return ('refuse', None,
                    "новый position_endstop %.3f совпадает с position_max"
                    " %.3f или выше — position_min не может стать равным"
                    " position_max (Klipper требует строгого max > min)."
                    % (new_endstop, cur_max))

        refusal = self._check_direction_flip(new_endstop, new_endstop, cur_max)
        if refusal is not None:
            return ('refuse', None, refusal)
        return ('adjust', new_endstop, '')

    def _check_direction_flip(self, new_endstop, result_min, cur_max):
        # stepper.py INFERS homing_positive_dir from where position_endstop
        # sits inside position_min..position_max whenever the option is not
        # spelled out, and it re-infers on every boot:
        #     if position_endstop <= position_min + axis_len/4:  False
        #     elif position_endstop >= position_max - axis_len/4: True
        #     else: config error "Unable to infer homing_positive_dir"
        # So a saved endstop value that lands in the top quarter of the range
        # silently flips G28 Z to home in +Z on the NEXT boot. On this machine
        # that means driving the BED AWAY from the switch instead of onto it -
        # a far worse outcome than a refused save, and one no offset write
        # should ever be allowed to cause implicitly. (With position_min
        # tracking the endstop this is unreachable by construction: equal
        # values always take the first branch. The check exists for the
        # 'nothing to adjust' path, where position_min stays put.)
        if self.homing_positive_dir is not None:
            return None
        axis_len = cur_max - result_min
        if axis_len <= 0.:
            return None
        if new_endstop >= cur_max - axis_len / 4.:
            return ("новый position_endstop %.3f попадает в верхнюю четверть"
                    " диапазона %.3f..%.3f — на следующей загрузке Klipper"
                    " ВЫВЕЛ БЫ homing_positive_dir=True и G28 Z поехал бы в"
                    " другую сторону (на этой машине — стол ПРОЧЬ от"
                    " концевика). homing_positive_dir в конфиге не задан явно,"
                    " так что подстраховаться нечем. Отказываюсь."
                    % (new_endstop, result_min, cur_max))
        return None

    def _apply_adjust(self, new_endstop, new_min, source):
        cur_min = self._cur_min()
        self.configfile.set(self.z_section, 'position_min',
                            "%.3f" % (new_min,))
        self.last_action = 'adjusted'
        direction = "опущен" if new_min < cur_min else "поднят"
        msg = ("z_offset_guard: position_min %s %.3f -> %.3f вслед за"
               " position_endstop %.3f (%s).\n"
               "Они держатся РАВНЫМИ намеренно: после хоуминга ось стоит"
               " ровно на границе, хода ниже концевика нет вообще — это та же"
               " нулевая перебежка, что была при min=0=endstop, а не"
               " расширение хода.\n"
               "Обе строки уйдут в блок SAVE_CONFIG. Klipper перезапустится и"
               " поднимется — без этой правки он бы отказался стартовать"
               " ('position_endstop in section %s must be between"
               " position_min and position_max').\n"
               "ПОСЛЕ рестарта: забери printer.cfg с принтера в репозиторий и"
               " закоммить, иначе следующий деплой затрёт калибровки."
               % (direction, cur_min, new_min, new_endstop, source,
                  self.z_section))
        self.last_message = msg
        if self.verbose:
            self.gcode.respond_info(msg)
        logging.info("z_offset_guard: set [%s] position_min = %.3f (endstop"
                     " %.3f, source %s)",
                     self.z_section, new_min, new_endstop, source)

    # ------------------------------------------------------------------
    # Z_OFFSET_APPLY_ENDSTOP wrapper
    # ------------------------------------------------------------------
    def cmd_Z_OFFSET_APPLY_ENDSTOP(self, gcmd):
        # Predict what the stock command is about to write. Same three inputs
        # it uses (manual_probe.py cmd_Z_OFFSET_APPLY_ENDSTOP): the endstop
        # value manual_probe cached AT CONFIG LOAD - deliberately not the
        # pending one, because that is the number the stock arithmetic uses -
        # the live gcode_move homing_origin.z, and the same "%.3f" rounding
        # that determines what actually lands in the file.
        new_endstop = None
        try:
            mp = self.printer.lookup_object('manual_probe', None)
            gcode_move = self.printer.lookup_object('gcode_move', None)
            if mp is not None and gcode_move is not None:
                old = getattr(mp, 'z_position_endstop', None)
                offset = gcode_move.get_status()['homing_origin'].z
                if old is not None and offset != 0:
                    new_endstop = float("%.3f" % (old - offset,))
        except Exception:
            logging.exception("z_offset_guard: could not predict the new"
                              " position_endstop - passing the command"
                              " through unguarded")
            new_endstop = None

        if new_endstop is None:
            # Either offset == 0 (the stock command will say "nothing to do")
            # or the prediction failed. Either way there is nothing to decide;
            # never block the command on this module's own uncertainty when
            # the SAVE_CONFIG backstop still has to approve the result.
            return self.prev_apply_endstop(gcmd)

        action, new_min, why = self._plan(new_endstop)
        if action == 'refuse':
            self.last_action = 'refused'
            self.last_message = why
            raise gcmd.error(
                "Z_OFFSET_APPLY_ENDSTOP отклонён: %s\n"
                "Ничего не сохранено, текущий конфиг не тронут." % (why,))

        # Let the stock command do its own arithmetic, its own respond_info
        # and its own configfile.set(position_endstop) - this module never
        # duplicates that write.
        self.prev_apply_endstop(gcmd)

        written = self._pending('position_endstop')
        if written is not None and abs(written - new_endstop) > 1e-9:
            # The prediction and the real write disagree. Re-plan on the real
            # number rather than trusting the prediction.
            logging.warning("z_offset_guard: predicted %.3f but"
                            " Z_OFFSET_APPLY_ENDSTOP wrote %.3f - re-planning",
                            new_endstop, written)
            new_endstop = written
            action, new_min, why = self._plan(new_endstop)
            if action == 'refuse':
                self.last_action = 'refused'
                self.last_message = why
                raise gcmd.error(
                    "Z_OFFSET_APPLY_ENDSTOP: значение уже записано в"
                    " ОЖИДАЮЩИЙ сейв, но оно не проходит проверку: %s\n"
                    "SAVE_CONFIG теперь будет отказывать, пока это значение"
                    " там лежит. Сбросить только его: Z_OFFSET_GUARD_DISCARD."
                    " Сбросить весь ожидающий блок: RESTART." % (why,))

        if action == 'adjust':
            self._apply_adjust(new_endstop, new_min, 'Z_OFFSET_APPLY_ENDSTOP')
        else:
            self.last_action = 'ok'
            self.last_message = ("position_endstop %.3f укладывается в"
                                 " %.3f..%.3f, правка не нужна"
                                 % (new_endstop, self._cur_min(),
                                    self._cur_max()))

    # ------------------------------------------------------------------
    # SAVE_CONFIG backstop
    # ------------------------------------------------------------------
    def cmd_SAVE_CONFIG(self, gcmd):
        # Last gate before the file is rewritten and Klipper restarts. Every
        # writer of position_endstop - Z_OFFSET_APPLY_ENDSTOP, whose own
        # wrapper above has usually already handled it, Z_ENDSTOP_CALIBRATE,
        # which has none, and anything a future Klipper adds - has to come
        # through here.
        try:
            self._check_before_save(gcmd)
        except self.printer.command_error:
            # A deliberate refusal (gcmd.error IS printer.command_error) must
            # propagate and stop the save.
            raise
        except Exception:
            # A bug in this module must not be able to block saving a real
            # calibration (PID values live in the same autosave block). Fail
            # OPEN, loudly: that is exactly the stock behaviour this module
            # replaces, i.e. no worse than not having it.
            logging.exception("z_offset_guard: SAVE_CONFIG pre-check failed"
                              " internally - falling through to the stock"
                              " SAVE_CONFIG")
            self.gcode.respond_info(
                "z_offset_guard: внутренняя ошибка проверки перед"
                " SAVE_CONFIG — сохраняю без защиты (см. klippy.log)."
                " Проверь [stepper_z] после рестарта.")
        return self.prev_save_config(gcmd)

    def _check_before_save(self, gcmd):
        pending_endstop = self._pending('position_endstop')
        if pending_endstop is None:
            return
        action, new_min, why = self._plan(pending_endstop)
        if action == 'refuse':
            self.last_action = 'refused'
            self.last_message = why
            raise gcmd.error(
                "SAVE_CONFIG остановлен [z_offset_guard]: %s\n"
                "Сохранение НЕ выполнено — файл не тронут, машина"
                " по-прежнему запускается.\n"
                "🔴 ЭТОТ ОТКАЗ ЛИПКИЙ: плохое значение остаётся в ОЖИДАЮЩЕМ"
                " сейве, и КАЖДЫЙ следующий SAVE_CONFIG будет отказывать тоже"
                " — включая сохранение чего-то полезного, что лежит в том же"
                " ожидающем блоке (например свежий результат PID_CALIBRATE).\n"
                "Два выхода:\n"
                "  Z_OFFSET_GUARD_DISCARD — выбросить ТОЛЬКО Z-офсет и"
                " сохранить остальное;\n"
                "  RESTART — выбросить ВЕСЬ ожидающий блок целиком (всё"
                " несохранённое пропадёт)." % (why,))
        if action == 'adjust':
            self._apply_adjust(pending_endstop, new_min, 'SAVE_CONFIG')

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    cmd_Z_OFFSET_GUARD_DISCARD_help = (
        "Drop only the pending Z endstop change so the rest of the pending"
        " SAVE_CONFIG block can still be saved")

    def cmd_Z_OFFSET_GUARD_DISCARD(self, gcmd):
        # Exists because a refusal is STICKY: the rejected position_endstop
        # stays queued, so every later SAVE_CONFIG hits the same wall, and the
        # only stock way out is RESTART - which throws away the WHOLE pending
        # block, including a PID_CALIBRATE result that has nothing to do with
        # Z. This puts the Z pair back to the values Klipper booted with, using
        # nothing but the public configfile.set(): the queued entries then
        # write the values already in effect, i.e. a no-op, and SAVE_CONFIG
        # stops refusing. Nothing else in the pending block is touched.
        if self.cfg_endstop is None:
            raise gcmd.error(
                "z_offset_guard: в конфиге нет position_endstop для %s —"
                " откатывать нечего." % (self.z_section,))
        pending_es = self._pending('position_endstop')
        pending_min = self._pending('position_min')
        if pending_es is None and pending_min is None:
            gcmd.respond_info(
                "z_offset_guard: в ожидающем сейве нет ни position_endstop,"
                " ни position_min — сбрасывать нечего.")
            return
        self.configfile.set(self.z_section, 'position_endstop',
                            "%.3f" % (self.cfg_endstop,))
        if pending_min is not None:
            self.configfile.set(self.z_section, 'position_min',
                                "%.3f" % (self.cfg_min,))
        self.last_action = 'discarded'
        self.last_message = ("откат к загрузочным значениям endstop=%.3f"
                             " min=%.3f" % (self.cfg_endstop, self.cfg_min))
        gcmd.respond_info(
            "z_offset_guard: ожидающий Z-офсет сброшен.\n"
            "  position_endstop: %s -> %.3f (как при загрузке)\n"
            "  position_min:     %s -> %.3f\n"
            "Остальное в ожидающем блоке (PID и т.п.) НЕ тронуто — теперь"
            " SAVE_CONFIG пройдёт. Сам Z-офсет при этом потерян: если он был"
            " нужен, надо разбираться с концевиком физически."
            % ("нет" if pending_es is None else "%.3f" % (pending_es,),
               self.cfg_endstop,
               "нет" if pending_min is None else "%.3f" % (pending_min,),
               self.cfg_min))

    cmd_Z_OFFSET_GUARD_STATUS_help = (
        "Report the Z endstop/min/max the guard sees and what it would do")

    def cmd_Z_OFFSET_GUARD_STATUS(self, gcmd):
        cur_endstop = self._cur_endstop()
        gcmd.respond_info(
            "z_offset_guard: секция %s\n"
            "  position_endstop = %s (в конфиге при загрузке: %s)\n"
            "  position_min     = %.3f\n"
            "  position_max     = %s\n"
            "  предел самоправки: концевик держится в %.3f..%.3f"
            " (max_auto_lower = %.3f, max_auto_raise = %.3f)\n"
            "  keep_equal = %s (следить за концевиком и вверх, не только вниз)\n"
            "  бэкстоп на SAVE_CONFIG: %s\n"
            "  последнее действие: %s"
            % (self.z_section,
               "нет" if cur_endstop is None else "%.3f" % (cur_endstop,),
               "нет" if self.cfg_endstop is None
               else "%.3f" % (self.cfg_endstop,),
               self._cur_min(),
               "нет" if self._cur_max() is None
               else "%.3f" % (self._cur_max(),),
               -self.max_auto_lower, self.max_auto_raise,
               self.max_auto_lower, self.max_auto_raise,
               self.keep_equal,
               "работает" if self.pending_readable else "ОТКЛЮЧЁН (см."
               " klippy.log)",
               self.last_action))

    def get_status(self, eventtime):
        return {
            'section': self.z_section,
            'position_endstop': self._cur_endstop(),
            'position_min': self._cur_min(),
            'position_max': self._cur_max(),
            'max_auto_lower': self.max_auto_lower,
            'max_auto_raise': self.max_auto_raise,
            'auto_lower_floor': -self.max_auto_lower,
            'auto_raise_ceiling': self.max_auto_raise,
            'keep_equal': self.keep_equal,
            'save_config_backstop': self.pending_readable,
            'last_action': self.last_action,
            'last_message': self.last_message,
        }


def load_config(config):
    return ZOffsetGuard(config)

#!/usr/bin/env python3
"""Klipper config linter — runs on the PRINTER's own Python (klippy-env),
using Klipper's REAL parsing pipeline (configfile.ConfigFileReader +
jinja2.Environment('{%','%}','{','}')), not a reimplementation of it.

Why it has to run on the printer and not be a "clean" standalone parser:
Klipper's configfile.py strips every config line at the first literal '#'
character BEFORE either configparser or Jinja ever see the text — quote-blind,
so a '#' inside a Jinja string literal inside a gcode: block truncates the
line and can silently mangle a {% if %}/{% endif %} pair. This exact bug shipped
once (power-loss-recovery.cfg, '#' inside `'#' in fname`, fixed by switching to
'\\x23') and a hand-rolled "looks like Klipper's parser" reimplementation gave a
false negative on it during an earlier verification attempt. So this script
imports the actual klippy/configfile.py and the actual jinja2 version Klipper
uses (via klippy-env) instead of guessing at either.

Checks:
  1. JINJA   — every [gcode_macro X]/[delayed_gcode X] section's gcode: value,
               in every printer-configs/*.cfg file, compiles as a Jinja
               template under Klipper's own Environment. Catches the class of
               bug described above.
  2. INCLUDE — every [include X] in the entry file (default printer.cfg),
               resolved recursively exactly the way Klipper's own
               _resolve_include() does it (glob relative to the including
               file's directory). Catches tonight's actual incident: printer.cfg
               referencing an include file that was never deployed/created.
  3. DUPLICATE — no section name (e.g. "gcode_macro NAME") appears more than
               once across the files actually reachable from the entry file's
               include tree. configparser.RawConfigParser(strict=False), which
               Klipper uses on purpose, MERGES same-named sections silently —
               a later file's option values silently win over an earlier
               file's, with no error. This walks the same include tree and
               records section headers before they get merged away.
  4. PIN     — no *_pin:/pin: value (after stripping !/^/~ modifiers) is
               claimed by more than one section, across all *.cfg files
               (whether or not they're currently included) — simple exact
               string match, no alias resolution.

Usage:
    <klippy-env-python> lint_configs.py <config_dir> [--klippy-dir DIR] [--root FILE]

Exit code 0 = clean, 1 = at least one real problem found, 2 = the linter
itself couldn't run (missing klippy/jinja2, bad args, etc — never treated as
"clean", see the house rule in deploy.sh: unknown state is a refusal, not a
pass).
"""
import argparse
import os
import re
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config_dir", help="directory holding the *.cfg files (printer-configs/, copied here)")
    ap.add_argument("--klippy-dir", default=os.path.expanduser("~/klipper/klippy"),
                     help="directory containing Klipper's configfile.py (default: ~/klipper/klippy)")
    ap.add_argument("--root", default="printer.cfg",
                     help="entry file for the include-tree checks (2, 3) — default printer.cfg")
    args = ap.parse_args()

    if not os.path.isdir(args.config_dir):
        print("LINTER ERROR: config dir not found: %s" % args.config_dir, file=sys.stderr)
        return 2

    sys.path.insert(0, args.klippy_dir)
    try:
        import configfile
    except ImportError as e:
        print("LINTER ERROR: couldn't import Klipper's configfile.py from %s: %s" % (args.klippy_dir, e), file=sys.stderr)
        return 2
    try:
        import jinja2
    except ImportError as e:
        print("LINTER ERROR: couldn't import jinja2 (need klippy-env's python3, not system python3): %s" % e, file=sys.stderr)
        return 2

    import configparser  # stdlib, fine to import directly for SECTCRE

    problems = []
    cfg_dir = args.config_dir
    all_cfg_files = sorted(f for f in os.listdir(cfg_dir) if f.endswith(".cfg"))

    # ---- Checks 2 + 3: walk the REAL include tree from the entry file -----
    # Recorded as (section_header, source_filename) in the order Klipper's own
    # _parse_config()/append_fileconfig() would process them. Subclassing
    # (rather than reimplementing the walk) means the comment-stripping,
    # include-glob-resolution and section-header regex are all Klipper's own
    # code, not a guess at it — only the recording is new.
    section_origins = []

    class RecordingReader(configfile.ConfigFileReader):
        def append_fileconfig(self, fileconfig, data, filename):
            if data:
                for line in data.split("\n"):
                    pos = line.find("#")
                    if pos >= 0:
                        line = line[:pos]
                    mo = configparser.RawConfigParser.SECTCRE.match(line)
                    if mo:
                        section_origins.append((mo.group("header"), os.path.basename(filename)))
            super().append_fileconfig(fileconfig, data, filename)

    root_path = os.path.join(cfg_dir, args.root)
    if not os.path.isfile(root_path):
        problems.append("[INCLUDE] entry file not found: %s" % args.root)
    else:
        reader = RecordingReader()
        try:
            data = reader.read_config_file(root_path)
            reader.build_fileconfig_with_includes(data, root_path)
        except configfile.error as e:
            # This is EXACTLY the exception _resolve_include() raises for a
            # missing include file — tonight's actual incident, reproduced.
            problems.append("[INCLUDE] %s" % e)

    if section_origins:
        by_name = {}
        for name, fn in section_origins:
            by_name.setdefault(name, []).append(fn)
        for name, files in by_name.items():
            if len(files) > 1:
                problems.append(
                    "[DUPLICATE] section [%s] defined %d times across: %s "
                    "(configparser strict=False merges these silently — last one wins)"
                    % (name, len(files), ", ".join(files))
                )

    # ---- Check 1: Jinja-compile every gcode: value, file by file ----------
    # Standalone per-file parse (build_fileconfig, no include-following): a
    # syntax error is a property of the raw text wherever it's written, and
    # this also naturally covers files not currently reachable from printer.cfg
    # (e.g. autotune_tmc.cfg while its [include] stays commented out).
    env = jinja2.Environment("{%", "%}", "{", "}")
    for fn in all_cfg_files:
        path = os.path.join(cfg_dir, fn)
        reader = configfile.ConfigFileReader()
        try:
            data = reader.read_config_file(path)
            fc = reader.build_fileconfig(data, path)
        except configfile.error as e:
            problems.append("[PARSE] %s: %s" % (fn, e))
            continue
        for section in fc.sections():
            if not (section.startswith("gcode_macro ") or section.startswith("delayed_gcode ")):
                continue
            if not fc.has_option(section, "gcode"):
                continue
            script = fc.get(section, "gcode")
            try:
                env.from_string(script)
            except jinja2.exceptions.TemplateSyntaxError as e:
                lines = script.splitlines()
                bad_line = lines[e.lineno - 1].strip() if lines and 0 < e.lineno <= len(lines) else "?"
                problems.append(
                    "[JINJA] %s [%s]: line %s of the gcode: block: %s  # %s"
                    % (fn, section, e.lineno, bad_line, e.message)
                )

    # ---- Check 4: pin collisions, across all top-level *.cfg files --------
    # Exact string match on the pin identifier after stripping !/^/~
    # modifiers — deliberately no alias/board-pin-alias resolution.
    # NOTE: the field-name part is intentionally NOT "[A-Za-z_]*pin" — that
    # requires at least one character before "pin" (needs 4+ total chars to
    # satisfy the regex engine's own backtracking), so it silently can never
    # match the bare field "pin:" itself, only "*_pin:" variants. Caught by
    # this linter's own self-test (t4: colliding two "pin:" fields produced
    # a false "LINT OK"). Match any field name, filter by .endswith() in
    # Python instead.
    field_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$")
    pin_claims = {}
    for fn in all_cfg_files:
        path = os.path.join(cfg_dir, fn)
        with open(path, "r") as f:
            raw = f.read().replace("\r\n", "\n")
        current_section = None
        for line in raw.split("\n"):
            pos = line.find("#")
            stripped = line[:pos] if pos >= 0 else line
            mo = configparser.RawConfigParser.SECTCRE.match(stripped)
            if mo:
                current_section = mo.group("header")
                continue
            m2 = field_re.match(stripped)
            if m2 and current_section and m2.group(1).lower().endswith("pin"):
                field, value = m2.group(1), m2.group(2)
                cleaned = value.strip().lstrip("!^~").strip()
                cleaned = cleaned.split()[0] if cleaned.split() else cleaned
                if not cleaned:
                    continue
                pin_claims.setdefault(cleaned, []).append((fn, current_section, field))
    for pin, claims in pin_claims.items():
        distinct_sections = {(f, s) for f, s, _ in claims}
        if len(distinct_sections) > 1:
            desc = ", ".join("%s [%s].%s" % (f, s, fld) for f, s, fld in claims)
            problems.append("[PIN] %s claimed by more than one section: %s" % (pin, desc))

    if problems:
        print("LINT FAILED: %d issue%s found" % (len(problems), "" if len(problems) == 1 else "s"))
        for p in problems:
            print("  - %s" % p)
        return 1

    print(
        "LINT OK: %d cfg file(s) checked, %d section(s) in the %s include tree, no problems found."
        % (len(all_cfg_files), len(section_origins), args.root)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

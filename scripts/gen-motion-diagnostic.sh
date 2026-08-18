#!/usr/bin/env bash
# Generate a motion-only diagnostic gcode file and upload it to the printer.
#
# WHY THIS EXISTS, and why it is not another TUNE_* ladder:
# the ladders in printer-configs/motor-tuning.cfg move ONE axis at a time with no
# extrusion, one long stroke per rung. On 2026-08-17 that blind spot bit us - the
# ladders passed Y clean to 300mm/s and 9000mm/s^2, and then a real print shifted
# layers on Y at LOWER numbers (144mm/s print / 200mm/s travel, accel <=7000).
# These patterns exist to close the gap between "one axis on a bench" and "a real
# print", one variable at a time. Results live in docs/motor-tuning-results.md.
#
# Both modes measure lost steps the same way: MEASURE_HOME (from the
# axis_travel_report extras module) anchors the axis at a synthetic position and
# reports the REAL physical travel to the endstop. The difference against the
# baseline measurement is the signed lost-step figure in mm. Full step on X/Y here
# is 0.180mm and endstop repeatability is ~0.02mm, so anything under ~0.05mm is
# noise, not loss.
#
#   combo    - both axes together, reversal every 42mm, no heat, no extrusion.
#              Isolates "simultaneous X+Y motion" from everything else.
#   circles  - continuous direction change instead of discrete corners, WITH the
#              bed and hotend at temperature. The hotend cartridge sits on the same
#              12V rail as the steppers and pulls ~3-4A in bursts, which `combo`
#              never exercised. Measures every few revolutions rather than once per
#              phase, so a loss is localised IN TIME - without that, a loss in one
#              direction can be partly cancelled by a loss in the other and hide.
#              Pull the filament out first: this holds 255C for minutes without
#              extruding.
#
# Usage: bash scripts/gen-motion-diagnostic.sh <combo|circles> [outfile]
# Then start it like any print (it is a normal gcode file, it does NOT call
# PRINT_START, so no RGB/park/cleanup macros run).
#
# SAFETY: both patterns begin with G28, so the bed must be CLEAR. Z homing on this
# machine raises the bed toward the nozzle and stops on the switch, not on an
# obstacle - a part left on the bed gets driven into the nozzle.
set -euo pipefail

MODE="${1:-}"
OUT="${2:-}"
# NOT named PRINTER: Windows already exports PRINTER as the default paper
# printer ("Pantum-E75CB3 (P2500W series)"), which scp then took for a hostname.
PRINTER_SSH="${PRINTER_SSH:-ultra@192.168.11.160}"

case "$MODE" in
    combo)    : ;;
    circles)  : ;;
    hotcombo) : ;;
    *) echo "usage: bash scripts/gen-motion-diagnostic.sh <combo|circles|hotcombo> [outfile]" >&2
       exit 2 ;;
esac
[ -n "$OUT" ] || OUT="y${MODE}.gcode"

# Bed is 245x190; every coordinate below stays well inside that with margin.
CX=122; CY=95

if [ "$MODE" = combo ]; then
    {
        echo "; COMBINED-LOAD X+Y diagnostic - no extrusion, no heat"
        echo "G90"; echo "G28"; echo "G1 Z10 F600"
        echo "SET_VELOCITY_LIMIT ACCEL=3000"
        echo "G1 X100 Y120 F12000"
        echo "M117 baseline"; echo "MEASURE_HOME AXIS=Y"; echo "G1 X100 Y120 F12000"
        echo "M117 phase1 143mms a3000"
        for _ in $(seq 1 250); do echo "G1 X130 Y150 F8600"; echo "G1 X100 Y120 F8600"; done
        echo "M117 after phase1"; echo "MEASURE_HOME AXIS=Y"; echo "G1 X100 Y120 F12000"
        echo "SET_VELOCITY_LIMIT ACCEL=7000"
        echo "M117 phase2 200mms a7000"
        for _ in $(seq 1 250); do echo "G1 X130 Y150 F12000"; echo "G1 X100 Y120 F12000"; done
        echo "M117 after phase2"; echo "MEASURE_HOME AXIS=Y"
        echo "SET_VELOCITY_LIMIT ACCEL=5000"; echo "M117 done"
    } > "$OUT"
elif [ "$MODE" = hotcombo ]; then
    # THE CELL THE OTHER TWO MODES LEAVE EMPTY - the user spotted it, 2026-08-18.
    # `combo` reverses hard (a standing reversal genuinely demands the configured
    # accel) but runs cold. `circles` runs hot, but a constant-speed circle only
    # ever demands v^2/R - at the radii used that peaks near 2000mm/s^2, so
    # setting ACCEL=7000 there is a ceiling that is never reached. Neither test
    # therefore applies high acceleration AND heat at once, which is exactly what
    # a real print does. This mode does.
    awk 'BEGIN{
      print "; HOT REVERSALS - high acceleration WITH bed and hotend at temperature"
      print "G90"; print "M140 S80"; print "M104 S255"; print "G28"; print "G1 Z10 F600"
      print "M190 S80"; print "M109 S255"
      print "M83"; print "G1 E-5 F300"       # keep the melt from drooling, no extrusion here
      print "G1 X100 Y120 F12000"
      print "M117 M00 baseline"; print "MEASURE_HOME AXIS=Y"; print "G1 X100 Y120 F12000"
      m=0
      # A: print-speed reversals at the deployed accel. B: travel-speed reversals
      # at the 7000 the slicer actually commands on this machine.
      split("A B", ph, " ")
      for(p=1;p<=2;p++){
        if(p==1){feed=8600;  acc=3000}
        if(p==2){feed=12000; acc=7000}
        printf "SET_VELOCITY_LIMIT ACCEL=%d\n", acc
        for(blk=1;blk<=8;blk++){
          printf "M117 %s%d f%d a%d\n", ph[p], blk, feed, acc
          for(c=0;c<30;c++){
            printf "G1 X130 Y150 F%d\n", feed
            printf "G1 X100 Y120 F%d\n", feed }
          m++; printf "M117 M%02d after %s%d\n", m, ph[p], blk
          print "MEASURE_HOME AXIS=Y"; print "G1 X100 Y120 F12000"
        }
      }
      print "SET_VELOCITY_LIMIT ACCEL=5000"; print "G1 E5 F300"
      print "M104 S0"; print "M140 S0"; print "M117 done"
    }' > "$OUT"
else
    awk -v cx="$CX" -v cy="$CY" 'BEGIN{
      PI=3.14159265358979
      print "; CIRCLES UNDER HEAT - continuous direction change, bed 80C + hotend 255C"
      print "G90"; print "M140 S80"; print "M104 S255"; print "G28"; print "G1 Z10 F600"
      print "M190 S80"; print "M109 S255"
      print "SET_VELOCITY_LIMIT ACCEL=3000"
      print "G1 X" cx " Y" cy " F12000"
      print "M117 M00 baseline"; print "MEASURE_HOME AXIS=Y"; print "G1 X" cx " Y" cy " F12000"
      m=0
      # A: R50 @143mm/s a3000 - print-speed arcs.   B: R10, same speed, junction
      # frequency 5x higher.   C: R50 @200mm/s a7000 - what Orca commands on travel.
      split("A B C", ph, " ")
      for(p=1;p<=3;p++){
        if(p==1){r=50; n=100; feed=8600;  revs=5;  acc=3000}
        if(p==2){r=10; n=40;  feed=8600;  revs=25; acc=3000}
        if(p==3){r=50; n=100; feed=12000; revs=5;  acc=7000}
        printf "SET_VELOCITY_LIMIT ACCEL=%d\n", acc
        for(blk=1;blk<=8;blk++){
          printf "M117 %s%d r%d f%d\n", ph[p], blk, r, feed
          printf "G1 X%.3f Y%.3f F12000\n", cx+r, cy
          for(rev=0;rev<revs;rev++) for(i=1;i<=n;i++){
            a=2*PI*i/n; printf "G1 X%.3f Y%.3f F%d\n", cx+r*cos(a), cy+r*sin(a), feed}
          print "G1 X" cx " Y" cy " F12000"
          m++; printf "M117 M%02d after %s%d\n", m, ph[p], blk
          print "MEASURE_HOME AXIS=Y"; print "G1 X" cx " Y" cy " F12000"
        }
      }
      print "SET_VELOCITY_LIMIT ACCEL=5000"; print "M104 S0"; print "M140 S0"; print "M117 done"
    }' > "$OUT"
fi

echo "wrote $OUT ($(wc -l < "$OUT") lines, $(grep -c MEASURE_HOME "$OUT") measurements)"
scp "$OUT" "$PRINTER_SSH:~/printer_data/gcodes/$(basename "$OUT")"
echo "uploaded to $PRINTER_SSH:~/printer_data/gcodes/$(basename "$OUT")"
echo "start it with: curl -X POST http://192.168.11.160:7125/printer/print/start \\"
echo "                 -H 'Content-Type: application/json' -d '{\"filename\":\"$(basename "$OUT")\"}'"

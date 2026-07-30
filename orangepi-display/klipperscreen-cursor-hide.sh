#!/bin/sh
# Прячет курсор мыши на корневом окне X - без оконного менеджера GTK'шный
# show_cursor:false этого не делает (только на окне самого KlipperScreen).
# Мышки физически нет и не будет (только 5 кнопок через gpio-keys), так что
# прячем раз и навсегда через unclutter-xfixes с idle=0.
set -eu

for i in $(seq 1 40); do
    [ -S /tmp/.X11-unix/X0 ] && break
    sleep 0.25
done

su - ultra -c "DISPLAY=:0 unclutter-xfixes -idle 0" >/tmp/cursor-hide.log 2>&1 &

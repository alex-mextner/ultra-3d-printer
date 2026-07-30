#!/usr/bin/env python3
# Перехватывает физические кнопки (nav_buttons: up/down/left/right/center) и
# переизлучает их через виртуальное uinput-устройство. left коротким нажатием
# работает как обычная стрелка влево, а долгим удержанием (>= LONG_PRESS_S)
# шлёт Backspace - у KlipperScreen это "назад" (screen.py:_key_press_event ->
# base_panel.back()), который, в отличие от Escape/_menu_go_back(home=True),
# спрашивает panel.back() у текущей панели и поэтому корректно закрывает
# оверлеи вроде numpad на main_menu, а не только всплывающие панели.
# Требует companion-патч screen.py (см. klipperscreen-patches/), снимающий
# guard `len(_cur_panels) > 1` с ветки BackSpace в _key_press_event - без
# него BackSpace на глубине 1 (main_menu + открытый numpad) не долетал бы
# до base_panel.back() вообще (issue #3).
import time

from evdev import InputDevice, UInput, ecodes as e

SRC_PATH = "/dev/input/by-path/platform-nav_buttons-event"
LONG_PRESS_S = 0.6

CAPS = {
    e.EV_KEY: [e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT, e.KEY_ENTER, e.KEY_BACKSPACE],
}


def emit_tap(ui, code):
    ui.write(e.EV_KEY, code, 1)
    ui.syn()
    ui.write(e.EV_KEY, code, 0)
    ui.syn()


def main():
    dev = InputDevice(SRC_PATH)
    dev.grab()

    ui = UInput(CAPS, name="nav_buttons_remap")

    left_press_time = None

    try:
        for event in dev.read_loop():
            if event.type != e.EV_KEY or event.value == 2:
                continue  # игнорируем autorepeat (value 2), нас интересуют только press/release

            if event.code == e.KEY_LEFT:
                if event.value == 1:
                    left_press_time = time.monotonic()
                elif event.value == 0 and left_press_time is not None:
                    held = time.monotonic() - left_press_time
                    left_press_time = None
                    emit_tap(ui, e.KEY_BACKSPACE if held >= LONG_PRESS_S else e.KEY_LEFT)
            else:
                ui.write(e.EV_KEY, event.code, event.value)
                ui.syn()
    finally:
        dev.ungrab()
        ui.close()


if __name__ == "__main__":
    main()

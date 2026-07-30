#!/usr/bin/env python3
# Перехватывает физические кнопки (nav_buttons: up/down/left/right/center) и
# переизлучает их через виртуальное uinput-устройство. left коротким нажатием
# работает как обычная стрелка влево, а долгим удержанием (>= LONG_PRESS_S)
# шлёт Escape - у KlipperScreen это "назад/домой" (screen.py:_key_press_event),
# а у 5-кнопочной раскладки без Escape/Backspace вообще нет способа выйти
# из панелей вроде notifications, если их не назначить явно.
import time

from evdev import InputDevice, UInput, ecodes as e

SRC_PATH = "/dev/input/by-path/platform-nav_buttons-event"
LONG_PRESS_S = 0.6

CAPS = {
    e.EV_KEY: [e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT, e.KEY_ENTER, e.KEY_ESC],
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
                    emit_tap(ui, e.KEY_ESC if held >= LONG_PRESS_S else e.KEY_LEFT)
            else:
                ui.write(e.EV_KEY, event.code, event.value)
                ui.syn()
    finally:
        dev.ungrab()
        ui.close()


if __name__ == "__main__":
    main()

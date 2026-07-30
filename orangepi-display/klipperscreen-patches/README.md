# Патчи поверх чекаута KlipperScreen

Это патчи прямо к исходникам `/home/ultra/KlipperScreen` (git-чекаут KlipperScreen
на самом принтере), НЕ к `printer-configs/KlipperScreen.conf`. Слетают при
`git pull`/самообновлении KlipperScreen - после обновления нужно переприменить
руками (`git apply`) и проверить, что верхний контекст патча (номера строк,
окружающий код) не разъехался с новой версией апстрима.

## keypad-width-overflow.patch

Экран 320x240 (после поворота на 90°), у цифровой клавиатуры (открывается по
тапу на температуру) есть колонка под `Gtk.Entry` + сетку кнопок 0-9. Без
явного `width_chars` GTK запрашивает под Entry "естественную" ширину ~165px
(больше, чем весь бюджет колонки ~150-160px), из-за чего сетка кнопок
уезжает за правый край экрана. Патч просто ограничивает `Entry` числом
символов, которое поле реально может вместить (`entry_max`).

Применить на принтере:

```
cd /home/ultra/KlipperScreen
git apply --check /path/to/keypad-width-overflow.patch   # сухой прогон
git apply /path/to/keypad-width-overflow.patch
sudo systemctl restart KlipperScreen
```

Живьём проверено (2026-07-30): было применено вручную на принтере, диф
снят с уже живой правки (`ks_includes/widgets/keypad.py`, `.orig-backup`
рядом на принтере - оригинал до патча, на случай отката).

#!/usr/bin/env python3
"""Вытаскивает контуры панелей корпуса из сборочного мануала MBot3D в SVG.

Страница 3 PDF (печатная стр. 1, «Parts Recognition I») — это векторная
иллюстрация из Illustrator, а не скан. Значит контуры всех шести фанерных
панелей можно достать НЕ картинкой, а вектором, пригодным для обводки
в Inkscape/FreeCAD.

Зачем это вообще: готового раскроя рамы в паблике нет (см. docs/bom.md,
раздел «Про open hardware»). Эти контуры — самое близкое к раскрою, что
существует. Размеров на них нет, поэтому сами по себе они не чертёж —
но как подложка под обводку по замерам экономят основную работу.

⚠️ Масштаб на чертеже не указан и между ячейками НЕ одинаков — калибровать
каждую панель отдельно, см. hardware/case/README.md.

Требует PyMuPDF:  pip install pymupdf

Запуск (из корня репозитория):
    python3 scripts/extract-panel-outlines.py
"""

import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("Нужен PyMuPDF:  pip install pymupdf")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "vendor" / "mbot3d-3DPKit_HowToAssembly_v1.pdf"
DEST = ROOT / "hardware" / "case"
PAGE_INDEX = 2  # 0-based: PDF-страница 3 = печатная страница 1
PREVIEW_DPI = 200


def main() -> int:
    if not SRC.exists():
        sys.exit(f"Нет исходника: {SRC}\nСначала: bash scripts/fetch-vendor-docs.sh")

    doc = fitz.open(SRC)
    if len(doc) <= PAGE_INDEX:
        sys.exit(f"В PDF всего {len(doc)} страниц, ожидали минимум {PAGE_INDEX + 1}")

    page = doc[PAGE_INDEX]

    # Страховка от того, что в источнике поменяли вёрстку: на нужной странице
    # обязаны быть подписи всех шести панелей. Молча выдать не тот лист хуже,
    # чем упасть.
    text = page.get_text()
    missing = [n for n in ("Front Panel", "Back Panel", "Top Panel",
                           "Bottom Panel", "Left Panel", "Right Panel")
               if n not in text]
    if missing:
        sys.exit(f"Страница {PAGE_INDEX + 1} не похожа на лист с панелями, "
                 f"не нашлись подписи: {', '.join(missing)}. "
                 f"Мануал обновился — проверить PAGE_INDEX.")

    DEST.mkdir(parents=True, exist_ok=True)

    svg_path = DEST / "panel-outlines-mbot3d-kit.svg"
    svg_path.write_text(page.get_svg_image(text_as_path=False), encoding="utf-8")
    print(f"✔ {svg_path.relative_to(ROOT)} ({svg_path.stat().st_size // 1024} КБ)")

    png_path = DEST / "panel-outlines-mbot3d-kit.png"
    page.get_pixmap(dpi=PREVIEW_DPI).save(png_path)
    print(f"✔ {png_path.relative_to(ROOT)} ({png_path.stat().st_size // 1024} КБ, {PREVIEW_DPI} dpi)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

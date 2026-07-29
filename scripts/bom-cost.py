#!/usr/bin/env python3
"""Считает стоимость проекта по таблицам из docs/purchases.md.

Источник истины — сам markdown-файл, а не отдельная база: правится документ,
скрипт только пересчитывает. Таблицы размечены комментариями вида
    <!-- cost-table: confirmed -->
где вид таблицы — один из: confirmed (цена подтверждена), estimate (диапазон
оценки), free (досталось бесплатно), unknown (цены нет, в сумму не идёт).

Формат ячейки цены: "251", "3500-6500" (дефис или тире), "0", "-" / "—".

Запуск:
    python3 scripts/bom-cost.py [путь/к/purchases.md]
"""

import re
import sys
from pathlib import Path

KINDS = ("confirmed", "estimate", "free", "unknown")
TABLE_MARKER = re.compile(r"<!--\s*cost-table:\s*(\w+)\s*-->")
PRICE_HEADER = "цена"
# Диапазон: 3500-6500 / 3500–6500 / 3500 — 6500. Разделители тысяч (пробел) чистим заранее.
RANGE = re.compile(r"^(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)$")
SINGLE = re.compile(r"^(\d+(?:\.\d+)?)$")


def split_row(line):
    """Разбирает строку markdown-таблицы в список ячеек."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_price(cell):
    """Возвращает (min, max) или None, если цены нет."""
    text = cell.replace(" ", " ").replace(" ", "").replace("₽", "")
    text = re.sub(r"\*\*|\*", "", text).strip()
    if not text or text in {"-", "–", "—", "?"}:
        return None
    m = RANGE.match(text)
    if m:
        low, high = float(m.group(1)), float(m.group(2))
        return (min(low, high), max(low, high))
    m = SINGLE.match(text)
    if m:
        value = float(m.group(1))
        return (value, value)
    return None


def parse(path):
    """Читает файл и возвращает {kind: [(название, min, max, есть_цена), ...]}."""
    items = {kind: [] for kind in KINDS}
    kind = None
    price_col = None
    name_col = 0

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        marker = TABLE_MARKER.search(line)
        if marker:
            kind = marker.group(1)
            if kind not in items:
                sys.exit(f"неизвестный вид таблицы: {kind!r} (ожидались {KINDS})")
            price_col = None          # ждём заголовок таблицы
            continue

        if kind is None:
            continue

        if not line.startswith("|"):
            # Таблица закончилась (пустая строка, заголовок, текст).
            if line:
                kind = None
            continue

        cells = split_row(line)

        if price_col is None:
            # Первая строка таблицы после маркера — заголовок.
            for i, cell in enumerate(cells):
                if PRICE_HEADER in cell.lower():
                    price_col = i
            if price_col is None:
                sys.exit(f"в таблице {kind!r} не найден столбец с ценой")
            continue

        if set("".join(cells)) <= set("-: "):
            continue                  # разделитель |---|---|

        if len(cells) <= price_col:
            continue

        name = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[name_col])
        name = re.sub(r"\*\*|\*|`", "", name).strip()
        price = parse_price(cells[price_col])
        if price is None:
            items[kind].append((name, 0.0, 0.0, False))
        else:
            items[kind].append((name, price[0], price[1], True))

    return items


def money(value):
    return f"{value:,.0f}".replace(",", " ")


def main():
    default = Path(__file__).resolve().parent.parent / "docs" / "purchases.md"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    if not path.exists():
        sys.exit(f"не найден файл {path}")

    items = parse(path)

    confirmed = sum(low for _, low, _, priced in items["confirmed"] if priced)
    est_low = sum(low for _, low, _, priced in items["estimate"] if priced)
    est_high = sum(high for _, _, high, priced in items["estimate"] if priced)
    free_count = len(items["free"])
    unpriced = [name for name, _, _, priced in
                items["unknown"] + items["confirmed"] + items["estimate"] if not priced]

    total_low = confirmed + est_low
    total_high = confirmed + est_high

    width = 34
    print()
    print(f"{'Подтверждённые траты:':<{width}}{money(confirmed):>16} ₽"
          f"   ({len(items['confirmed'])} позиций)")
    print(f"{'Оценка по неподтверждённым:':<{width}}"
          f"{money(est_low) + ' – ' + money(est_high):>16} ₽"
          f"   ({len(items['estimate'])} позиций)")
    print(f"{'Досталось бесплатно с mbot:':<{width}}{'0':>16} ₽   ({free_count} позиций)")
    print(f"{'Не оценено (нужны цены):':<{width}}{'—':>16}     ({len(unpriced)} позиций)")
    print("-" * (width + 24))
    print(f"{'ИТОГО по проекту:':<{width}}"
          f"{money(total_low) + ' – ' + money(total_high):>16} ₽")
    print(f"{'  середина диапазона:':<{width}}{money((total_low + total_high) / 2):>16} ₽")
    print()

    if unpriced:
        print("Без цены (в итог не входят):")
        for name in unpriced:
            print(f"  · {name}")
        print()


if __name__ == "__main__":
    main()

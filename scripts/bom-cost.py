#!/usr/bin/env python3
"""Считает стоимость проекта по таблицам из docs/purchases.md.

Все цены — в долларах США (рублёвые покупки пересчитаны в самом документе,
исходная сумма сохранена там отдельным столбцом).

Источник истины — сам markdown-файл, а не отдельная база: правится документ,
скрипт только пересчитывает. Таблицы размечены комментариями вида
    <!-- cost-table: confirmed -->
где вид таблицы — один из: confirmed (цена подтверждена), estimate (диапазон
оценки по купленному), planned (запланировано, ещё не куплено), free
(досталось бесплатно), unknown (цены нет, в сумму не идёт).

Формат ячейки цены: "3.18", "40-75" (дефис или тире), "0", "-" / "—".

Запуск:
    python3 scripts/bom-cost.py [путь/к/purchases.md]
"""

import re
import sys
from pathlib import Path

KINDS = ("confirmed", "estimate", "planned", "free", "unknown")
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
    return "$" + f"{value:,.2f}".replace(",", " ")


def span(low, high):
    return money(low) if low == high else f"{money(low)} – {money(high)}"


def positions(count):
    """«1 позиция» / «3 позиции» / «21 позиция» — иначе отчёт читается коряво."""
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        word = "позиция"
    elif tail in (2, 3, 4) and hundred not in (12, 13, 14):
        word = "позиции"
    else:
        word = "позиций"
    return f"{count} {word}"


def main():
    default = Path(__file__).resolve().parent.parent / "docs" / "purchases.md"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    if not path.exists():
        sys.exit(f"не найден файл {path}")

    items = parse(path)

    def total(kind):
        low = sum(l for _, l, _, priced in items[kind] if priced)
        high = sum(h for _, _, h, priced in items[kind] if priced)
        return low, high

    conf_low, conf_high = total("confirmed")
    est_low, est_high = total("estimate")
    plan_low, plan_high = total("planned")
    unpriced = [name for kind in KINDS for name, _, _, priced in items[kind] if not priced]

    spent_low, spent_high = conf_low + est_low, conf_high + est_high
    all_low, all_high = spent_low + plan_low, spent_high + plan_high

    n_conf = positions(len(items["confirmed"]))
    n_est = positions(len(items["estimate"]))
    n_plan = positions(len(items["planned"]))
    n_free = positions(len(items["free"]))
    n_unpriced = positions(len(unpriced))

    width = 34
    value = 20
    print()
    print(f"{'Подтверждённые траты:':<{width}}{span(conf_low, conf_high):>{value}}   ({n_conf})")
    print(f"{'Оценка по неподтверждённым:':<{width}}{span(est_low, est_high):>{value}}   ({n_est})")
    print(f"{'Запланировано, не куплено:':<{width}}{span(plan_low, plan_high):>{value}}   ({n_plan})")
    print(f"{'Досталось бесплатно с mbot:':<{width}}{money(0):>{value}}   ({n_free})")
    if unpriced:
        print(f"{'Не оценено (нужны цены):':<{width}}{'—':>{value}}   ({n_unpriced})")
    print("-" * (width + value + 6))
    print(f"{'ПОТРАЧЕНО (факт + оценка):':<{width}}{span(spent_low, spent_high):>{value}}")
    print(f"{'  середина диапазона:':<{width}}{money((spent_low + spent_high) / 2):>{value}}")
    print(f"{'С УЧЁТОМ ПЛАНОВ:':<{width}}{span(all_low, all_high):>{value}}")
    print(f"{'  середина диапазона:':<{width}}{money((all_low + all_high) / 2):>{value}}")
    print()

    if unpriced:
        print("Без цены (в итог не входят):")
        for name in unpriced:
            print(f"  · {name}")
        print()


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Перекачивает официальную документацию MBot3D в docs/vendor/ и сверяет хеши.
#
# Зачем скрипт, если файлы и так лежат в репозитории: чтобы провенанс был
# воспроизводим, а не «кто-то когда-то принёс PDF». Запускать не обязательно —
# только чтобы проверить, что источник ещё жив и не подменился.
#
# ВАЖНО про источник: собственный саппорт-портал MBot3D (mbot3d.zendesk.com)
# уже отдаёт 403/404 по ключевым статьям, а GitHub-репозиторий MBot3D/3DPrinterKit
# последний раз обновлялся много лет назад. Это ровно тот случай, когда файлы
# держат в репозитории копией: если источник исчезнет, восстановить будет неоткуда.
# Подробнее — docs/bom.md, раздел «Про open hardware».

set -euo pipefail

BASE="https://raw.githubusercontent.com/MBot3D/3DPrinterKit/master"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docs/vendor"

# имя-в-репозитории | путь-в-источнике | sha256 на момент скачивания (2026-07-30)
FILES=(
  "mbot3d-3DPKit_HowToAssembly_v1.pdf|Docs/3DPKit_HowToAssembly_v1.pdf|f668ee72e398aa1dbd07c209df50a5f2a1786529759391ef5556a55ff8b5854f"
  "mbot3d-3DPrinter_User_Manual_V1.pdf|Docs/3DPrinter%20User%20Manual_V1.pdf|33fe72dd1e6009d1edc7288957822aa356e9070e7b8a5fa896e3c941930d7ba1"
)

mkdir -p "$DEST"
rc=0

for entry in "${FILES[@]}"; do
  IFS='|' read -r name path want <<<"$entry"
  tmp="$(mktemp)"

  echo "→ $name"
  if ! curl -fsSL --max-time 300 -o "$tmp" "$BASE/$path"; then
    echo "  ОШИБКА: не скачалось. Источник мог умереть — копия в репозитории остаётся единственной." >&2
    rm -f "$tmp"; rc=1; continue
  fi

  got="$(sha256sum "$tmp" | cut -d' ' -f1)"
  if [ "$got" = "$want" ]; then
    echo "  ok, хеш совпал"
  else
    echo "  ⚠ хеш ИЗМЕНИЛСЯ (ожидали $want, получили $got)" >&2
    echo "    Источник обновил файл. Сравнить содержимое вручную ПЕРЕД тем, как коммитить новую версию." >&2
    rc=1
  fi

  mv "$tmp" "$DEST/$name"
done

exit $rc

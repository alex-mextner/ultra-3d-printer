#!/usr/bin/env bash
# Скачивает модели с Thingiverse в hardware/case/models/ вместе с метаданными
# и лицензией, и генерирует SOURCES.md с атрибуцией.
#
# ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ «просто скачать браузером»:
# лицензии у этих вещей — CC BY-SA 4.0 и CC BY 4.0, обе ТРЕБУЮТ указания автора
# и ссылки на лицензию при распространении. Раз файлы кладутся в git, атрибуция
# обязана приехать вместе с ними и не «потеряться» — поэтому её генерирует
# скрипт, а не человек по памяти.
#
# ТОКЕН. Публичного доступа к списку файлов у Thingiverse нет: страница вещи —
# это SPA, а api.thingiverse.com на запрос без ключа отвечает 401. Нужен
# бесплатный app token: https://www.thingiverse.com/apps/create (тип "Desktop"),
# после создания скопировать App Token.
# Положить в .env в корне репозитория (файл в .gitignore, в git не попадает):
#     THINGIVERSE_TOKEN=...
# Это ровно та же схема, что уже принята для остальных секретов проекта.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/hardware/case/models"

# Вещи, которые тянем. id|краткое-имя-каталога
THINGS=(
  "28499|mbot-cube"
  "183018|mbot-cube-kit"
  "4056766|left-bar-holder-remake"
)

if [ -f "$ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$ROOT/.env"; set +a
fi

if [ -z "${THINGIVERSE_TOKEN:-}" ]; then
  cat >&2 <<'EOF'
ОШИБКА: не задан THINGIVERSE_TOKEN.

Без него список файлов не получить: api.thingiverse.com отвечает 401, а
страница вещи отдаёт только JS-оболочку без имён файлов (проверено 2026-07-30).

Что сделать:
  1. https://www.thingiverse.com/apps/create → тип "Desktop" → создать
  2. скопировать App Token
  3. дописать в .env в корне репозитория:  THINGIVERSE_TOKEN=...
  4. запустить этот скрипт снова

Ручная альтернатива, если возиться с токеном не хочется: открыть каждую вещь
в браузере, нажать Download All Files, распаковать в соответствующий каталог
hardware/case/models/<имя>/ и заполнить SOURCES.md руками (шаблон — там же).
EOF
  exit 2
fi

api() { curl -fsSL --max-time 120 -H "Authorization: Bearer $THINGIVERSE_TOKEN" "$1"; }

mkdir -p "$DEST"
SOURCES="$DEST/SOURCES.md"

{
  echo "# Источники моделей — атрибуция и лицензии"
  echo
  echo "**Файл сгенерирован \`scripts/fetch-thingiverse.sh\`. Руками не править** —"
  echo "правки затрутся при следующем запуске. Менять надо скрипт."
  echo
  echo "Все модели ниже взяты с Thingiverse и распространяются по лицензиям Creative"
  echo "Commons. Обе встречающиеся здесь лицензии требуют указания автора и ссылки"
  echo "на лицензию; **CC BY-SA дополнительно требует share-alike** — производные"
  echo "работы должны идти под той же лицензией. Это касается любой переделки этих"
  echo "деталей, которую мы положим в \`hardware/\`."
  echo
} > "$SOURCES"

for entry in "${THINGS[@]}"; do
  IFS='|' read -r id name <<<"$entry"
  dir="$DEST/$name"
  mkdir -p "$dir"

  echo "→ thing:$id ($name)"
  meta="$(api "https://api.thingiverse.com/things/$id")"
  printf '%s' "$meta" > "$dir/thing.json"

  title=$(printf '%s' "$meta"   | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))')
  author=$(printf '%s' "$meta"  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("creator",{}).get("name",""))')
  license=$(printf '%s' "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("license",""))')

  files="$(api "https://api.thingiverse.com/things/$id/files")"
  printf '%s' "$files" > "$dir/files.json"

  count=0
  while IFS='|' read -r fname furl; do
    [ -z "$fname" ] && continue
    echo "   • $fname"
    curl -fsSL --max-time 300 -H "Authorization: Bearer $THINGIVERSE_TOKEN" \
      -o "$dir/$fname" "$furl"
    count=$((count + 1))
  done < <(printf '%s' "$files" | python3 -c '
import json, sys
for f in json.load(sys.stdin):
    url = f.get("direct_url") or f.get("download_url") or ""
    if url:
        print("{}|{}".format(f.get("name", ""), url))
')

  {
    echo "## $title"
    echo
    echo "- Автор: **$author**"
    echo "- Источник: https://www.thingiverse.com/thing:$id"
    echo "- Лицензия: $license"
    echo "- Каталог: \`$name/\` ($count файл(ов))"
    echo
  } >> "$SOURCES"
done

echo
echo "Готово. Атрибуция записана в $SOURCES"
echo "Проверить размеры перед коммитом:  du -sh $DEST/*"

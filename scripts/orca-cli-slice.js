#!/usr/bin/env node
/*
 * orca-cli-slice.js — нарезать модель OrcaSlicer'ом из командной строки.
 *
 * НЕ имеет отношения к принтеру: ничего не деплоит, по SSH не ходит, лежит здесь
 * только потому, что это единственный воспроизводимый способ проверить, что
 * реально попадает в gcode из наших профилей (см. docs/tasks.md, P8).
 *
 * Запуск (Node из "C:\Program Files\nodejs\", в PATH Git Bash он не попадает):
 *   "/c/Program Files/nodejs/node.exe" scripts/orca-cli-slice.js model.stl -o out
 *
 * Ключи:
 *   -o <dir>            куда положить gcode (по умолчанию ./orca-out)
 *   --machine <name>    имя машинного пресета   (по умолчанию "ULTRA mini 0.6 nozzle")
 *   --process <name>    имя процессного пресета (по умолчанию "0.20mm Standard @MyKlipper")
 *   --filament <name>   имя пресета филамента   (по умолчанию "Generic PETG @System")
 *   --set <type>.<key>=<json>   разовый override, type = machine|process|filament
 *                       напр. --set machine.machine_max_acceleration_x='["1500","1500"]'
 *   --keep              не удалять развёрнутые промежуточные JSON
 *
 * ПОЧЕМУ ЭТО НЕ ОДНА СТРОЧКА С orca-slicer.exe. Два независимых препятствия,
 * оба стоили сессии 2026-08-18:
 *
 *  1. `--load-settings` НЕ разворачивает `inherits`. В OrcaSlicer.cpp обе строки,
 *     которые могли бы это сделать, закомментированы. Пресет надо подать плоским —
 *     этим и занят flatten() ниже.
 *
 *  2. Но `inherits` при этом обязан ОСТАТЬСЯ в файле как строка. Для пресета с
 *     `"from": "User"` слайсер берёт `new_printer_system_name = inherits` и сверяет
 *     его со списком `compatible_printers` процессного пресета
 *     (OrcaSlicer.cpp:2560-2600). Плоский файл без `inherits` сверяется с пустой
 *     строкой, не совпадает ни с чем и падает на строке 2652
 *     "process not compatible with printer" — БЕЗ ТЕКСТА в консоли, потому что это
 *     BOOST_LOG_TRIVIAL(error), а не cerr. Наружу видно только
 *     "Slic3r::CLI::run found error, exit". Пустой `compatible_printers` не спасает:
 *     в этой ветке проверки нет отката «пустой список = совместимо».
 *     Отсюда правило: пресет плоский, но `inherits` в нём сохранён.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const cp = require('child_process');

const ORCA = process.env.ORCA_EXE || 'C:\\Program Files\\OrcaSlicer\\orca-slicer.exe';
const DATA = process.env.ORCA_DATA || path.join(process.env.APPDATA, 'OrcaSlicer');
const VENDORS = ['Custom', 'OrcaFilamentLibrary'];

// ---------- индекс пресетов: имя -> файл ----------
const index = {};
for (const v of VENDORS) {
  const bundleFile = path.join(DATA, 'system', v + '.json');
  if (!fs.existsSync(bundleFile)) continue;
  const bundle = JSON.parse(fs.readFileSync(bundleFile, 'utf8'));
  for (const listKey of ['machine_list', 'process_list', 'filament_list']) {
    for (const e of bundle[listKey] || []) index[e.name] = path.join(DATA, 'system', v, e.sub_path);
  }
}
for (const kind of ['machine', 'process', 'filament']) {           // пользовательские перекрывают системные
  const dir = path.join(DATA, 'user', 'default', kind);
  if (!fs.existsSync(dir)) continue;
  for (const f of fs.readdirSync(dir)) if (f.endsWith('.json')) index[f.slice(0, -5)] = path.join(dir, f);
}

// Ключи, которые load_from_json() разбирает как метаданные, а не как значения конфига.
const META = new Set(['type', 'name', 'from', 'inherits', 'instantiation', 'setting_id',
                      'version', 'is_custom_defined', 'description', 'filament_id',
                      'filament_settings_id']);

function chain(name) {
  const out = [];
  const seen = new Set();
  for (let cur = name; cur; ) {
    if (seen.has(cur)) throw new Error('цикл inherits на ' + cur);
    seen.add(cur);
    if (!index[cur]) throw new Error('пресет не найден: ' + cur + ' (искал в ' + DATA + ')');
    const j = JSON.parse(fs.readFileSync(index[cur], 'utf8'));
    out.unshift(j);                                                 // корень первым
    cur = j.inherits;
  }
  return out;
}

function flatten(name, overrides) {
  const c = chain(name);
  const doc = {};
  const meta = {};
  const merged = {};
  for (const j of c) for (const [k, v] of Object.entries(j)) {
    if (META.has(k)) meta[k] = v; else merged[k] = v;               // ребёнок перекрывает родителя целиком
  }
  doc.type = meta.type;
  doc.name = name;
  doc.from = 'User';
  // Последний instantiable системный предок — именно та строка, которую слайсер
  // сверяет с compatible_printers. Без неё см. пункт 2 в шапке файла.
  const sysAncestor = c.filter(j => j.from === 'system' && j.instantiation === 'true').pop();
  if (sysAncestor) doc.inherits = sysAncestor.name;
  if (meta.filament_id) doc.filament_id = meta.filament_id;
  Object.assign(doc, merged, overrides || {});
  return doc;
}

// ---------- разбор аргументов ----------
if (require.main === module) {
  const argv = process.argv.slice(2);
  const opt = {
    machine: 'ULTRA mini 0.6 nozzle',
    process: '0.20mm Standard @MyKlipper',
    filament: 'Generic PETG @System',
    out: path.resolve('orca-out'),
    keep: false,
    models: [],
    over: { machine: {}, process: {}, filament: {} },
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '-o' || a === '--outputdir') opt.out = path.resolve(argv[++i]);
    else if (a === '--machine' || a === '--process' || a === '--filament') opt[a.slice(2)] = argv[++i];
    else if (a === '--keep') opt.keep = true;
    else if (a === '--set') {
      const s = argv[++i];
      const eq = s.indexOf('=');
      const dot = s.indexOf('.');
      const kind = s.slice(0, dot);
      if (!opt.over[kind]) throw new Error('--set: тип должен быть machine|process|filament, получено ' + kind);
      opt.over[kind][s.slice(dot + 1, eq)] = JSON.parse(s.slice(eq + 1));
    } else if (a === '-h' || a === '--help') {
      console.log(fs.readFileSync(__filename, 'utf8').split('*/')[0]); process.exit(0);
    } else opt.models.push(path.resolve(a));
  }
  if (!opt.models.length) { console.error('нужна хотя бы одна модель (.stl/.3mf/.step)'); process.exit(2); }

  fs.mkdirSync(opt.out, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(require('os').tmpdir(), 'orca-cli-'));
  const mF = path.join(tmp, 'machine.json');
  const pF = path.join(tmp, 'process.json');
  const fF = path.join(tmp, 'filament.json');
  fs.writeFileSync(mF, JSON.stringify(flatten(opt.machine, opt.over.machine), null, 1));
  fs.writeFileSync(pF, JSON.stringify(flatten(opt.process, opt.over.process), null, 1));
  fs.writeFileSync(fF, JSON.stringify(flatten(opt.filament, opt.over.filament), null, 1));

  const args = ['--load-settings', mF + ';' + pF, '--load-filaments', fF,
                '--slice', '0', '--outputdir', opt.out, ...opt.models];
  const r = cp.spawnSync(ORCA, args, { encoding: 'utf8' });
  const out = (r.stdout || '') + (r.stderr || '');
  if (out.trim()) console.log(out.trim());

  const made = fs.readdirSync(opt.out).filter(f => f.endsWith('.gcode') || f.endsWith('.gcode.3mf'));
  if (r.status !== 0 || !made.length) {
    console.error('\nНАРЕЗКА НЕ ПРОШЛА (код ' + r.status + ').');
    console.error('Пустое сообщение = ошибка ушла в boost log, а не в консоль.');
    console.error('Смотреть outputdir на файл отчёта и docs/tasks.md, P8.');
    console.error('Промежуточные пресеты оставлены в ' + tmp);
    process.exit(r.status || 1);
  }
  for (const f of made) {
    const p = path.join(opt.out, f);
    const txt = fs.readFileSync(p, 'utf8');
    console.log(f + ': ' + (fs.statSync(p).size / 1024).toFixed(0) + ' КБ, слоёв ' +
                (txt.match(/;LAYER_CHANGE/g) || []).length + ', экструзий ' +
                (txt.match(/^G1 .*E[0-9]/gm) || []).length);
  }
  if (opt.keep) console.log('пресеты: ' + tmp);
  else fs.rmSync(tmp, { recursive: true, force: true });
}

module.exports = { flatten, chain, index };

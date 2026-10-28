#!/usr/bin/env node
// Проверка честности оценки остатка: прогоняет etaRange из шаблона панели по сданным
// прогонам и считает, как часто реальное оставшееся рабочее время попало в показанный диапазон.
//
//   node tools/eta-honesty.cjs <state.js> [<state.js> …]
//
// Формула берётся из шаблона по меткам ETA-BEGIN / ETA-END, как есть. Для каждого сданного
// прогона состояние восстанавливается на каждую метку времени (с тем, что было известно в тот момент,
// и с историей, которую init положил в этот state.js). Диапазон только разработки (tailUnknown:
// проверка и приёмка без истории) сверяется с реальным концом этапа «Разработка». Вывод — только числа:
// ни названий, ни slug, ни путей.
'use strict';
const fs = require('fs');
const path = require('path');

const template = path.join(__dirname, '..', 'skills', 'aispa', 'phases', 'dashboard-template.html');
const html = fs.readFileSync(template, 'utf8');
const block = html.match(/\/\*ETA-BEGIN\*\/([\s\S]*?)\/\*ETA-END\*\//);
const cap = html.match(/const IDLE_CAP_MS = (\d+)/);
if (!block || !cap) {
  console.error('в шаблоне нет формулы остатка');
  process.exit(2);
}
const eta = new Function('IDLE_CAP_MS',
  block[1] + '; return {etaRange, stateMarks, collectWaits, workMs, toMs};')(+cap[1]);

function load(file) {
  const raw = fs.readFileSync(file, 'utf8');
  return JSON.parse(raw.slice(raw.indexOf('=') + 1).trim().replace(/;$/, ''));
}

// Состояние на момент t: только то, что уже было отмечено.
function asOf(S, t) {
  const X = JSON.parse(JSON.stringify(S));
  const iso = ms => new Date(ms).toISOString();
  const cut = running => x => {
    const a = eta.toMs(x.startedAt), b = eta.toMs(x.finishedAt);
    if (a == null || a > t) {
      delete x.startedAt; delete x.finishedAt;
      return x.status === 'skipped' && b != null && b <= t ? x : Object.assign(x, {status: 'pending'});
    }
    if (b == null || b > t) {
      delete x.finishedAt;
      x.status = running;
    }
    return x;
  };
  X.stages = (X.stages || []).map(cut('active'));
  X.tickets = (X.tickets || []).map(cut('in-progress'));
  X.finishedAt = null;
  X.updatedAt = iso(t);
  X.beats = (X.beats || []).filter(b => eta.toMs(b) <= t);
  X.waiting = null;
  X.waits = (X.waits || []).filter(w => eta.toMs(w.from) <= t).map(w => {
    if (eta.toMs(w.to) > t) { X.waiting = {since: w.from}; return null; }
    return w;
  }).filter(Boolean);
  return X;
}

let ranged = 0, inside = 0, unknown = 0;
for (const file of process.argv.slice(2)) {
  const S = load(file);
  const end = eta.toMs(S.finishedAt);
  if (end == null) continue;                              // идущие прогоны не судим
  const marks = eta.stateMarks(S), waits = eta.collectWaits(S);
  const build = (S.stages || []).find(s => s.id === 'build') || {};
  const buildEnd = eta.toMs(build.finishedAt);
  for (const t of marks) {
    if (t >= end) continue;
    const r = eta.etaRange(asOf(S, t), t);
    if (!r) continue;
    if (r.unknown) { unknown += 1; continue; }
    const until = r.tailUnknown ? buildEnd : end;
    if (until == null) continue;                          // разработка не закончилась — судить не с чем
    ranged += 1;
    const real = eta.workMs(marks, waits, t, until, end) / 60000;
    if (real >= r.lo && real <= r.hi) inside += 1;
  }
}
console.log('точек с диапазоном: ' + ranged);
console.log('попало внутрь: ' + inside);
console.log('пока не знаю: ' + unknown);
const share = n => (ranged + unknown ? Math.round(100 * n / (ranged + unknown)) : 0) + '%';
console.log('доля с диапазоном: ' + share(ranged));
console.log('доля «пока не знаю»: ' + share(unknown));

/* static/js/dashboard.js — FruitSorter Dashboard Logic
 * Kết nối Flask-SocketIO, nhận stats_update event, cập nhật UI.
 */

'use strict';

// ── Constants ────────────────────────────────────────────────────────────
const BAR_BINS    = 20;         // 20 cột × 3s = 60s window
const BIN_TICK_MS = 3000;
const CIRC        = 2 * Math.PI * 46;  // SVG r=46

// ── State ─────────────────────────────────────────────────────────────────
let prev           = { GREEN: 0, RED: 0, YELLOW: 0, rejects: 0 };
let throughput     = new Array(BAR_BINS).fill(0);
let currentDelta   = 0;
let eventCount     = 0;

// ── DOM refs ──────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

// ── Clock ─────────────────────────────────────────────────────────────────
setInterval(() => {
  $('clock').textContent = new Date().toLocaleTimeString('vi-VN', { hour12: false });
}, 1000);

// ── SocketIO ──────────────────────────────────────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => {
  $('ws-dot').className   = 'status-dot online';
  $('ws-label').textContent = 'Live';
  loadBootstrapData();
});

socket.on('disconnect', () => {
  $('ws-dot').className   = 'status-dot offline';
  $('ws-label').textContent = 'Offline';
});

socket.on('stats_update', data => {
  applyStats(data);
});

// Load today's persisted stats from DB on page open
async function loadBootstrapData() {
  try {
    const r = await fetch('/api/stats/today');
    if (!r.ok) return;
    const d = await r.json();
    applyStats({
      GREEN:   d.green   || 0,
      RED:     d.red     || 0,
      YELLOW:  d.yellow  || 0,
      rejects: d.rejects || 0,
    });
  } catch (e) { /* ignore */ }

  try {
    const r = await fetch('/api/events/recent?limit=30');
    if (!r.ok) return;
    const events = await r.json();
    events.reverse().forEach(e => addLogEntry({
      fruit_color: e.fruit_color,
      confidence:  e.confidence,
      action:      e.action,
      station:     e.station,
      is_reject:   e.is_reject,
      ts_ms:       e.sorted_at,
    }));
  } catch (e) { /* ignore */ }
}

// ── Stats update ──────────────────────────────────────────────────────────
function applyStats(data) {
  const g   = data.GREEN   || 0;
  const r   = data.RED     || 0;
  const y   = data.YELLOW  || 0;
  const rej = data.rejects || 0;
  const tot = g + r + y;

  // Stat cards
  setCard('cnt-green',  g,   'sub-green',  prev.GREEN,  'GREEN');
  setCard('cnt-red',    r,   'sub-red',    prev.RED,    'RED');
  setCard('cnt-yellow', y,   'sub-yellow', prev.YELLOW, 'YELLOW');

  $('cnt-total').textContent = tot;
  $('sub-reject').textContent = tot > 0
    ? `Reject ${Math.round(rej / (tot + rej) * 100)}%`
    : 'Reject 0%';

  // Sidebar
  $('sb-green').textContent   = g;
  $('sb-red').textContent     = r;
  $('sb-yellow').textContent  = y;
  $('sb-rejects').textContent = rej;

  // Donut
  updateDonut(g, r, y, tot);

  // Throughput delta
  const newTot = tot;
  const oldTot = (prev.GREEN || 0) + (prev.RED || 0) + (prev.YELLOW || 0);
  currentDelta += Math.max(0, newTot - oldTot);

  $('last-update').textContent = 'Updated ' +
    new Date().toLocaleTimeString('vi-VN', { hour12: false });

  prev = { GREEN: g, RED: r, YELLOW: y, rejects: rej };
}

function setCard(valId, val, subId, prevVal, colorKey) {
  const el = $(valId);
  if (parseInt(el.textContent) !== val) {
    el.textContent = val;
    el.classList.remove('bumping');
    void el.offsetWidth;
    el.classList.add('bumping');
  }
  const diff = val - (prevVal || 0);
  $(subId).textContent = diff > 0 ? `+${diff} this session` : '—';
  $(subId).className = 'stat-card__sub' + (diff > 0 ? ' up' : '');
}

// ── Donut ─────────────────────────────────────────────────────────────────
function updateDonut(g, r, y, tot) {
  const total = tot || 1;
  const gArc = (g / total) * CIRC;
  const rArc = (r / total) * CIRC;
  const yArc = (y / total) * CIRC;
  const gPct = Math.round(g / total * 100);
  const rPct = Math.round(r / total * 100);
  const yPct = 100 - gPct - rPct;

  setArc('d-green',  gArc, 0);
  setArc('d-red',    rArc, gArc);
  setArc('d-yellow', yArc, gArc + rArc);

  $('pct-green').textContent  = gPct + '%';
  $('pct-red').textContent    = rPct + '%';
  $('pct-yellow').textContent = yPct + '%';
  $('donut-total').textContent = tot;
}

function setArc(id, arc, offset) {
  const el = $(id);
  el.setAttribute('stroke-dasharray', `${arc.toFixed(2)} ${(CIRC - arc).toFixed(2)}`);
  // Rotate via dashoffset trick: offset prior arcs
  // SVG starts at top (rotate(-90)), each arc starts after previous
  const r = 46;
  // We use stroke-dashoffset to shift the start of this arc
  el.setAttribute('stroke-dashoffset', (-offset).toFixed(2));
}

// ── Throughput bars ───────────────────────────────────────────────────────
function rebuildBars() {
  const bc    = $('bar-chart');
  const bx    = $('bar-xaxis');
  const maxV  = Math.max(...throughput, 1);
  bc.innerHTML = '';
  bx.innerHTML = '';

  throughput.forEach((v, i) => {
    const h   = Math.max(3, Math.round((v / maxV) * 68));
    const age = (BAR_BINS - 1 - i) * (BIN_TICK_MS / 1000);
    const op  = 0.25 + (i / (BAR_BINS - 1)) * 0.75;

    const bar = document.createElement('div');
    bar.className = 'bar-chart__bar';
    bar.style.height     = h + 'px';
    bar.style.background = `rgba(99,102,241,${op.toFixed(2)})`;
    bc.appendChild(bar);

    const lbl = document.createElement('div');
    lbl.className = 'bar-chart-xaxis__lbl';
    lbl.textContent = i === BAR_BINS - 1 ? 'now' : (age > 0 ? `-${age}s` : '');
    bx.appendChild(lbl);
  });
}

rebuildBars();

setInterval(() => {
  throughput.shift();
  throughput.push(currentDelta);
  currentDelta = 0;
  rebuildBars();
}, BIN_TICK_MS);

// ── Event log ─────────────────────────────────────────────────────────────
socket.on('sort_event', e => addLogEntry(e));

function addLogEntry(e) {
  const log = $('event-log');
  if (log.children.length >= 200) log.lastElementChild?.remove();

  const ts  = e.ts_ms
    ? new Date(e.ts_ms).toLocaleTimeString('vi-VN', { hour12: false })
    : new Date().toLocaleTimeString('vi-VN', { hour12: false });

  const row = document.createElement('div');
  row.className = 'log-entry' + (e.is_reject ? ' reject' : '');
  row.innerHTML = `
    <span class="log-entry__time">${ts}</span>
    <span class="log-entry__color ${e.fruit_color}">${e.fruit_color}</span>
    <span class="log-entry__conf">${(e.confidence * 100).toFixed(0)}%</span>
    <span class="log-entry__action">${e.action}</span>
    <span class="log-entry__station">IR${e.station || 1}</span>
  `;
  log.prepend(row);

  eventCount++;
  $('event-count').textContent = eventCount + ' events';
}
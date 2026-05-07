/* static/js/dashboard.js — FruitSorter Dashboard */
'use strict';

const CIRC = 2 * Math.PI * 46;

let prev       = { GREEN: 0, RED: 0, YELLOW: 0, rejects: 0 };
let eventCount = 0;

const $ = id => document.getElementById(id);

// ── Clock ─────────────────────────────────────────────────────────────────
setInterval(() => {
  $('clock').textContent = new Date().toLocaleTimeString('vi-VN', { hour12: false });
}, 1000);

// ── SocketIO ──────────────────────────────────────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => {
  $('ws-dot').className    = 'status-dot online';
  $('ws-label').textContent = 'Live';
  loadBootstrapData();
});

socket.on('disconnect', () => {
  $('ws-dot').className    = 'status-dot offline';
  $('ws-label').textContent = 'Offline';
});

socket.on('stats_update', data => applyStats(data));
socket.on('sort_event',   e    => addLogEntry(e));

// ── Detection event từ server (push ngay khi detect) ──────────────────────
socket.on('detection', data => {
  const { label, confidence } = data;
  showDetectionOverlay(label, confidence);
});

// ── Bootstrap data khi mở trang ───────────────────────────────────────────
async function loadBootstrapData() {
  try {
    const r = await fetch('/api/stats/today');
    if (!r.ok) return;
    const d = await r.json();
    applyStats({ GREEN: d.green||0, RED: d.red||0, YELLOW: d.yellow||0, rejects: d.rejects||0 });
  } catch {}

  try {
    const r = await fetch('/api/events/recent?limit=30');
    if (!r.ok) return;
    const events = await r.json();
    events.reverse().forEach(e => addLogEntry({
      fruit_color: e.fruit_color, confidence: e.confidence,
      action: e.action, station: e.station,
      is_reject: e.is_reject, ts_ms: e.sorted_at,
    }));
  } catch {}
}

// ── Stats update ──────────────────────────────────────────────────────────
function applyStats(data) {
  const g   = data.GREEN   || 0;
  const r   = data.RED     || 0;
  const y   = data.YELLOW  || 0;
  const rej = data.rejects || 0;
  const tot = g + r + y;

  setCard('cnt-green',  g, 'sub-green',  prev.GREEN);
  setCard('cnt-red',    r, 'sub-red',    prev.RED);
  setCard('cnt-yellow', y, 'sub-yellow', prev.YELLOW);

  $('cnt-total').textContent  = tot;
  $('sub-reject').textContent = tot > 0
    ? `Reject ${Math.round(rej / (tot + rej) * 100)}%`
    : 'Reject 0%';

  $('sb-green').textContent   = g;
  $('sb-red').textContent     = r;
  $('sb-yellow').textContent  = y;
  $('sb-rejects').textContent = rej;

  updateDonut(g, r, y, tot);

  $('last-update').textContent = 'Updated ' +
    new Date().toLocaleTimeString('vi-VN', { hour12: false });

  prev = { GREEN: g, RED: r, YELLOW: y, rejects: rej };
}

function setCard(valId, val, subId, prevVal) {
  const el = $(valId);
  if (parseInt(el.textContent) !== val) {
    el.textContent = val;
    el.classList.remove('bumping');
    void el.offsetWidth;
    el.classList.add('bumping');
  }
  const diff = val - (prevVal || 0);
  $(subId).textContent = diff > 0 ? `+${diff} this session` : '—';
  $(subId).className   = 'stat-card__sub' + (diff > 0 ? ' up' : '');
}

// ── Donut ─────────────────────────────────────────────────────────────────
function updateDonut(g, r, y, tot) {
  const total = tot || 1;
  const gArc  = (g / total) * CIRC;
  const rArc  = (r / total) * CIRC;
  const yArc  = (y / total) * CIRC;

  setArc('d-green',  gArc, 0);
  setArc('d-red',    rArc, gArc);
  setArc('d-yellow', yArc, gArc + rArc);

  $('pct-green').textContent  = Math.round(g / total * 100) + '%';
  $('pct-red').textContent    = Math.round(r / total * 100) + '%';
  $('pct-yellow').textContent = (100 - Math.round(g / total * 100) - Math.round(r / total * 100)) + '%';
  $('donut-total').textContent = tot;
}

function setArc(id, arc, offset) {
  const el = $(id);
  el.setAttribute('stroke-dasharray',  `${arc.toFixed(2)} ${(CIRC - arc).toFixed(2)}`);
  el.setAttribute('stroke-dashoffset', (-offset).toFixed(2));
}

// ── Camera stream ──────────────────────────────────────────────────────────
let _camOk        = false;
let _lastFrameTs  = 0;
let _frameCount   = 0;
let _fpsCounter   = 0;
let _fpsLastTs    = performance.now();
let _camCheckTimer = null;

function handleCamLoad() {
  _camOk = true;
  $('cam-offline').classList.remove('visible');
  $('cam-live').classList.add('active');

  // FPS 계산: 매 프레임마다 카운터 증가
  _frameCount++;
  _fpsCounter++;
  const now = performance.now();
  if (now - _fpsLastTs >= 1000) {
    const fps = Math.round(_fpsCounter * 1000 / (now - _fpsLastTs));
    $('cam-fps-badge').textContent = fps + ' fps';
    _fpsCounter = 0;
    _fpsLastTs  = now;
  }
  _lastFrameTs = now;

  // Reset error check timer
  clearTimeout(_camCheckTimer);
  _camCheckTimer = setTimeout(checkCamAlive, 3000);
}

function handleCamError() {
  _camOk = false;
  $('cam-offline').classList.add('visible');
  $('cam-live').classList.remove('active');
  $('cam-fps-badge').textContent = '-- fps';

  // Thử reload sau 2s
  setTimeout(() => {
    const img = $('cam-stream');
    img.src = '/video_feed?' + Date.now();
  }, 2000);
}

function checkCamAlive() {
  const elapsed = performance.now() - _lastFrameTs;
  if (elapsed > 3000) {
    handleCamError();
  }
}

// ── Detection overlay ─────────────────────────────────────────────────────
let _detHideTimer = null;

function showDetectionOverlay(label, confidence) {
  const overlay  = $('cam-det-overlay');
  const labelEl  = $('cam-det-label');
  const confEl   = $('cam-det-conf');
  const badge    = $('cam-detect-badge');

  // Update overlay
  labelEl.textContent  = label;
  labelEl.className    = 'cam-det-label ' + label;
  confEl.textContent   = (confidence * 100).toFixed(0) + '%';
  overlay.style.display = 'flex';

  // Update header badge
  badge.textContent = label + ' ' + (confidence * 100).toFixed(0) + '%';
  badge.className   = 'cam-badge cam-badge--detect ' + label.toLowerCase();

  // Auto-hide sau 2s nếu không có detection mới
  clearTimeout(_detHideTimer);
  _detHideTimer = setTimeout(() => {
    overlay.style.display = 'none';
    badge.textContent     = 'No detection';
    badge.className       = 'cam-badge cam-badge--detect';
  }, 2000);
}

// ── Event log ─────────────────────────────────────────────────────────────
function addLogEntry(e) {
  const logEl = $('event-log');
  if (logEl.children.length >= 200) logEl.lastElementChild?.remove();

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
  logEl.prepend(row);

  eventCount++;
  $('event-count').textContent = eventCount + ' events';

  // Khi có event mới, show detection overlay nếu không phải reject
  if (!e.is_reject) {
    showDetectionOverlay(e.fruit_color, e.confidence);
  }
}

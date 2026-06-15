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
  $('ws-dot').className     = 'status-dot online';
  $('ws-label').textContent = 'Live';
  loadBootstrapData();
});

socket.on('disconnect', () => {
  $('ws-dot').className     = 'status-dot offline';
  $('ws-label').textContent = 'Offline';
});

socket.on('stats_update', data => applyStats(data));
socket.on('sort_event',   e    => {
  addLogEntry(e);
  scheduleAnalyticsRefresh();
});

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
    applyStats({
      GREEN: d.green || 0,
      RED: d.red || 0,
      YELLOW: d.yellow || 0,
      rejects: d.rejects || 0,
      total: d.total,
    });
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

  loadAnalyticsData();
}

// ── Stats update ──────────────────────────────────────────────────────────
function applyStats(data) {
  const g   = data.GREEN   || 0;
  const r   = data.RED     || 0;
  const y   = data.YELLOW  || 0;
  const rej = data.rejects || 0;
  const accepted = g + r + y;
  const reportedTotal = Number(data.total);
  const tot = Number.isFinite(reportedTotal) ? reportedTotal : accepted + rej;

  setCard('cnt-green',  g, 'sub-green',  prev.GREEN);
  setCard('cnt-red',    r, 'sub-red',    prev.RED);
  setCard('cnt-yellow', y, 'sub-yellow', prev.YELLOW);

  $('cnt-total').textContent  = tot;
  $('sub-reject').textContent = tot > 0
    ? `Reject ${Math.round(rej / tot * 100)}%`
    : 'Reject 0%';

  $('sb-green').textContent   = g;
  $('sb-red').textContent     = r;
  $('sb-yellow').textContent  = y;
  $('sb-rejects').textContent = rej;

  updateDonut(g, r, y, accepted);

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
// MJPEG stream: <img src="/video_feed"> nhận multipart liên tục.
// onload chỉ fire 1 lần khi header đến — KHÔNG dùng để track liveness.
// Thay vào đó: poll /api/health mỗi 3s để kiểm tra server còn sống không.
// Nếu server alive → coi stream OK. Nếu mất kết nối → show offline overlay.

let _camOnline = false;

function setCamOnline(online) {
  if (online === _camOnline) return;
  _camOnline = online;
  if (online) {
    $('cam-offline').classList.remove('visible');
    $('cam-live').classList.add('active');
  } else {
    $('cam-offline').classList.add('visible');
    $('cam-live').classList.remove('active');
    $('cam-fps-badge').textContent = '-- fps';
  }
}

async function fetchHealth(timeoutMs = 2000) {
  const r = await fetch('/api/health', { signal: AbortSignal.timeout(timeoutMs) });
  return await r.json();
}

function healthCameraOnline(health) {
  return health?.components?.camera?.status === 'ok';
}

// Kiểm tra camera health mỗi 3 giây.
// Health tổng thể có thể fail vì serial/DB, nên chỉ dùng component camera.
setInterval(async () => {
  try {
    setCamOnline(healthCameraOnline(await fetchHealth()));
  } catch {
    setCamOnline(false);
  }
}, 3000);

// Tính FPS dựa trên detection events từ SocketIO thay vì onload
// (chính xác hơn vì phản ánh tốc độ inference thực tế)
let _detCount  = 0;
let _fpsTs     = performance.now();

setInterval(() => {
  const now     = performance.now();
  const elapsed = (now - _fpsTs) / 1000;
  if (elapsed > 0) {
    // Không dùng detection count vì có thể không có trái cây
    // → chỉ hiển thị "LIVE" khi server online
    if (_camOnline) {
      $('cam-fps-badge').textContent = 'streaming';
    }
  }
  _fpsTs = now;
}, 5000);

// Khi img load lần đầu — set online ngay không cần chờ poll
function handleCamLoad() {
  setCamOnline(true);
}

// Khi img thực sự lỗi (ví dụ 404, network error) — không phải do MJPEG
function handleCamError() {
  // Chỉ set offline nếu camera health cũng fail
  // (tránh false negative do browser quirk với MJPEG)
  fetchHealth(1000)
    .then(health => { if (!healthCameraOnline(health)) setCamOnline(false); })
    .catch(() => setCamOnline(false));
}

// ── Detection overlay ─────────────────────────────────────────────────────
let _detHideTimer = null;

function showDetectionOverlay(label, confidence) {
  const overlay  = $('cam-det-overlay');
  const labelEl  = $('cam-det-label');
  const confEl   = $('cam-det-conf');
  const badge    = $('cam-detect-badge');

  // Update overlay
  labelEl.textContent   = label;
  labelEl.className     = 'cam-det-label ' + label;
  confEl.textContent    = (confidence * 100).toFixed(0) + '%';
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

// ── Analytics from SQLite-backed API ──────────────────────────────────────
let _analyticsRefreshTimer = null;

document.querySelectorAll('.sidebar__item[data-jump]').forEach(item => {
  item.addEventListener('click', () => {
    document.querySelectorAll('.sidebar__item').forEach(el => el.classList.remove('active'));
    item.classList.add('active');

    const target = $(item.dataset.jump);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

setInterval(loadAnalyticsData, 10000);

function scheduleAnalyticsRefresh() {
  clearTimeout(_analyticsRefreshTimer);
  _analyticsRefreshTimer = setTimeout(loadAnalyticsData, 6000);
}

async function loadAnalyticsData() {
  try {
    const [todayRes, historyRes, recentRes] = await Promise.all([
      fetch('/api/stats/today'),
      fetch('/api/stats/history?days=14'),
      fetch('/api/events/recent?limit=25'),
    ]);

    if (!todayRes.ok || !historyRes.ok || !recentRes.ok) {
      throw new Error('Database API unavailable');
    }

    applyTodayAnalytics(await todayRes.json());
    renderHistoryTable(await historyRes.json());
    renderRecentTable(await recentRes.json());
    $('analytics-updated').textContent = 'Database updated ' +
      new Date().toLocaleTimeString('vi-VN', { hour12: false });
  } catch {
    $('analytics-updated').textContent = 'Database unavailable';
    renderTableMessage('history-table', 6, 'Cannot load daily history');
    renderTableMessage('recent-table', 6, 'Cannot load recent events');
  }
}

function applyTodayAnalytics(d) {
  $('db-green').textContent   = d.green || 0;
  $('db-red').textContent     = d.red || 0;
  $('db-yellow').textContent  = d.yellow || 0;
  $('db-rejects').textContent = d.rejects || 0;
  $('db-total').textContent   = d.total || 0;
}

function renderHistoryTable(rows) {
  const body = $('history-table');
  body.innerHTML = '';
  if (!rows.length) {
    renderTableMessage('history-table', 6, 'No daily stats yet');
    return;
  }

  rows.slice().reverse().forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(row.date || '')}</td>
      <td class="num green-text">${row.green || 0}</td>
      <td class="num red-text">${row.red || 0}</td>
      <td class="num yellow-text">${row.yellow || 0}</td>
      <td class="num">${row.rejects || 0}</td>
      <td class="num strong">${row.total || 0}</td>
    `;
    body.appendChild(tr);
  });
}

function renderRecentTable(rows) {
  const body = $('recent-table');
  body.innerHTML = '';
  if (!rows.length) {
    renderTableMessage('recent-table', 6, 'No sort events yet');
    return;
  }

  rows.forEach(row => {
    const ts = row.sorted_at
      ? new Date(row.sorted_at).toLocaleString('vi-VN', { hour12: false })
      : '—';
    const color = row.fruit_color || 'UNKNOWN';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="num">${row.id}</td>
      <td>${ts}</td>
      <td class="${colorClass(color)}">${escapeHtml(color)}</td>
      <td>${escapeHtml(row.action || '')}</td>
      <td class="num">IR${row.station || 1}</td>
      <td class="num">${formatConfidence(row.confidence)}</td>
    `;
    body.appendChild(tr);
  });
}

function renderTableMessage(tableId, colspan, message) {
  $(tableId).innerHTML = `<tr><td colspan="${colspan}" class="empty-cell">${message}</td></tr>`;
}

function formatConfidence(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${Math.round(n * 100)}%` : '—';
}

function colorClass(color) {
  if (color === 'GREEN') return 'green-text';
  if (color === 'RED') return 'red-text';
  if (color === 'YELLOW') return 'yellow-text';
  return '';
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[char]));
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

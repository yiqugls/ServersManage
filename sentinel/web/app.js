'use strict';

/* ============================================================ 基础工具 */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  servers: [],
  settings: null,
  presets: {},
  status: null,
  version: '',
  route: { name: 'overview', id: null, hours: 6 },
  timer: null,
  charts: [],
};

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toFixed(digits) + '%';
}

function fmtGB(bytes, digits = 1) {
  if (!bytes && bytes !== 0) return '—';
  const gb = bytes / 1024 ** 3;
  if (gb >= 1024) return (gb / 1024).toFixed(2) + ' TB';
  return gb.toFixed(digits) + ' GB';
}

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}

function pad2(n) { return String(n).padStart(2, '0'); }

function fmtTime(ts, withDate = false) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const hm = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  if (!withDate) return hm;
  return `${d.getMonth() + 1}-${pad2(d.getDate())} ${hm}`;
}

function fmtFull(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function fmtDur(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分`;
  return `${m} 分钟`;
}

function fmtHours(hours) {
  if (hours === null || hours === undefined) return '—';
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} 分钟`;
  if (hours < 48) return `${hours.toFixed(1)} 小时`;
  return `${(hours / 24).toFixed(1)} 天`;
}

function relTime(ts) {
  if (!ts) return '—';
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 5) return '刚刚';
  if (diff < 60) return `${diff} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

function barClass(pct) {
  if (pct === null || pct === undefined) return '';
  if (pct >= 90) return 'crit';
  if (pct >= 75) return 'warn';
  return '';
}

function rangeLabel(hours) {
  const map = { 1: '1 小时', 6: '6 小时', 24: '24 小时', 168: '7 天' };
  return map[Number(hours)] || `${hours} 小时`;
}

function usageBar(pct) {
  const width = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="bar ${barClass(pct)}"><i style="width:${width}%"></i></div>`;
}

function sevLabel(sev) {
  return { critical: '严重', high: '高', warning: '警告', info: '提示', resolved: '恢复' }[sev] || sev;
}
function sevChip(sev) {
  const cls = { critical: 'crit', high: 'warn', warning: 'warn', info: 'info', resolved: 'ok' }[sev] || 'muted';
  return `<span class="chip ${cls}">${esc(sevLabel(sev))}</span>`;
}

async function api(path, options = {}) {
  const init = { method: options.method || 'GET', headers: {} };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function toast(title, detail = '', type = '') {
  const root = $('#toastRoot');
  const node = document.createElement('div');
  node.className = 'toast ' + type;
  node.innerHTML = `<div class="t">${esc(title)}</div>${detail ? `<div class="d">${esc(detail)}</div>` : ''}`;
  root.appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .2s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 200);
  }, type === 'err' ? 7000 : 3800);
}

/* ============================================================ 图表 */
const CHART_COLORS = ['#4f8cff', '#a78bfa', '#22d3ee', '#f472b6', '#f59e0b', '#34d399'];

function chartDomain(series) {
  let t0 = Infinity, t1 = -Infinity;
  series.forEach(s => (s.data || []).forEach(p => {
    if (p.t < t0) t0 = p.t;
    if (p.t > t1) t1 = p.t;
  }));
  if (!isFinite(t0)) return null;
  if (t0 === t1) { t0 -= 60; t1 += 60; }
  return [t0, t1];
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawChart(canvas, cfg, hover) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width) return;
  const w = Math.round(rect.width * dpr), h = Math.round(rect.height * dpr);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const pad = { l: 40, r: 14, t: 14, b: 24 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const series = (cfg.series || []).filter(s => (s.data || []).length);
  const domain = chartDomain(series);
  ctx.font = '10px ui-monospace, Consolas, monospace';

  if (!domain) {
    ctx.fillStyle = '#4b5263';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = '12px -apple-system, "Microsoft YaHei", sans-serif';
    ctx.fillText('暂无数据', W / 2, H / 2);
    return;
  }
  const [t0, t1] = domain;
  const yMin = cfg.yMin ?? 0, yMax = cfg.yMax ?? 100;
  const X = t => pad.l + ((t - t0) / (t1 - t0)) * iw;
  const Y = v => pad.t + ih - ((Math.min(yMax, Math.max(yMin, v)) - yMin) / (yMax - yMin)) * ih;

  // 网格与 Y 轴
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yMax - yMin) * i / 4;
    const y = Y(v);
    ctx.strokeStyle = i === 0 ? 'rgba(255,255,255,.09)' : 'rgba(255,255,255,.05)';
    ctx.beginPath(); ctx.moveTo(pad.l, y + .5); ctx.lineTo(W - pad.r, y + .5); ctx.stroke();
    ctx.fillStyle = '#5f6779';
    ctx.fillText(cfg.formatY ? cfg.formatY(v) : v.toFixed(0), pad.l - 8, y);
  }

  // X 轴刻度
  const span = t1 - t0;
  ctx.textBaseline = 'top';
  for (let i = 0; i <= 3; i++) {
    const t = t0 + span * i / 3;
    const x = X(t);
    let text;
    if (span <= 36 * 3600) {
      const d = new Date(t * 1000);
      text = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    } else {
      const d = new Date(t * 1000);
      text = `${d.getMonth() + 1}/${pad2(d.getDate())} ${pad2(d.getHours())}:00`;
    }
    ctx.fillStyle = '#5f6779';
    ctx.textAlign = i === 0 ? 'left' : (i === 3 ? 'right' : 'center');
    ctx.fillText(text, x, H - pad.b + 7);
  }

  // 数据线
  series.forEach((s, idx) => {
    const color = s.color || CHART_COLORS[idx % CHART_COLORS.length];
    const pts = s.data;
    if (s.area !== false && series.length <= 2) {
      const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ih);
      grad.addColorStop(0, hexAlpha(color, .22));
      grad.addColorStop(1, hexAlpha(color, 0));
      ctx.beginPath();
      ctx.moveTo(X(pts[0].t), Y(yMin));
      pts.forEach(p => ctx.lineTo(X(p.t), Y(p.v)));
      ctx.lineTo(X(pts[pts.length - 1].t), Y(yMin));
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    }
    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = X(p.t), y = Y(p.v);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
  });

  // 悬停
  if (hover && series.length) {
    const p0 = series[0].data[hover.index];
    if (p0) {
      const x = X(p0.t);
      ctx.strokeStyle = 'rgba(255,255,255,.22)';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ih); ctx.stroke();
      ctx.setLineDash([]);

      const rows = [];
      series.forEach((s, idx) => {
        const p = s.data[hover.index];
        if (!p) return;
        const color = s.color || CHART_COLORS[idx % CHART_COLORS.length];
        ctx.beginPath();
        ctx.arc(X(p.t), Y(p.v), 3, 0, Math.PI * 2);
        ctx.fillStyle = color; ctx.fill();
        ctx.strokeStyle = '#0b0e14'; ctx.lineWidth = 1.5; ctx.stroke();
        rows.push({ color, name: s.name, value: (cfg.formatY ? cfg.formatY(p.v, true) : p.v.toFixed(1)) });
      });

      const head = fmtFull(p0.t);
      ctx.font = '11px ui-monospace, Consolas, monospace';
      const lineH = 15;
      const boxW = Math.max(
        ctx.measureText(head).width,
        ...rows.map(r => ctx.measureText(r.name + '  ' + r.value).width)
      ) + 22;
      const boxH = 12 + rows.length * lineH + 6;
      let bx = x + 12;
      if (bx + boxW > W - 6) bx = x - boxW - 12;
      const by = Math.min(Math.max(pad.t + 4, 8), H - boxH - 6);

      ctx.fillStyle = 'rgba(17,22,31,.96)';
      ctx.strokeStyle = 'rgba(255,255,255,.1)';
      roundRect(ctx, bx, by, boxW, boxH, 7);
      ctx.fill(); ctx.stroke();

      ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      ctx.fillStyle = '#8b93a3';
      ctx.fillText(head, bx + 11, by + 7);
      rows.forEach((r, i) => {
        const y = by + 8 + (i + 1) * lineH;
        ctx.fillStyle = r.color;
        ctx.beginPath(); ctx.arc(bx + 14, y + 5, 3, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = '#8b93a3';
        ctx.fillText(r.name, bx + 22, y);
        ctx.fillStyle = '#e7eaf0';
        ctx.fillText(r.value, bx + 22 + 62, y);
      });
    }
  }
}

function hexAlpha(hex, alpha) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return `rgba(79,140,255,${alpha})`;
  return `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${alpha})`;
}

function mountChart(canvas, cfg) {
  if (!canvas) return;
  const hover = { index: -1 };
  const redraw = () => drawChart(canvas, cfg, hover.index >= 0 ? hover : null);

  const onMove = ev => {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const mx = ev.clientX - rect.left;
    const series = cfg.series.filter(s => (s.data || []).length);
    const domain = chartDomain(series);
    if (!domain) return;
    const padL = 40, padR = 14;
    const iw = rect.width - padL - padR;
    const t = domain[0] + ((mx - padL) / Math.max(1, iw)) * (domain[1] - domain[0]);
    let best = -1, bestDist = Infinity;
    series[0].data.forEach((p, i) => {
      const d = Math.abs(p.t - t);
      if (d < bestDist) { bestDist = d; best = i; }
    });
    hover.index = best;
    redraw();
  };
  canvas.addEventListener('mousemove', onMove);
  canvas.addEventListener('mouseleave', () => { hover.index = -1; redraw(); });

  const ro = new ResizeObserver(() => redraw());
  ro.observe(canvas.parentElement || canvas);
  requestAnimationFrame(redraw);
  state.charts.push({ canvas, redraw, cfg, ro });
}

function disposeCharts() {
  state.charts.forEach(c => { try { c.ro && c.ro.disconnect(); } catch (e) {} });
  state.charts = [];
}

/* ============================================================ 布局渲染 */
function updateNav() {
  const name = state.route.name === 'server' ? 'overview' : state.route.name;
  $$('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.nav === name));
  const pending = state.servers.reduce((acc, s) => acc + (s.open_alerts || 0), 0);
  const badge = $('#navAlertCount');
  if (pending > 0) { badge.hidden = false; badge.textContent = pending > 99 ? '99+' : pending; }
  else { badge.hidden = true; }
}

function statusOf(entry) {
  if (entry.open_alerts > 0) return { cls: 'crit', text: '告警中', dot: 'crit' };
  if (entry.ok === false) return { cls: 'crit', text: '失联', dot: 'crit' };
  if (entry.ok === null || entry.ok === undefined) return { cls: 'muted', text: '等待采集', dot: '' };
  return { cls: 'ok', text: '正常', dot: 'ok' };
}

function worstDisk(latest) {
  const disks = (latest && latest.disks) || [];
  if (!disks.length) return null;
  return disks.reduce((a, b) => ((b.pct || 0) > (a.pct || 0) ? b : a), disks[0]);
}

function renderSidebar() {
  const list = $('#serverList');
  $('#serverCount').textContent = state.servers.length;
  if (!state.servers.length) {
    list.innerHTML = '<div class="dim" style="padding:10px;font-size:12px;">还没有服务器</div>';
    return;
  }
  list.innerHTML = state.servers.map(entry => {
    const s = entry.server;
    const st = statusOf(entry);
    const disk = worstDisk(entry.latest);
    const active = state.route.name === 'server' && Number(state.route.id) === Number(s.id);
    return `<div class="server-item ${active ? 'active' : ''}" data-act="open" data-id="${s.id}">
      <span class="dot ${st.dot}"></span>
      <div class="server-meta">
        <div class="server-name">${esc(s.name)}</div>
        <div class="server-host">${esc(s.host)}</div>
      </div>
      <span class="mini-pct">${disk ? Math.round(disk.pct) + '%' : '—'}</span>
    </div>`;
  }).join('');
}

function renderFoot() {
  const st = state.status || {};
  const dotCls = st.last_tick && (Date.now() / 1000 - st.last_tick) < 30 ? 'ok' : 'warn';
  $('#sideStatus').innerHTML = `<span class="dot ${dotCls}"></span>调度运行中 · ${st.stats ? st.stats.servers : 0} 台`;
  $('#footVersion').textContent = 'v' + (state.version || '1.0.0');
  $('#footInterval').textContent = state.settings ? `采集 ${state.settings.collect.interval_sec}s` : '—';
}

function setPage(title, sub, actions = '') {
  $('#pageTitle').textContent = title;
  $('#pageSub').textContent = sub || '';
  $('#topActions').innerHTML = actions;
}

/* ============================================================ 总览 */
async function renderOverview() {
  try {
    const data = await api('/api/overview');
    state.servers = data.servers;
    state.status = data.status;
  } catch (e) {
    toast('加载失败', e.message, 'err');
  }
  renderSidebar();
  renderFoot();
  updateNav();

  const servers = state.servers;
  const total = servers.length;
  const alerts = servers.reduce((a, s) => a + (s.open_alerts || 0), 0);
  const offline = servers.filter(s => s.ok === false).length;
  const latestTs = Math.max(0, ...servers.map(s => s.collected_at || 0));

  setPage('总览', total ? `${total} 台服务器 · 最近采集 ${relTime(latestTs)}` : '还没有配置服务器',
    `<button class="btn" data-act="refresh">刷新</button>
     <button class="btn" data-act="collect-all">全部采集</button>
     <button class="btn primary" data-act="add-server">添加服务器</button>`);

  const summary = `<div class="summary">
    <div class="item"><div class="k">服务器</div><div class="v">${total}<small>台</small></div></div>
    <div class="item"><div class="k">未恢复告警</div><div class="v ${alerts ? 'crit' : 'ok'}">${alerts}<small>条</small></div></div>
    <div class="item"><div class="k">采集异常</div><div class="v ${offline ? 'warn' : ''}">${offline}<small>台</small></div></div>
    <div class="item"><div class="k">采集周期</div><div class="v">${state.settings ? state.settings.collect.interval_sec : '—'}<small>秒</small></div></div>
  </div>`;

  if (!servers.length) {
    $('#view').innerHTML = summary + `<div class="card"><div class="empty">
      <div class="icon">🛡</div><h3>还没有绑定服务器</h3>
      <p>填入 IP、SSH 用户名和密码即可开始巡检，无需在目标机器上安装任何东西。</p>
      <button class="btn primary" data-act="add-server">添加第一台服务器</button>
      <button class="btn" data-act="add-demo" style="margin-left:8px">添加演示数据</button>
    </div></div>`;
    return;
  }

  const cards = servers.map(entry => {
    const s = entry.server;
    const st = statusOf(entry);
    const latest = entry.latest || {};
    const disk = worstDisk(latest);
    const preds = (entry.predictions || []).filter(p => p.eta_hours !== null && p.eta_hours !== undefined);
    const urgent = preds.sort((a, b) => a.eta_hours - b.eta_hours)[0];
    const foot = urgent
      ? `<span class="forecast ${urgent.eta_hours <= 24 ? 'crit' : ''}">${esc(urgent.mount)} ${fmtHours(urgent.eta_hours)}后满</span>`
      : `<span class="muted">趋势平稳</span>`;
    const alertText = entry.open_alerts ? `<span style="color:var(--crit)">${entry.open_alerts} 条告警</span>` : `<span class="dim">无告警</span>`;
    return `<article class="card srv-card" data-act="open" data-id="${s.id}">
      <div class="srv-head">
        <div>
          <div class="srv-name">${esc(s.name)}${s.kind === 'demo' ? '<span class="srv-badge">演示</span>' : ''}
            ${s.enabled ? '' : '<span class="srv-badge">已暂停</span>'}</div>
          <div class="srv-host">${esc(s.host)}:${s.port}</div>
        </div>
        <span class="chip ${st.cls}">${st.text}</span>
      </div>
      <div class="metric-row">
        <div class="metric-top"><span class="metric-label">CPU</span><span class="metric-value">${fmtPct(latest.cpu_pct)}</span></div>
        ${usageBar(latest.cpu_pct)}
      </div>
      <div class="metric-row">
        <div class="metric-top"><span class="metric-label">内存</span><span class="metric-value">${fmtPct(latest.mem_pct)}</span></div>
        ${usageBar(latest.mem_pct)}
      </div>
      <div class="metric-row">
        <div class="metric-top"><span class="metric-label">磁盘 ${disk ? esc(disk.mount) : ''}</span><span class="metric-value">${disk ? fmtPct(disk.pct) : '—'}</span></div>
        ${usageBar(disk ? disk.pct : 0)}
      </div>
      <div class="srv-foot">
        ${foot}
        <span>${alertText} · <span class="dim">${entry.collected_at ? relTime(entry.collected_at) : '未采集'}</span></span>
      </div>
    </article>`;
  }).join('');

  $('#view').innerHTML = summary + `<div class="grid cards">${cards}</div>`;
}

/* ============================================================ 详情 */
async function renderServer(id) {
  const hours = Number(state.route.hours) || 6;
  let data;
  try {
    data = await api(`/api/servers/${id}?hours=${hours}`);
  } catch (e) {
    setPage('出错了', e.message);
    $('#view').innerHTML = `<div class="card"><div class="empty"><div class="icon">!</div>
      <h3>无法加载服务器</h3><p>${esc(e.message)}</p>
      <button class="btn" data-act="back">返回总览</button></div></div>`;
    return;
  }
  const s = data.server;
  const latest = data.latest || {};
  const preds = data.predictions || [];
  const disk = worstDisk(latest);
  const hostInfo = latest.host_info || {};
  const entry = state.servers.find(x => Number(x.server.id) === Number(s.id)) || { open_alerts: 0, ok: true };
  const st = statusOf({ ...entry, ok: latest.ts ? true : entry.ok });

  setPage(s.name, `${s.host}:${s.port} · ${s.username}`,
    `<button class="btn" data-act="back">返回</button>
     <button class="btn" data-act="collect" data-id="${s.id}">立即采集</button>
     ${s.kind === 'demo' ? '' : `<button class="btn" data-act="test" data-id="${s.id}">测试连接</button>`}
     <button class="btn" data-act="edit" data-id="${s.id}">编辑</button>
     <button class="btn danger" data-act="del" data-id="${s.id}">删除</button>`);

  const ranges = [[1, '1 小时'], [6, '6 小时'], [24, '24 小时'], [168, '7 天']]
    .map(([h, label]) => `<button class="${Number(hours) === h ? 'active' : ''}" data-act="range" data-hours="${h}">${label}</button>`)
    .join('');

  const stats = `<div class="stats">
    <div class="stat ${(latest.cpu_pct || 0) >= 85 ? 'crit' : ''}">
      <div class="k">CPU 使用率</div><div class="v">${fmtPct(latest.cpu_pct)}</div>
      <div class="sub">${latest.cpu_cores ? latest.cpu_cores + ' 核' : '—'} · 负载 ${fmtNum(latest.load1, 2)}</div>
    </div>
    <div class="stat ${(latest.mem_pct || 0) >= 90 ? 'crit' : ''}">
      <div class="k">内存</div><div class="v">${fmtPct(latest.mem_pct)}</div>
      <div class="sub">${fmtGB(latest.mem_used)} / ${fmtGB(latest.mem_total)}</div>
    </div>
    <div class="stat ${disk && disk.pct >= 90 ? 'crit' : disk && disk.pct >= 75 ? 'warn' : ''}">
      <div class="k">磁盘 ${disk ? esc(disk.mount) : ''}</div><div class="v">${disk ? fmtPct(disk.pct) : '—'}</div>
      <div class="sub">${disk ? fmtGB(disk.used) + ' / ' + fmtGB(disk.total) : '—'}</div>
    </div>
    <div class="stat">
      <div class="k">运行时长</div><div class="v" style="font-size:20px">${fmtDur(latest.uptime_sec)}</div>
      <div class="sub">${esc(hostInfo.hostname || '—')}</div>
    </div>
  </div>`;

  const diskRows = ((latest.disks) || []).map(d => {
    const pred = preds.find(p => p.mount === d.mount) || {};
    const eta = pred.eta_hours;
    const trend = pred.slope_gb_day;
    return `<tr>
      <td class="mount">${esc(d.mount)}<span class="fs">${esc(d.fs || '')}</span></td>
      <td><div class="usage-cell">${usageBar(d.pct)}<span class="pct">${fmtPct(d.pct)}</span></div></td>
      <td class="num">${fmtGB(d.used)} / ${fmtGB(d.total)}</td>
      <td class="num">${fmtGB(d.avail)}</td>
      <td class="num">${d.inode_pct === null || d.inode_pct === undefined ? '—' : fmtPct(d.inode_pct)}</td>
      <td class="num">${trend === undefined || trend === null ? '—' : (trend > 0 ? '+' : '') + Number(trend).toFixed(2) + ' GB/天'}</td>
      <td class="num" style="color:${eta !== null && eta !== undefined && eta <= 24 ? 'var(--crit)' : 'inherit'}">${eta === null || eta === undefined ? '—' : fmtHours(eta)}</td>
    </tr>`;
  }).join('');

  const alerts = data.alerts || [];
  const alertHtml = alerts.length ? `<div class="alert-list">${alerts.map(a => `
    <div class="alert-item">
      <span class="alert-dot ${a.severity}"></span>
      <div class="alert-body">
        <div class="alert-title">${esc(a.title)}</div>
        <div class="alert-detail">${esc(a.detail)}</div>
        <div class="alert-meta"><span>${fmtFull(a.ts)}</span><span>${a.notified ? '邮件已发送' : (a.notify_error || '未发送邮件')}</span></div>
        ${a.llm && a.llm.summary ? `<div class="alert-llm"><b>AI</b> ${esc(a.llm.summary)}${a.llm.likely_cause ? `<div class="dim" style="margin-top:4px">可能原因：${esc(a.llm.likely_cause)}</div>` : ''}${(a.llm.actions || []).length ? `<ul>${a.llm.actions.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>` : ''}
      </div>
    </div>`).join('')}</div>` : `<div class="empty" style="padding:28px"><p>最近没有告警</p></div>`;

  $('#view').innerHTML = `
  <div class="detail-meta" style="margin-bottom:14px">
    <span class="chip ${st.cls}">${st.text}</span>
    ${s.kind === 'demo' ? '<span class="srv-badge">演示数据</span>' : ''}
    <span>${esc(hostInfo.kernel || '—')}</span>
    <span>采集耗时 ${latest.latency_ms ? latest.latency_ms + ' ms' : '—'}</span>
    <span>下次采集 ${data.next_run ? fmtTime(data.next_run) : '—'}</span>
    ${s.note ? `<span>${esc(s.note)}</span>` : ''}
  </div>
  ${stats}
  <div class="grid" style="grid-template-columns:1fr;gap:14px">
    <div class="card">
      <div class="card-head">
        <div><div class="card-title">CPU 与内存</div><div class="card-sub">最近 ${rangeLabel(hours)}</div></div>
        <div class="tabs">${ranges}</div>
      </div>
      <div class="chart-wrap"><canvas class="chart-canvas" id="chart-main"></canvas></div>
      <div class="legend">
        <span class="item"><span class="swatch" style="background:#4f8cff"></span>CPU <span class="val">${fmtPct(latest.cpu_pct)}</span></span>
        <span class="item"><span class="swatch" style="background:#a78bfa"></span>内存 <span class="val">${fmtPct(latest.mem_pct)}</span></span>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><div><div class="card-title">磁盘使用率</div><div class="card-sub">按挂载点</div></div></div>
      <div class="chart-wrap"><canvas class="chart-canvas" id="chart-disk"></canvas></div>
      <div class="legend" id="diskLegend"></div>
    </div>
    <div class="card pad-0">
      <div style="padding:16px 18px 6px"><div class="card-title">磁盘明细与容量预测</div>
        <div class="card-sub">预测基于最近 12 小时的最小二乘拟合，仅供参考</div></div>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>挂载点</th><th>使用率</th><th class="num">已用 / 总量</th><th class="num">可用</th><th class="num">inode</th><th class="num">增长</th><th class="num">预计写满</th></tr></thead>
        <tbody>${diskRows || '<tr><td colspan="7" class="dim" style="padding:24px;text-align:center">暂无磁盘数据</td></tr>'}</tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="card-head"><div class="card-title">最近告警</div>
        <a class="btn sm ghost" href="#/alerts">查看全部</a></div>
      ${alertHtml}
    </div>
  </div>`;

  const series = data.series || { points: [], disks: [] };
  mountChart($('#chart-main'), {
    series: [
      { name: 'CPU', color: '#4f8cff', data: series.points.filter(p => p.cpu !== null).map(p => ({ t: p.t, v: p.cpu })) },
      { name: '内存', color: '#a78bfa', area: false, data: series.points.filter(p => p.mem !== null).map(p => ({ t: p.t, v: p.mem })) },
    ],
    yMin: 0, yMax: 100,
    formatY: (v, withUnit) => withUnit ? v.toFixed(1) + '%' : Math.round(v) + '',
  });

  const diskSeries = (series.disks || []).map((d, i) => ({
    name: d.mount, color: CHART_COLORS[i % CHART_COLORS.length], area: false,
    data: d.points.map(p => ({ t: p.t, v: p.v })),
  }));
  mountChart($('#chart-disk'), {
    series: diskSeries, yMin: 0, yMax: 100,
    formatY: (v, withUnit) => withUnit ? v.toFixed(1) + '%' : Math.round(v) + '',
  });
  $('#diskLegend').innerHTML = diskSeries.map(d => {
    const last = d.data.length ? d.data[d.data.length - 1].v : null;
    return `<span class="item"><span class="swatch" style="background:${d.color}"></span>${esc(d.name)} <span class="val">${last === null ? '—' : last.toFixed(1) + '%'}</span></span>`;
  }).join('') || '<span class="dim" style="font-size:11.5px">暂无数据</span>';
}

/* ============================================================ 告警中心 */
async function renderAlerts() {
  const serverId = state.alertFilter || '';
  let alerts = [];
  try {
    const qs = serverId ? `?limit=200&server_id=${serverId}` : '?limit=200';
    alerts = (await api('/api/alerts' + qs)).alerts;
  } catch (e) {
    toast('加载告警失败', e.message, 'err');
  }
  setPage('告警中心', `最近 ${alerts.length} 条记录`, `<button class="btn" data-act="refresh">刷新</button>`);

  const options = ['<option value="">全部服务器</option>'].concat(
    state.servers.map(s => `<option value="${s.server.id}" ${String(serverId) === String(s.server.id) ? 'selected' : ''}>${esc(s.server.name)}</option>`)
  ).join('');

  const rows = alerts.map(a => {
    const llm = a.llm && a.llm.summary ? `<div class="alert-llm"><b>AI</b> ${esc(a.llm.summary)}
      ${a.llm.likely_cause ? `<div class="dim" style="margin-top:4px">可能原因：${esc(a.llm.likely_cause)}</div>` : ''}
      ${(a.llm.actions || []).length ? `<ul>${a.llm.actions.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>` : '';
    const notify = a.notified ? '<span class="chip ok">已发信</span>' : `<span class="chip muted">${esc(a.notify_error || '未发信')}</span>`;
    return `<div class="alert-item">
      <span class="alert-dot ${a.severity}"></span>
      <div class="alert-body">
        <div class="alert-title">${esc(a.title)} ${a.state === 'resolved' ? '<span class="chip ok">已恢复</span>' : sevChip(a.severity)}</div>
        <div class="alert-detail">${esc(a.detail)}</div>
        <div class="alert-meta">
          <span>${esc(a.server_name || '')}</span><span>${fmtFull(a.ts)}</span><span>${notify}</span>
        </div>
        ${llm}
      </div>
    </div>`;
  }).join('');

  const dg = state.settings && state.settings.digest;
  let cadenceText = '当前为即时模式：异常会立即发信（受冷却时间限制）';
  if (dg && dg.alerts_mode === 'summary') {
    const names = ['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    const day = names[Number(dg.weekday)] || '';
    cadenceText = `当前为汇总模式：异常只记录，${
      dg.frequency === 'weekly' ? '每' + day : '每天'
    } ${dg.time} 汇总发送`;
  }

  $('#view').innerHTML = `
    <div class="card" style="margin-bottom:14px;display:flex;align-items:center;gap:12px;justify-content:space-between;flex-wrap:wrap">
      <div class="pill-row">
        <span class="muted" style="font-size:12px">筛选</span>
        <select class="select" id="alertFilter" style="width:200px">${options}</select>
      </div>
      <div class="dim" style="font-size:11.5px">${esc(cadenceText)} · 记录保留 30 天</div>
    </div>
    <div class="card pad-0" style="padding:16px 18px">
      ${rows ? `<div class="alert-list">${rows}</div>` : `<div class="empty"><div class="icon">🔔</div><h3>暂无告警</h3><p>指标超过阈值、磁盘预测将满或服务器失联时会出现在这里</p></div>`}
    </div>`;

  const filter = $('#alertFilter');
  if (filter) filter.addEventListener('change', () => {
    state.alertFilter = filter.value;
    renderAlerts();
  });
}

/* ============================================================ 设置 */
function inputField(label, path, value, opts = {}) {
  const { type = 'number', step = '1', hint = '', full = false, mono = false, placeholder = '' } = opts;
  return `<div class="field ${full ? 'full' : ''}">
    <label>${esc(label)}</label>
    <input class="input ${mono ? 'mono' : ''}" type="${type}" data-path="${path}"
      value="${value === null || value === undefined ? '' : esc(value)}"
      ${step ? `step="${step}"` : ''} placeholder="${esc(placeholder)}">
    ${hint ? `<span class="hint">${hint}</span>` : ''}
  </div>`;
}

function selectField(label, path, value, options, hint = '', full = false) {
  const opts = options.map(([v, t]) => `<option value="${esc(v)}" ${String(value) === String(v) ? 'selected' : ''}>${esc(t)}</option>`).join('');
  return `<div class="field ${full ? 'full' : ''}">
    <label>${esc(label)}</label>
    <select class="select" data-path="${path}">${opts}</select>
    ${hint ? `<span class="hint">${hint}</span>` : ''}
  </div>`;
}

function switchRow(label, path, checked, desc = '', full = false) {
  return `<div class="switch-row ${full ? 'full' : ''}">
    <div><div style="font-size:12.5px;color:var(--text-2)">${esc(label)}</div>
      ${desc ? `<div class="desc">${esc(desc)}</div>` : ''}</div>
    <label class="switch"><input type="checkbox" data-path="${path}" ${checked ? 'checked' : ''}><span class="track"></span></label>
  </div>`;
}

function renderSettings() {
  const s = state.settings;
  if (!s) { $('#view').innerHTML = '<div class="card">配置加载失败，请刷新页面</div>'; return; }
  setPage('设置', '改动保存后立即生效，无需重启');
  const presets = state.presets || {};
  const providerOptions = [
    ['opencode-go', 'OpenCode Go（DeepSeek 套餐）'],
    ['deepseek', 'DeepSeek 官方 API'],
    ['custom', '自定义 OpenAI 兼容端点'],
  ];

  $('#view').innerHTML = `
  <form id="settingsForm" autocomplete="off">
  <div class="settings-grid">

    <div class="card">
      <div class="section-head"><h3>采集策略</h3><span class="dim" style="font-size:11px">单机可覆盖</span></div>
      <div class="form-grid">
        ${inputField('采集周期（秒）', 'collect.interval_sec', s.collect.interval_sec, { hint: '默认 60 秒；只看日报的话调到 300–3600 秒也够' })}
        ${inputField('并发采集数', 'collect.parallel', s.collect.parallel, { hint: '同时 SSH 的服务器数量' })}
        ${inputField('连接超时（秒）', 'collect.connect_timeout_sec', s.collect.connect_timeout_sec, {})}
        ${inputField('命令超时（秒）', 'collect.command_timeout_sec', s.collect.command_timeout_sec, {})}
        ${inputField('指标保留（天）', 'collect.retention_days', s.collect.retention_days, { hint: '超期指标每小时自动清理' })}
        ${selectField('主机指纹校验', 'collect.host_key_policy', s.collect.host_key_policy,
          [['auto', '自动接受（推荐）'], ['strict', '严格校验']],
          'strict 模式下新机器需要先手动 ssh 一次')}
      </div>
      <div class="pill-row" style="margin-top:10px">
        <span class="dim" style="font-size:11px">快捷设置</span>
        <button type="button" class="btn sm ghost" data-act="set-interval" data-value="60">60 秒</button>
        <button type="button" class="btn sm ghost" data-act="set-interval" data-value="300">5 分钟</button>
        <button type="button" class="btn sm ghost" data-act="set-interval" data-value="900">15 分钟</button>
        <button type="button" class="btn sm ghost" data-act="set-interval" data-value="3600">1 小时</button>
        <button type="button" class="btn sm ghost" data-act="set-interval" data-value="21600">6 小时</button>
        <button type="button" class="btn sm ghost" data-act="set-interval" data-value="86400">1 天</button>
      </div>
    </div>

    <div class="card">
      <div class="section-head"><h3>告警阈值</h3></div>
      ${switchRow('启用告警', 'alerts.enabled', s.alerts.enabled, '关闭后只采集不提醒')}
      <div class="form-grid" style="margin-top:12px">
        ${inputField('CPU 阈值（%）', 'alerts.cpu_pct', s.alerts.cpu_pct, { step: '1' })}
        ${inputField('CPU 连续周期', 'alerts.cpu_consecutive', s.alerts.cpu_consecutive, { hint: '连续几个周期超标才告警' })}
        ${inputField('内存阈值（%）', 'alerts.mem_pct', s.alerts.mem_pct, { step: '1' })}
        ${inputField('内存连续周期', 'alerts.mem_consecutive', s.alerts.mem_consecutive, {})}
        ${inputField('磁盘阈值（%）', 'alerts.disk_pct', s.alerts.disk_pct, { step: '1' })}
        ${inputField('预测时间窗（小时）', 'alerts.disk_forecast_hours', s.alerts.disk_forecast_hours, { step: '1', hint: '预测在该时间内写满就提前提醒' })}
        ${inputField('最小增长（GB/天）', 'alerts.disk_min_slope_gb_day', s.alerts.disk_min_slope_gb_day, { step: '0.1', hint: '低于此速度不预测，避免噪声' })}
        ${inputField('失联判定（次）', 'alerts.offline_after', s.alerts.offline_after, {})}
        ${inputField('告警冷却（分钟）', 'alerts.cooldown_minutes', s.alerts.cooldown_minutes, { hint: '同一问题在此期间不重复发信' })}
        ${inputField('每小时邮件上限', 'alerts.max_emails_per_hour', s.alerts.max_emails_per_hour, {})}
        ${switchRow('恢复时通知', 'alerts.notify_recovery', s.alerts.notify_recovery, '指标回落后发一封恢复邮件', true)}
      </div>
    </div>

    <div class="card">
      <div class="section-head"><h3>邮件通知</h3>
        <button type="button" class="btn sm" data-act="test-email">发送测试邮件</button></div>
      ${switchRow('启用邮件', 'email.enabled', s.email.enabled, 'SMTP 发送异常与预测提醒')}
      <div class="form-grid" style="margin-top:12px">
        ${inputField('SMTP 服务器', 'email.host', s.email.host, { type: 'text', step: '', mono: true, placeholder: 'smtp.qq.com' })}
        ${inputField('端口', 'email.port', s.email.port, {})}
        ${selectField('加密方式', 'email.security', s.email.security,
          [['ssl', 'SSL（465）'], ['starttls', 'STARTTLS（587）'], ['none', '不加密（25）']], '')}
        ${inputField('账号', 'email.username', s.email.username, { type: 'text', step: '', mono: true, placeholder: 'you@qq.com' })}
        ${inputField('密码 / 授权码', 'email.password', '', { type: 'password', step: '', hint: s.email.password_set ? '已配置，留空表示不修改' : 'QQ/163 请填授权码而不是登录密码' })}
        ${inputField('发件人地址', 'email.from_addr', s.email.from_addr, { type: 'text', step: '', mono: true })}
        ${inputField('发件人名称', 'email.from_name', s.email.from_name, { type: 'text', step: '' })}
        ${inputField('主题前缀', 'email.subject_prefix', s.email.subject_prefix, { type: 'text', step: '' })}
        <div class="field full">
          <label>收件人</label>
          <textarea class="textarea" data-path="email.to_addrs" data-type="list"
            placeholder="多个地址用逗号或换行分隔">${esc((s.email.to_addrs || []).join('\n'))}</textarea>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-head"><h3>AI 研判</h3>
        <button type="button" class="btn sm" data-act="test-llm">测试模型</button></div>
      ${switchRow('启用 AI 分析', 'llm.enabled', s.llm.enabled, '异常时让模型给出原因判断和处置建议')}
      <div class="form-grid" style="margin-top:12px">
        ${selectField('服务商', 'llm.provider', s.llm.provider, providerOptions, '', true)}
        ${inputField('Base URL', 'llm.base_url', s.llm.base_url, { type: 'text', step: '', mono: true, full: true })}
        ${inputField('模型名', 'llm.model', s.llm.model, { type: 'text', step: '', mono: true })}
        ${inputField('API Key', 'llm.api_key', '', { type: 'password', step: '', hint: s.llm.api_key_set ? '已配置，留空表示不修改' : 'OpenCode Go 控制台里复制' })}
        ${selectField('调用时机', 'llm.mode', s.llm.mode,
          [['on_alert', '仅异常时（省钱，推荐）'], ['off', '不调用']], '', true)}
        ${inputField('超时（秒）', 'llm.timeout_sec', s.llm.timeout_sec, {})}
        ${inputField('最大输出 token', 'llm.max_tokens', s.llm.max_tokens, { hint: '推理型模型会被思考过程吃掉预算，建议 1500 以上' })}
        ${inputField('温度', 'llm.temperature', s.llm.temperature, { step: '0.1' })}
        <div class="hint full" style="grid-column:1/-1;color:var(--dim);font-size:11px;line-height:1.6">
          模型只做“解释和分级”，是否发信仍由上面的规则决定，接口挂了也不会漏报警。
          OpenCode Go 是 coding 订阅，用它跑监控属于低频非典型流量，建议保持“仅异常时”调用。
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-head"><h3>汇总与提醒节奏</h3>
        <button type="button" class="btn sm" data-act="digest-send">立即发送</button></div>
      ${switchRow('启用定期汇总', 'digest.enabled', s.digest.enabled, '把告警和体检表合成一封，按天或按周发')}
      <div class="form-grid" style="margin-top:12px">
        ${selectField('频率', 'digest.frequency', s.digest.frequency,
          [['daily', '每天一封'], ['weekly', '每周一封']], '')}
        ${inputField('发送时间', 'digest.time', s.digest.time, { type: 'time', step: '', hint: '本机时区' })}
        ${selectField('星期几', 'digest.weekday', s.digest.weekday,
          [['1', '周一'], ['2', '周二'], ['3', '周三'], ['4', '周四'], ['5', '周五'], ['6', '周六'], ['7', '周日']],
          '仅「每周」模式生效', true)}
        ${selectField('告警投递方式', 'digest.alerts_mode', s.digest.alerts_mode,
          [['summary', '只进汇总（安静，推荐）'], ['immediate', '异常立即发信']],
          '', true)}
        <div class="hint full" style="grid-column:1/-1;color:var(--dim);font-size:11px;line-height:1.6">
          选「只进汇总」：异常只记到告警中心，不发即时邮件；到点后一封邮件列出未恢复和本期已恢复的告警，再附全量体检表。
          选「异常立即发信」：保留实时告警，汇总邮件只做体检。选「只进汇总」会自动打开上面的开关，避免漏收。
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-head"><h3>访问控制</h3></div>
      <div class="form-grid">
        ${inputField('登录用户名', 'web.basic_auth_user', s.web.basic_auth_user, { type: 'text', step: '', hint: '留空则不做校验（仅监听 127.0.0.1 时可留空）' })}
        ${inputField('登录密码', 'web.basic_auth_password', '', { type: 'password', step: '', hint: s.web.basic_auth_password_set ? '已配置，留空表示不修改' : '' })}
      </div>
      <div class="hint" style="margin-top:10px;color:var(--dim);font-size:11px;line-height:1.6">
        对公网开放时请务必设置用户名密码，或用 Nginx 反代加 HTTPS。
      </div>
    </div>

  </div>
  <div class="save-bar">
    <span class="tip">改动保存后立即生效；SSH 密码与 API Key 会加密存放在 data/secret.key 保护下。</span>
    <span class="pill-row">
      <button type="button" class="btn" data-act="reload-settings">还原</button>
      <button type="button" class="btn primary" data-act="save-settings">保存设置</button>
    </span>
  </div>
  </form>`;

  const form = $('#settingsForm');
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    await saveSettings();
  });
  const provider = $('[data-path="llm.provider"]', form);
  if (provider) provider.addEventListener('change', () => {
    const preset = presets[provider.value];
    if (!preset || provider.value === 'custom') return;
    const base = $('[data-path="llm.base_url"]', form);
    const model = $('[data-path="llm.model"]', form);
    if (base && preset.base_url) base.value = preset.base_url;
    if (model && preset.model) model.value = preset.model;
    toast('已填入预设', preset.note || '');
  });
}

function collectSettingsPayload() {
  const form = $('#settingsForm');
  if (!form) return null;
  const payload = {};
  $$('[data-path]', form).forEach(el => {
    const path = el.dataset.path;
    let value;
    if (el.type === 'checkbox') value = el.checked;
    else if (el.dataset.type === 'list') value = el.value.split(/[,;\s]+/).filter(Boolean);
    else if (el.type === 'number') {
      if (el.value === '') return;  // 留空的不提交，避免把默认值写回去
      value = Number(el.value);
    } else value = el.value;
    const parts = path.split('.');
    let node = payload;
    for (let i = 0; i < parts.length - 1; i++) {
      node[parts[i]] = node[parts[i]] || {};
      node = node[parts[i]];
    }
    node[parts[parts.length - 1]] = value;
  });
  return payload;
}

async function saveSettings(silent = false) {
  const payload = collectSettingsPayload();
  if (!payload) return false;
  try {
    const data = await api('/api/settings', { method: 'PUT', body: payload });
    state.settings = data.settings;
    if (!silent) toast('设置已保存', '采集周期等改动已生效', 'ok');
    renderFoot();
    return true;
  } catch (e) {
    toast('保存失败', e.message, 'err');
    return false;
  }
}

/* ============================================================ 弹窗 */
function closeModal() {
  $('#modalRoot').innerHTML = '';
  state.editingServer = null;
  if (state.route && state.route.name === 'new') {
    state.route = { name: 'overview' };
    location.hash = '#/';
  }
}

function openModal({ title, body, footer, width }) {
  $('#modalRoot').innerHTML = `<div class="modal-mask" data-act="close-modal">
    <div class="modal" style="${width ? 'width:' + width + 'px' : ''}" data-stop="1">
      <div class="modal-head"><h3>${esc(title)}</h3><button class="close" data-act="close-modal">×</button></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div>
  </div>`;
  const first = $('#modalRoot input, #modalRoot select, #modalRoot textarea');
  if (first) setTimeout(() => first.focus(), 30);
}

function openServerModal(server) {
  const isEdit = !!server;
  const s = server || { name: '', host: '', port: 22, username: 'root', note: '', enabled: true, interval_sec: null };
  openModal({
    title: isEdit ? '编辑服务器' : '添加服务器',
    body: `<form id="serverForm" autocomplete="off">
      <div class="form-grid">
        <div class="field"><label>名称</label>
          <input class="input" name="name" value="${esc(s.name)}" placeholder="web-01"></div>
        <div class="field"><label>端口</label>
          <input class="input" name="port" type="number" value="${esc(s.port)}"></div>
        <div class="field full"><label>IP / 主机名</label>
          <input class="input mono" name="host" value="${esc(s.host)}" placeholder="192.168.1.10"></div>
        <div class="field"><label>SSH 用户名</label>
          <input class="input mono" name="username" value="${esc(s.username)}"></div>
        <div class="field"><label>SSH 密码</label>
          <input class="input" name="password" type="password" placeholder="${isEdit ? '留空表示不修改' : '密码登录'}"></div>
        <div class="field"><label>采集间隔（秒，可选）</label>
          <input class="input" name="interval_sec" type="number" value="${s.interval_sec || ''}" placeholder="留空跟随全局"></div>
        <div class="field"><label>备注</label>
          <input class="input" name="note" value="${esc(s.note || '')}" placeholder="用途 / 机房 / 负责人"></div>
        <div class="field full">
          <label class="switch" style="margin-top:2px">
            <input type="checkbox" name="enabled" ${s.enabled ? 'checked' : ''}><span class="track"></span>
            <span class="label">启用这台服务器</span>
          </label>
        </div>
        <div class="full" id="probeResult"></div>
      </div>
    </form>`,
    footer: `<button class="btn" data-act="close-modal">取消</button>
      <button class="btn" data-act="server-test">测试连接</button>
      <button class="btn primary" data-act="server-save">${isEdit ? '保存' : '添加并绑定'}</button>`,
  });
  const form = $('#serverForm');
  if (form) form.addEventListener('submit', ev => { ev.preventDefault(); saveServerForm(); });
  state.editingServer = server || null;
}

async function testServerForm() {
  const form = $('#serverForm');
  const box = $('#probeResult');
  if (!form || !box) return;
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.host || !data.username) { toast('请填写 IP 和用户名', '', 'err'); return; }
  box.innerHTML = '<div class="hint" style="display:flex;gap:8px;align-items:center"><span class="spinner"></span>正在连接…</div>';
  try {
    const res = await api('/api/servers/probe', { method: 'POST', body: data });
    const p = res.probe;
    box.innerHTML = `<div class="alert-llm" style="border-color:rgba(47,191,113,.3);background:rgba(47,191,113,.07)">
      <b style="color:#7ee2ac">连接成功</b>
      <div style="margin-top:4px;color:var(--muted);font-size:12px">
        ${esc((p.host_info || {}).hostname || '')} · ${esc((p.host_info || {}).kernel || '')} ·
        CPU ${fmtPct(p.cpu_pct)} · 内存 ${fmtPct(p.mem_pct)} · ${p.disks.length} 个挂载点 · ${p.latency_ms} ms
      </div></div>`;
  } catch (e) {
    box.innerHTML = `<div class="alert-llm" style="border-color:rgba(239,83,80,.35);background:rgba(239,83,80,.07)">
      <b style="color:#ffa3a1">连接失败</b><div style="margin-top:4px;color:var(--muted);font-size:12px">${esc(e.message)}</div></div>`;
  }
}

async function saveServerForm() {
  const form = $('#serverForm');
  if (!form) return;
  const data = Object.fromEntries(new FormData(form).entries());
  data.enabled = form.enabled.checked;
  data.port = Number(data.port || 22);
  data.interval_sec = data.interval_sec ? Number(data.interval_sec) : null;
  if (state.editingServer) {
    if (!data.password) delete data.password;
    try {
      await api('/api/servers/' + state.editingServer.id, { method: 'PUT', body: data });
      toast('已保存', data.name || data.host, 'ok');
      closeModal();
      await refresh(true);
    } catch (e) { toast('保存失败', e.message, 'err'); }
  } else {
    try {
      const res = await api('/api/servers', { method: 'POST', body: data });
      toast('已添加', `${data.host} 将在下个周期开始采集`, 'ok');
      state.editingServer = null;
      closeModal();
      location.hash = `#/s/${res.id}`;
      await refresh(true);
    } catch (e) { toast('添加失败', e.message, 'err'); }
  }
}

function confirmDialog(title, text, onOk) {
  openModal({
    title,
    body: `<p style="margin:0;color:var(--text-2);font-size:13px;line-height:1.7">${esc(text)}</p>`,
    footer: `<button class="btn" data-act="close-modal">取消</button>
      <button class="btn danger" data-act="confirm-ok">确定</button>`,
  });
  state.confirmCallback = onOk;
}

/* ============================================================ 动作 */
async function doCollect(id) {
  toast('正在采集…', 'SSH 执行中');
  try {
    const res = await api(`/api/servers/${id}/collect`, { method: 'POST', body: {} });
    if (!res.ok) { toast('采集失败', res.error || '', 'err'); }
    else {
      const n = (res.findings || []).length;
      toast('采集完成', n ? `命中 ${n} 条规则` : '指标正常', 'ok');
    }
    await refresh(true);
  } catch (e) { toast('采集失败', e.message, 'err'); }
}

async function doCollectAll() {
  const ids = state.servers.map(s => s.server.id);
  toast('开始采集', `${ids.length} 台服务器`);
  await Promise.all(ids.map(id => api(`/api/servers/${id}/collect`, { method: 'POST', body: {} }).catch(() => null)));
  await refresh(true);
}

async function doTest(id) {
  toast('正在测试连接…');
  try {
    const res = await api(`/api/servers/${id}/test`, { method: 'POST', body: {} });
    const p = res.probe;
    toast('连接正常', `${(p.host_info || {}).hostname || ''} · ${p.latency_ms} ms`, 'ok');
  } catch (e) { toast('连接失败', e.message, 'err'); }
}

async function refresh(force = false) {
  if ($('#modalRoot').innerHTML && !force) return;
  const route = state.route;
  try {
    if (route.name === 'server') { disposeCharts(); await renderServer(route.id); }
    else if (route.name === 'alerts') await renderAlerts();
    else if (route.name === 'settings') { /* 不自动刷新表单 */ }
    else await renderOverview();
  } catch (e) {
    console.warn(e);
  }
}

document.addEventListener('click', async ev => {
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  const id = el.dataset.id;

  if (act === 'close-modal') {
    if (ev.target.closest('[data-stop]') && ev.target !== el && !el.classList.contains('close')) {
      return;
    }
    closeModal();
    return;
  }
  if (act === 'confirm-ok') {
    const cb = state.confirmCallback;
    closeModal();
    state.confirmCallback = null;
    if (cb) await cb();
    return;
  }

  switch (act) {
    case 'open':
      location.hash = `#/s/${id}`;
      break;
    case 'back':
      location.hash = '#/';
      break;
    case 'add-server':
      state.editingServer = null;
      openServerModal(null);
      break;
    case 'add-demo':
      await api('/api/demo', { method: 'POST', body: {} });
      await refresh(true);
      break;
    case 'server-save':
      saveServerForm();
      break;
    case 'server-test':
      testServerForm();
      break;
    case 'edit': {
      const entry = state.servers.find(x => Number(x.server.id) === Number(id));
      if (entry) { state.editingServer = entry.server; openServerModal(entry.server); }
      break;
    }
    case 'del':
      confirmDialog('删除服务器', '会同时删除这台服务器的历史指标和告警记录，无法恢复。', async () => {
        try {
          await api('/api/servers/' + id, { method: 'DELETE' });
          toast('已删除', '', 'ok');
          location.hash = '#/';
          await refresh(true);
        } catch (e) { toast('删除失败', e.message, 'err'); }
      });
      break;
    case 'collect':
      doCollect(id);
      break;
    case 'collect-all':
      doCollectAll();
      break;
    case 'test':
      doTest(id);
      break;
    case 'range': {
      state.route.hours = Number(el.dataset.hours);
      if (state.route.name === 'server') {
        location.hash = `#/s/${state.route.id}?h=${state.route.hours}`;
      }
      break;
    }
    case 'refresh':
      await refresh(true);
      toast('已刷新');
      break;
    case 'save-settings':
      await saveSettings();
      break;
    case 'reload-settings':
      renderSettings();
      break;
    case 'set-interval': {
      const input = $('[data-path="collect.interval_sec"]');
      if (input) input.value = el.dataset.value;
      break;
    }
    case 'test-email': {
      const ok = await saveSettings(true);
      if (!ok) break;
      toast('正在发送测试邮件…');
      try {
        const res = await api('/api/settings/test-email', { method: 'POST', body: {} });
        toast('发送成功', res.message || '', 'ok');
      } catch (e) { toast('发送失败', e.message, 'err'); }
      break;
    }
    case 'test-llm': {
      const ok = await saveSettings(true);
      if (!ok) break;
      toast('正在请求模型…');
      try {
        const res = await api('/api/settings/test-llm', { method: 'POST', body: {} });
        toast('模型可用', (res.result && (res.result.model + ' · ' + res.result.text)) || '', 'ok');
      } catch (e) { toast('模型调用失败', e.message, 'err'); }
      break;
    }
    case 'digest-send': {
      toast('正在生成汇总…');
      try {
        const res = await api('/api/digest/send', { method: 'POST', body: {} });
        if (res.error) toast('汇总已生成但未发出', res.error, 'err');
        else toast('汇总邮件已发送', `${res.rows} 台服务器`, 'ok');
      } catch (e) { toast('发送失败', e.message, 'err'); }
      break;
    }
    default:
      break;
  }
});

document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape') closeModal();
});

/* ============================================================ 路由 */
function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, qs] = raw.split('?');
  const params = new URLSearchParams(qs || '');
  const seg = path.split('/').filter(Boolean);
  if (seg[0] === 's' && seg[1]) {
    return { name: 'server', id: Number(seg[1]), hours: Number(params.get('h') || state.route.hours || 6) };
  }
  if (seg[0] === 'alerts') return { name: 'alerts' };
  if (seg[0] === 'settings') return { name: 'settings' };
  if (seg[0] === 'new') return { name: 'new' };
  return { name: 'overview' };
}

async function route() {
  disposeCharts();
  state.route = parseHash();
  updateNav();
  renderSidebar();
  renderFoot();
  if (state.route.name === 'overview') await renderOverview();
  else if (state.route.name === 'new') {
    await renderOverview();
    state.editingServer = null;
    openServerModal(null);
  }
  else if (state.route.name === 'server') await renderServer(state.route.id);
  else if (state.route.name === 'alerts') await renderAlerts();
  else if (state.route.name === 'settings') renderSettings();
  updateNav();
  renderFoot();
}

async function boot() {
  try {
    const data = await api('/api/bootstrap');
    state.servers = data.servers;
    state.settings = data.settings;
    state.presets = data.presets || {};
    state.status = data.status;
    state.version = data.version;
  } catch (e) {
    document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif;color:#ddd">
      <h2>无法连接后端</h2><p style="color:#999">${esc(e.message)}</p></div>`;
    return;
  }
  await route();
  window.addEventListener('hashchange', () => { route(); });
  state.timer = setInterval(() => {
    if (document.hidden) return;
    refresh(false);
  }, 15000);
  // 侧栏数据也定时同步
  setInterval(async () => {
    if (document.hidden) return;
    if (state.route.name === 'settings') return;
    try {
      const data = await api('/api/overview');
      state.servers = data.servers;
      state.status = data.status;
      renderSidebar();
      renderFoot();
      updateNav();
    } catch (e) { /* ignore */ }
  }, 30000);
}

boot();

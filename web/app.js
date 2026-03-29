/* ═══════════════════════════════════════════════════════════════
   SWARMCHAIN-SIM  ·  Frontend Application Logic
   WebSocket, Chart.js, SVG topology, Replay mode, Reasoning logs
   ═══════════════════════════════════════════════════════════════ */

// ── State ────────────────────────────────────────────────────
let socket = null;
let costChart = null;
let financialChart = null;
let forecastChart = null;
let prevState = null;
let replayMode = false;      // true = scrubbing historical ticks
let replayPlaying = false;   // true = auto-advancing replay
let replayInterval = null;

// ── Node display name mapping (DB id → friendly label) ──────────
// IB nodes: 1→IB1, 2→IB2, 6→IB3   |   FC nodes: 3→FC1, 4→FC2, 5→FC3, 7→FC4, 8→FC5, 9→FC6
const NODE_DISPLAY = {
  1: 'IB1', 2: 'IB2', 6: 'IB3',
  3: 'FC1', 4: 'FC2', 5: 'FC3',
  7: 'FC4', 8: 'FC5', 9: 'FC6',
};
function nodeLabel(id) { return NODE_DISPLAY[id] || ('Node' + id); }

const AGENT_COLORS = {
  Disruptor: { css: 'agent-disruptor', hex: '#ff5252' },
  Repair:    { css: 'agent-repair',    hex: '#69f0ae' },
  Planner:   { css: 'agent-planner',   hex: '#ffab40' },
  Demand:    { css: 'agent-demand',    hex: '#00e5ff' },
  Supply:    { css: 'agent-supply',    hex: '#448aff' },
  Forecast:  { css: 'agent-forecast',  hex: '#ff80ab' },
  Staffing:  { css: 'agent-staffing',  hex: '#b388ff' },
};

const TOPO = {
  width: 620, height: 430,
  nodes: {
    1: { x: 105, y:  85, label: 'IB1', type: 'ib' },
    2: { x: 105, y: 215, label: 'IB2', type: 'ib' },
    6: { x: 105, y: 345, label: 'IB3', type: 'ib' },
    3: { x: 470, y:  45, label: 'FC1', type: 'fc' },
    4: { x: 470, y: 115, label: 'FC2', type: 'fc' },
    5: { x: 470, y: 185, label: 'FC3', type: 'fc' },
    7: { x: 470, y: 255, label: 'FC4', type: 'fc' },
    8: { x: 470, y: 325, label: 'FC5', type: 'fc' },
    9: { x: 470, y: 395, label: 'FC6', type: 'fc' },
  },
  suppliers: [
    { x: 25, y:  85, label: 'Supplier → IB1', node: 1 },
    { x: 25, y: 215, label: 'Supplier → IB2', node: 2 },
    { x: 25, y: 345, label: 'Supplier → IB3', node: 6 },
  ],
  lanes: {
     1: { from: 1, to: 3 },
     2: { from: 1, to: 4 },
     3: { from: 2, to: 4 },
     4: { from: 2, to: 5 },
     5: { from: 1, to: 7 },
     6: { from: 2, to: 8 },
     7: { from: 6, to: 7 },
     8: { from: 6, to: 8 },
     9: { from: 6, to: 9 },
    10: { from: 2, to: 9 },
  }
};


// ═══════════════════════════════════════════════════════════════
// WebSocket Connection
// ═══════════════════════════════════════════════════════════════

function connectSocket() {
  socket = io();

  socket.on('connect', () => {
    document.getElementById('conn-dot').classList.remove('disconnected');
  });

  socket.on('disconnect', () => {
    document.getElementById('conn-dot').classList.add('disconnected');
  });

  socket.on('state_update', (data) => {
    // Only update if in live mode OR if we requested this tick
    if (!replayMode || data.is_live === false) {
      updateDashboard(data);
      prevState = data;
    }
    // Update replay slider range from live updates
    if (data.tick_range) {
      updateReplayRange(data.tick_range);
    }
  });

  socket.on('tick_range', (data) => {
    updateReplayRange(data);
  });
}


// ═══════════════════════════════════════════════════════════════
// Replay Mode
// ═══════════════════════════════════════════════════════════════

function initReplay() {
  const slider = document.getElementById('replay-slider');
  const prevBtn = document.getElementById('replay-prev');
  const nextBtn = document.getElementById('replay-next');
  const playBtn = document.getElementById('replay-play');
  const liveBtn = document.getElementById('replay-live');

  let lastScrubTime = 0;
  slider.addEventListener('input', () => {
    enterReplayMode();
    const now = Date.now();
    if (now - lastScrubTime > 50) {
      requestTickState(parseInt(slider.value));
      lastScrubTime = now;
    }
    updateReplayLabel();
  });
  
  slider.addEventListener('change', () => {
    // Ensure final state is fetched when mouse is released
    requestTickState(parseInt(slider.value));
  });

  prevBtn.addEventListener('click', () => {
    enterReplayMode();
    const val = Math.max(parseInt(slider.min), parseInt(slider.value) - 1);
    slider.value = val;
    requestTickState(val);
    updateReplayLabel();
  });

  nextBtn.addEventListener('click', () => {
    enterReplayMode();
    const val = Math.min(parseInt(slider.max), parseInt(slider.value) + 1);
    slider.value = val;
    requestTickState(val);
    updateReplayLabel();
    // If we reached the end, go back to live
    if (val >= parseInt(slider.max)) {
      exitReplayMode();
    }
  });

  playBtn.addEventListener('click', () => {
    if (replayPlaying) {
      stopReplayPlay();
    } else {
      startReplayPlay();
    }
  });

  liveBtn.addEventListener('click', () => {
    exitReplayMode();
  });
}

function enterReplayMode() {
  replayMode = true;
  document.getElementById('mode-badge').textContent = 'REPLAY';
  document.getElementById('mode-badge').classList.add('replay');
  document.getElementById('topo-badge').textContent = 'Replay';
}

function exitReplayMode() {
  replayMode = false;
  replayPlaying = false;
  if (replayInterval) clearInterval(replayInterval);
  document.getElementById('mode-badge').textContent = 'LIVE';
  document.getElementById('mode-badge').classList.remove('replay');
  document.getElementById('topo-badge').textContent = 'Live';
  document.getElementById('replay-play').textContent = '▶';
  const slider = document.getElementById('replay-slider');
  slider.value = slider.max;
  updateReplayLabel();
}

function startReplayPlay() {
  enterReplayMode();
  replayPlaying = true;
  document.getElementById('replay-play').textContent = '⏸';
  const slider = document.getElementById('replay-slider');
  replayInterval = setInterval(() => {
    const val = Math.min(parseInt(slider.max), parseInt(slider.value) + 1);
    slider.value = val;
    requestTickState(val);
    updateReplayLabel();
    if (val >= parseInt(slider.max)) {
      stopReplayPlay();
      exitReplayMode();
    }
  }, 1000);
}

function stopReplayPlay() {
  replayPlaying = false;
  if (replayInterval) clearInterval(replayInterval);
  document.getElementById('replay-play').textContent = '▶';
}

function requestTickState(tick) {
  if (socket) {
    socket.emit('request_tick_state', { tick: tick });
  }
}

function updateReplayRange(range) {
  const slider = document.getElementById('replay-slider');
  slider.min = range.min_tick || 0;
  slider.max = range.max_tick || 0;
  if (!replayMode) {
    slider.value = slider.max;
  }
  updateReplayLabel();
}

function updateReplayLabel() {
  const slider = document.getElementById('replay-slider');
  document.getElementById('replay-tick-label').textContent =
    `${slider.value} / ${slider.max}`;
}


// ═══════════════════════════════════════════════════════════════
// Master Update
// ═══════════════════════════════════════════════════════════════

function updateDashboard(state) {
  updateHeader(state);
  updateTopology(state);
  updateInventory(state);
  updateDemand(state);
  updateDisruptions(state);
  updateTokens(state);
  updateFinancials(state);
  updateForecastAccuracy(state);
  updateNetworkHealth(state);
  updateLog(state);
}


// ═══════════════════════════════════════════════════════════════
// Header
// ═══════════════════════════════════════════════════════════════

function updateHeader(state) {
  document.getElementById('tick-value').textContent = `Tick ${state.tick}`;
  document.getElementById('timestamp').textContent = state.timestamp || '';
}


// ═══════════════════════════════════════════════════════════════
// Network Topology
// ═══════════════════════════════════════════════════════════════

function updateTopology(state) {
  const svg = document.getElementById('topology-svg');
  if (!svg) return;

  const nodeMap = {};
  (state.inventory || []).forEach(n => { nodeMap[n.id] = n; });
  const laneDisruptions = {};
  (state.disruptions || []).forEach(d => {
    if (d.type === 'lane') laneDisruptions[d.target_id] = d.severity;
  });
  const laneInfo = {};
  (state.lanes || []).forEach(l => { laneInfo[l.id] = l; });
  const demandMap = {};
  (state.demand || []).forEach(r => { demandMap[r.node_id] = r; });
  const laneCapMap = state.lane_capacity || {};
  const demandAvg  = state.demand_avg   || {};

  let html = `<defs>
    <filter id="glow-healthy" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="4" result="blur"/>
      <feComposite in="SourceGraphic" in2="blur" operator="over"/>
    </filter>
    <filter id="glow-warn" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="4" result="blur"/>
      <feComposite in="SourceGraphic" in2="blur" operator="over"/>
    </filter>
    <filter id="glow-crit" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="6" result="blur"/>
      <feComposite in="SourceGraphic" in2="blur" operator="over"/>
    </filter>
    <linearGradient id="grad-ib" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#448aff" stop-opacity="0.3"/>
      <stop offset="100%" stop-color="#2962ff" stop-opacity="0.06"/>
    </linearGradient>
    <linearGradient id="grad-fc" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1de9b6" stop-opacity="0.25"/>
      <stop offset="100%" stop-color="#00bfa5" stop-opacity="0.05"/>
    </linearGradient>
  </defs>`;

  // Supplier arrows (minimal, left edge only)
  TOPO.suppliers.forEach(s => {
    const node = TOPO.nodes[s.node];
    html += `<line x1="${s.x + 8}" y1="${s.y}" x2="${node.x - 22}" y2="${node.y}"
                   stroke="rgba(99,115,175,0.3)" stroke-width="1.5" stroke-dasharray="3 5"/>
             <text x="${s.x}" y="${s.y - 5}" class="topo-supplier-label" fill="#3d4666">▶ Supplier</text>`;
  });

  // Lanes — label placed at 20% along path (near origin), preventing mid-canvas stacking
  Object.entries(TOPO.lanes).forEach(([laneId, lane]) => {
    const from = TOPO.nodes[lane.from];
    const to   = TOPO.nodes[lane.to];
    if (!from || !to) return;
    const sev = laneDisruptions[parseInt(laneId)] || 0;
    const isDisrupted = sev > 0;
    const info = laneInfo[parseInt(laneId)];
    const tc   = info ? info.transport_cost : '?';

    // Straight path (nodes are already well separated horizontally)
    const x1 = from.x + 22, y1 = from.y;
    const x2 = to.x - 22,   y2 = to.y;
    // Slight curve via midpoint offset
    const cmx = (x1 + x2) / 2;
    const cmy = (y1 + y2) / 2 - 10;
    const pathD = `M${x1},${y1} Q${cmx},${cmy} ${x2},${y2}`;

    const strokeColor = isDisrupted ? 'var(--red)' : 'rgba(68,138,255,0.55)';
    html += `<path d="${pathD}" fill="none" stroke="${isDisrupted ? 'rgba(255,82,82,0.2)' : 'rgba(99,115,175,0.08)'}" stroke-width="5" stroke-linecap="round"/>`;
    html += `<path d="${pathD}" class="topo-lane ${isDisrupted ? 'disrupted' : 'healthy'}" id="lane-path-${laneId}"
                   fill="none" stroke="${strokeColor}" stroke-width="1.5" stroke-linecap="round"/>`;

    // Label at t=0.18 (close to origin IB, so lanes from same IB stack near it)
    const t = 0.18;
    const lx = (1-t)*(1-t)*x1 + 2*(1-t)*t*cmx + t*t*x2;
    const ly = (1-t)*(1-t)*y1 + 2*(1-t)*t*cmy + t*t*y2;
    const sevTag = sev > 0 ? ` ⚠${Math.round(sev*100)}%` : '';
    html += `<rect x="${lx - 26}" y="${ly - 8}" width="52" height="11" rx="3"
                   fill="rgba(11,14,23,0.88)" stroke="rgba(99,115,175,0.18)" stroke-width="1"/>
             <text x="${lx}" y="${ly}" class="topo-lane-label">L${laneId} ${tc}${sevTag}</text>`;

    if (!isDisrupted) {
      html += `<circle r="3" fill="var(--cyan)">
                 <animateMotion dur="${2.8 + parseInt(laneId) * 0.35}s" repeatCount="indefinite">
                   <mpath href="#lane-path-${laneId}"/>
                 </animateMotion>
               </circle>`;
    }
  });

  // Nodes
  const R = 20; // node radius
  Object.entries(TOPO.nodes).forEach(([nodeId, pos]) => {
    const n     = nodeMap[parseInt(nodeId)];
    const isIB  = pos.type === 'ib';

    let fill, stroke, filter;
    if (!n || n.inventory <= 0) {
      fill = 'rgba(255,82,82,0.2)'; stroke = 'var(--red)'; filter = 'url(#glow-crit)';
    } else if (n.inventory < n.safety_stock) {
      fill = 'rgba(255,171,64,0.12)'; stroke = 'var(--yellow)'; filter = 'url(#glow-warn)';
    } else {
      fill = isIB ? 'url(#grad-ib)' : 'url(#grad-fc)';
      stroke = isIB ? 'var(--blue)' : 'var(--green)';
      filter = 'url(#glow-healthy)';
    }

    // Subtle pulse ring
    html += `<circle cx="${pos.x}" cy="${pos.y}" r="${R + 7}" fill="none" stroke="${stroke}"
                     stroke-width="1" opacity="0.18" filter="${filter}">
      <animate attributeName="r"       values="${R+7};${R+11};${R+7}" dur="3s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0.18;0.05;0.18"        dur="3s" repeatCount="indefinite"/>
    </circle>`;

    // Node circle + label
    html += `<circle cx="${pos.x}" cy="${pos.y}" r="${R}" fill="${fill}" stroke="${stroke}" stroke-width="2" filter="${filter}"/>`;
    html += `<text x="${pos.x}" y="${pos.y + 4}" class="topo-node-label">${pos.label}</text>`;

    if (isIB && n) {
      // IB: just show inventory % below label
      const invPct = Math.round((n.inventory / n.capacity) * 100);
      const iColor = n.inventory <= 0 ? 'var(--red)' : n.inventory < n.safety_stock ? 'var(--yellow)' : 'rgba(99,115,175,0.7)';
      html += `<text x="${pos.x}" y="${pos.y + 14}" class="topo-node-inv" fill="${iColor}">${invPct}%</text>`;
    }

    if (!isIB && n) {
      const dem = demandMap[parseInt(nodeId)];

      // ── Inventory % of max capacity ─────────────────────────
      const invPct   = Math.round((n.inventory / n.capacity) * 100);
      const invColor = n.inventory <= 0 ? 'var(--red)' : n.inventory < n.safety_stock ? 'var(--yellow)' : 'var(--green)';

      // ── Labor staffing % of max labor capacity ──────────────
      const laborPct   = dem ? Math.round((dem.labor_capacity / n.labor_capacity_base) * 100) : null;
      const laborColor = laborPct == null ? '#5c6380'
        : laborPct < 75  ? 'var(--red)'
        : laborPct < 95  ? 'var(--yellow)'
        : 'var(--green)';

      // ── Backlog days (backlog ÷ avg actual demand last 3t) ──
      let blDays = '0d', blColor = 'var(--text-dim)';
      if (dem && dem.backlog > 0) {
        const avgD = demandAvg[parseInt(nodeId)] || dem.orders || 1;
        const d = avgD > 0 ? (dem.backlog / avgD).toFixed(1) : '—';
        blDays  = `${d}d`;
        blColor = parseFloat(d) >= 2 ? 'var(--red)' : 'var(--yellow)';
      }

      // ── Variable cost per unit shipped (labor cost/unit) ───
      let vcpu = '—', vcColor = 'var(--text-dim)';
      if (dem && dem.shipped > 0 && n.units_per_hour > 0) {
        const laborCost = (dem.labor_capacity / n.units_per_hour) * n.hourly_labor_cost;
        vcpu    = `€${(laborCost / dem.shipped).toFixed(2)}`;
        vcColor = 'var(--purple)';
      }

      // ── Side panel (right of node) ─────────────────────────
      const PW = 82, PH = 58, PX = pos.x + R + 5, PY = pos.y - PH / 2;
      html += `<rect x="${PX}" y="${PY}" width="${PW}" height="${PH}" rx="4"
                     fill="rgba(11,14,23,0.82)" stroke="rgba(99,115,175,0.15)" stroke-width="1"/>`;

      // Row helper: key left, value right
      const row = (label, val, color, rowY) =>
        `<text x="${PX + 5}"        y="${rowY}" class="topo-panel-key">${label}</text>
         <text x="${PX + PW - 4}"   y="${rowY}" class="topo-panel-val" fill="${color}" text-anchor="end">${val}</text>`;

      html += row('INV',  `${invPct}%`,    invColor,   PY + 13);
      html += row('LAB',  laborPct != null ? `${laborPct}%` : '—', laborColor, PY + 26);
      html += row('BLG',  blDays,           blColor,    PY + 39);
      html += row('€/U',  vcpu,             vcColor,    PY + 52);
    }
  });

  svg.innerHTML = html;
}


// ═══════════════════════════════════════════════════════════════
// Inventory Health
// ═══════════════════════════════════════════════════════════════

function updateInventory(state) {
  const container = document.getElementById('inventory-list');
  if (!container) return;

  const nodes = state.inventory || [];
  const history = state.history || {};

  let html = '';
  nodes.forEach(n => {
    const pct = n.capacity > 0 ? (n.inventory / n.capacity) * 100 : 0;
    const safetyPct = n.capacity > 0 ? (n.safety_stock / n.capacity) * 100 : 0;
    let color = n.inventory <= 0 ? 'red' : n.inventory < n.safety_stock ? 'yellow' : 'green';
    const displayName = nodeLabel(n.id);
    const typeLabel = displayName.slice(0, 2);   // 'IB' or 'FC'
    const idPart    = displayName.slice(2);       // '1', '2', …

    html += `
      <div class="inv-node">
        <div class="inv-label">
          <div class="node-type">${typeLabel}</div>
          <div class="node-id">${idPart}</div>
        </div>
        <div class="inv-bar-wrap">
          <div class="inv-bar-track">
            <div class="inv-bar-fill ${color}" style="width: ${Math.min(100, pct)}%"></div>
            <div class="inv-bar-safety" style="left: ${safetyPct}%"></div>
          </div>
        </div>
        <div class="inv-stats">
          <div class="inv-pct val-${color}">${pct.toFixed(1)}%</div>
          <div class="inv-count">${n.inventory}/${n.capacity}</div>
        </div>
        <canvas class="inv-chart" id="inv-chart-${n.id}" width="90" height="32"></canvas>
      </div>`;
  });
  container.innerHTML = html;

  nodes.forEach(n => {
    const canvas = document.getElementById(`inv-chart-${n.id}`);
    if (!canvas) return;
    const vals = history[n.id] || [];
    renderSparkline(canvas, vals, n.capacity, n.safety_stock);
  });
}

function renderSparkline(canvas, values, max, safety) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2) return;

  const scaleMax = max || Math.max(...values) || 1;

  if (safety > 0) {
    const sy = h - (safety / scaleMax) * h;
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(255, 171, 64, 0.25)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.moveTo(0, sy);
    ctx.lineTo(w, sy);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  const step = w / (values.length - 1);
  ctx.beginPath();
  ctx.strokeStyle = '#00e5ff';
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  values.forEach((v, i) => {
    const x = i * step;
    const y = h - (v / scaleMax) * (h - 2);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const gradient = ctx.createLinearGradient(0, 0, 0, h);
  gradient.addColorStop(0, 'rgba(0, 229, 255, 0.15)');
  gradient.addColorStop(1, 'rgba(0, 229, 255, 0)');
  ctx.lineTo((values.length - 1) * step, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();
}


// ═══════════════════════════════════════════════════════════════
// Demand Table
// ═══════════════════════════════════════════════════════════════

function updateDemand(state) {
  const tbody = document.getElementById('demand-tbody');
  if (!tbody) return;

  const rows = state.demand || [];
  let html = '';
  rows.forEach(r => {
    const maeClass = r.mae == null ? 'val-dim' : r.mae < 10 ? 'val-green' : r.mae < 25 ? 'val-yellow' : 'val-red';
    const maeVal   = r.mae != null ? r.mae.toFixed(1) : '--';
    const fcstVal  = r.forecast_demand != null ? Math.round(r.forecast_demand) : '--';
    const fcstClass = (r.method === 'fallback' || r.method === 'last_value') ? 'val-dim' : 'val-white';
    const blClass  = r.backlog > 0 ? 'val-red' : 'val-green';
    const laborPct = r.labor_capacity_base > 0 ? r.labor_capacity / r.labor_capacity_base : 1;
    const laborClass = laborPct < 0.5 ? 'val-red' : laborPct < 0.85 ? 'val-yellow' : 'val-white';

    const laborPctVal = (laborPct * 100).toFixed(0);
    html += `<tr>
      <td>${nodeLabel(r.node_id)}</td>
      <td class="val-white">${r.orders}</td>
      <td class="${fcstClass}">${fcstVal}</td>
      <td class="${maeClass}">${maeVal}</td>
      <td class="${laborClass}">${laborPctVal}% <span class="val-dim">(${r.labor_capacity}/${r.labor_capacity_base})</span></td>
      <td class="val-cyan">${r.shipped}</td>
      <td class="${blClass}">${r.backlog}</td>
    </tr>`;
  });
  tbody.innerHTML = html;
}


// ═══════════════════════════════════════════════════════════════
// Disruptions
// ═══════════════════════════════════════════════════════════════

function updateDisruptions(state) {
  const container = document.getElementById('disruption-list');
  const countBadge = document.getElementById('disruption-count');
  if (!container) return;

  const items = state.disruptions || [];
  if (countBadge) countBadge.textContent = items.length;

  if (items.length === 0) {
    container.innerHTML = '<div class="no-disruptions">No active disruptions — network is healthy ✓</div>';
    return;
  }

  let html = '';
  items.forEach(d => {
    const typeClass = d.type === 'lane' ? 'lane-type' : d.type === 'ib_node' ? 'ib-type' : 'fc-type';
    const sevClass  = d.severity >= 0.6 ? 'sev-high' : d.severity >= 0.3 ? 'sev-medium' : 'sev-low';
    let targetLabel = d.type === 'lane' ? `Lane ${d.target_id}` : nodeLabel(d.target_id);

    html += `
      <div class="disruption-item ${typeClass}">
        <div class="disruption-main">
          <span class="disruption-target">${targetLabel}</span>
          <span class="disruption-severity ${sevClass}">${Math.round(d.severity * 100)}%</span>
          <span class="disruption-remaining">${Math.max(0, d.remaining)}t left</span>
          <button class="action-btn resolve-btn" onclick="removeDisruption(${d.id})" title="Manually resolve this disruption">Resolve</button>
        </div>
        <span class="disruption-desc">${d.description}</span>
      </div>`;
  });
  container.innerHTML = html;
}

function removeDisruption(id) {
  if (socket && !replayMode) {
    socket.emit('remove_disruption', { id: id });
  } else if (replayMode) {
    alert("Cannot modify state while in Replay Mode. Switch to Live first.");
  }
}


// ═══════════════════════════════════════════════════════════════
// Token Cost
// ═══════════════════════════════════════════════════════════════

function updateTokens(state) {
  const t = state.tokens || {};

  const tickCost = document.getElementById('tick-cost');
  const totalCost = document.getElementById('total-cost');
  if (tickCost) tickCost.textContent = `€${(t.tick_cost || 0).toFixed(5)}`;
  if (totalCost) totalCost.textContent = `€${(t.total_cost || 0).toFixed(4)}`;

  updateCostChart(t.cost_history || []);

  const tbody = document.getElementById('token-tbody');
  if (!tbody) return;
  const entries = t.breakdown || [];
  let html = '';
  entries.forEach(e => {
    const ac = AGENT_COLORS[e.agent_name] || { css: '', hex: '#ccc' };
    const modelShort = (e.model_name || '').replace('gemini-', 'g-').replace('claude-', 'c-');
    html += `<tr>
      <td class="val-dim">${e.tick}</td>
      <td class="${ac.css}">${e.agent_name}</td>
      <td class="val-dim" title="${e.model_name || ''}">${modelShort}</td>
      <td class="val-dim">${e.input_tokens}</td>
      <td class="val-dim">${e.output_tokens}</td>
      <td class="val-cyan">${e.cost_eur.toFixed(5)}</td>
    </tr>`;
  });
  tbody.innerHTML = html;
}

function updateCostChart(history) {
  const canvas = document.getElementById('cost-chart');
  if (!canvas || history.length === 0) return;

  if (costChart) {
    costChart.data.labels = history.map((_, i) => i + 1);
    costChart.data.datasets[0].data = history;
    costChart.update('none');
  } else {
    costChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: history.map((_, i) => i + 1),
        datasets: [{
          data: history,
          borderColor: '#00e5ff',
          backgroundColor: 'rgba(0, 229, 255, 0.08)',
          fill: true,
          tension: 0.4,
          pointRadius: 0,
          borderWidth: 2,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false },
          y: {
            display: true,
            grid: { color: 'rgba(99,115,175,0.08)' },
            ticks: {
              color: '#5c6380',
              font: { family: 'JetBrains Mono', size: 9 },
              callback: v => '€' + v.toFixed(4),
            }
          }
        },
        animation: { duration: 300 }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// Financials
// ═══════════════════════════════════════════════════════════════

function updateFinancials(state) {
  const fin = state.financials || { total_labor_cost: 0, total_transport_cost: 0, cost_per_unit_shipped: 0 };
  const history = state.financials_history || [];

  const badge = document.getElementById('fin-tick-badge');
  const totalCost = document.getElementById('fin-total-cost');
  const cpu = document.getElementById('fin-cpu');

  if (badge) badge.textContent = `Tick ${state.tick}`;
  if (totalCost) totalCost.textContent = `€${(fin.total_labor_cost + fin.total_transport_cost).toFixed(2)}`;
  if (cpu) cpu.textContent = `€${fin.cost_per_unit_shipped.toFixed(2)}`;

  updateFinancialChart(history);
}

function updateFinancialChart(history) {
  const canvas = document.getElementById('financial-chart');
  if (!canvas || history.length === 0) return;

  const labels = history.map(h => h.tick);
  const totalCosts = history.map(h => h.total_labor_cost + h.total_transport_cost);
  const cpus = history.map(h => h.cost_per_unit_shipped);

  if (financialChart) {
    financialChart.data.labels = labels;
    financialChart.data.datasets[0].data = totalCosts;
    financialChart.data.datasets[1].data = cpus;
    financialChart.update('none');
  } else {
    financialChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          {
            label: 'Total Cost (€)',
            data: totalCosts,
            borderColor: '#ffab40',
            backgroundColor: 'rgba(255, 171, 64, 0.08)',
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            borderWidth: 2,
            yAxisID: 'y'
          },
          {
            label: 'Cost/Unit (€)',
            data: cpus,
            borderColor: '#b388ff',
            backgroundColor: 'transparent',
            borderDash: [5, 5],
            fill: false,
            tension: 0.4,
            pointRadius: 0,
            borderWidth: 2,
            yAxisID: 'y1'
          }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: {
            display: true,
            position: 'top',
            labels: { color: '#8892a6', font: { family: 'JetBrains Mono', size: 10 } }
          }
        },
        scales: {
          x: {
            display: true,
            title: {
              display: true,
              text: 'Simulation Tick',
              color: '#5c6380',
              font: { family: 'JetBrains Mono', size: 9 }
            },
            ticks: { color: '#5c6380', font: { family: 'JetBrains Mono', size: 9 } },
            grid: { color: 'rgba(255,255,255,0.06)' }
          },
          y: {
            display: true,
            position: 'left',
            title: {
              display: true,
              text: 'Total Cost (€)',
              color: '#ffab40',
              font: { family: 'JetBrains Mono', size: 9 }
            },
            grid: { color: 'rgba(255,255,255,0.06)' },
            ticks: { color: '#8892a6', font: { family: 'JetBrains Mono', size: 9 }, callback: v => '€' + v }
          },
          y1: {
            display: true,
            position: 'right',
            title: {
              display: true,
              text: 'Cost / Unit (€)',
              color: '#b388ff',
              font: { family: 'JetBrains Mono', size: 9 }
            },
            grid: { drawOnChartArea: false },
            ticks: { color: '#8892a6', font: { family: 'JetBrains Mono', size: 9 }, callback: v => '€' + v }
          }
        },
        animation: { duration: 300 }
      },
      plugins: [{
        id: 'customBackground',
        beforeDraw(chart) {
          const ctx = chart.canvas.getContext('2d');
          ctx.save();
          ctx.globalCompositeOperation = 'destination-over';
          ctx.fillStyle = 'rgba(17,24,39,0.92)';
          ctx.fillRect(0, 0, chart.width, chart.height);
          ctx.restore();
        }
      }]
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// Forecast Accuracy (WAPE over time)
// ═══════════════════════════════════════════════════════════════

function updateForecastAccuracy(state) {
  const canvas = document.getElementById('forecast-chart');
  if (!canvas) return;

  const history = state.forecast_accuracy_history || [];
  if (history.length === 0) return;

  const labels   = history.map(h => h.tick);
  const wapeIB   = history.map(h => h.wape_ib   != null ? +(h.wape_ib   * 100).toFixed(1) : null);
  const wapeFC   = history.map(h => h.wape_fc   != null ? +(h.wape_fc   * 100).toFixed(1) : null);

  if (forecastChart) {
    forecastChart.data.labels = labels;
    forecastChart.data.datasets[0].data = wapeIB;
    forecastChart.data.datasets[1].data = wapeFC;
    forecastChart.update('none');
    return;
  }

  const commonTickStyle = { color: '#8892a6', font: { family: 'JetBrains Mono', size: 9 } };

  forecastChart = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'IB WAPE (%)',
          data: wapeIB,
          borderColor: '#448aff',
          backgroundColor: 'rgba(68,138,255,0.08)',
          fill: true,
          tension: 0.4,
          pointRadius: 2,
          borderWidth: 2,
          spanGaps: true,
        },
        {
          label: 'FC WAPE (%)',
          data: wapeFC,
          borderColor: '#1de9b6',
          backgroundColor: 'rgba(29,233,182,0.08)',
          fill: true,
          tension: 0.4,
          pointRadius: 2,
          borderWidth: 2,
          spanGaps: true,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: true,
          position: 'top',
          labels: { color: '#8892a6', font: { family: 'JetBrains Mono', size: 10 }, boxWidth: 12 }
        },
        tooltip: {
          callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(1)}%` }
        }
      },
      scales: {
        x: {
          display: true,
          title: { display: true, text: 'Simulation Tick', color: '#5c6380', font: { family: 'JetBrains Mono', size: 9 } },
          ticks: commonTickStyle,
          grid: { color: 'rgba(255,255,255,0.06)' }
        },
        y: {
          display: true,
          title: { display: true, text: 'WAPE (%)', color: '#5c6380', font: { family: 'JetBrains Mono', size: 9 } },
          ticks: { ...commonTickStyle, callback: v => v + '%' },
          grid: { color: 'rgba(255,255,255,0.06)' },
          min: 0,
        }
      },
      animation: { duration: 300 }
    },
    plugins: [{
      id: 'customBackground',
      beforeDraw(chart) {
        const ctx = chart.canvas.getContext('2d');
        ctx.save();
        ctx.globalCompositeOperation = 'destination-over';
        ctx.fillStyle = 'rgba(17,24,39,0.92)';
        ctx.fillRect(0, 0, chart.width, chart.height);
        ctx.restore();
      }
    }]
  });
}


// ═══════════════════════════════════════════════════════════════
// Network Health Narrative (Planner Assessment)
// ═══════════════════════════════════════════════════════════════

function updateNetworkHealth(state) {
  const statusBar  = document.getElementById('health-status-bar');
  const invEl      = document.getElementById('health-inventory');
  const disEl      = document.getElementById('health-disruptions');
  const riskEl     = document.getElementById('health-risks');
  const tickBadge  = document.getElementById('health-tick-badge');
  if (!statusBar) return;

  if (tickBadge) tickBadge.textContent = `Tick ${state.tick}`;

  const nodes       = state.inventory || [];
  const disruptions = state.disruptions || [];
  const demand      = state.demand || [];
  const finHistory  = state.financials_history || [];

  // ── Inventory analysis ──────────────────────────────────────
  const fcs         = nodes.filter(n => n.type === 'FulfillmentCenter');
  const ibs         = nodes.filter(n => n.type === 'IBCenter');
  const fcCritical  = fcs.filter(n => n.inventory <= 0);
  const fcLow       = fcs.filter(n => n.inventory > 0 && n.inventory < n.safety_stock);
  const fcHealthy   = fcs.filter(n => n.inventory >= n.safety_stock);
  const ibLow       = ibs.filter(n => n.inventory < n.safety_stock);

  let invLines = [];
  if (fcCritical.length > 0)
    invLines.push(`<span class="health-warn">⚠ ${fcCritical.length} FC(s) at zero stock: ${fcCritical.map(n=>nodeLabel(n.id)).join(', ')}</span>`);
  if (fcLow.length > 0)
    invLines.push(`<span class="health-risk">↓ ${fcLow.length} FC(s) below safety stock: ${fcLow.map(n=>nodeLabel(n.id)+' ('+n.inventory+'/'+n.safety_stock+')').join(', ')}</span>`);
  if (fcHealthy.length === fcs.length && fcCritical.length === 0)
    invLines.push(`<span class="health-ok">✓ All ${fcs.length} FCs above safety stock</span>`);
  if (ibLow.length > 0)
    invLines.push(`<span class="health-risk">↓ ${ibLow.length} IB(s) running low: ${ibLow.map(n=>nodeLabel(n.id)).join(', ')}</span>`);
  else
    invLines.push(`<span class="health-ok">✓ All IBCenters well-stocked</span>`);
  if (invEl) invEl.innerHTML = invLines.join('<br>');

  // ── Disruption analysis ─────────────────────────────────────
  const laneDisruptions = disruptions.filter(d => d.type === 'lane');
  const nodeDisruptions = disruptions.filter(d => d.type !== 'lane');
  const highSev = disruptions.filter(d => d.severity >= 0.6);
  let disLines = [];
  if (disruptions.length === 0) {
    disLines.push(`<span class="health-ok">✓ No active disruptions — all lanes and nodes operational</span>`);
  } else {
    if (laneDisruptions.length > 0)
      disLines.push(`<span class="health-risk">↓ ${laneDisruptions.length} lane(s) disrupted: ${laneDisruptions.map(d=>'L'+d.target_id+' ('+Math.round(d.severity*100)+'%)').join(', ')}</span>`);
    if (nodeDisruptions.length > 0)
      disLines.push(`<span class="health-warn">⚠ ${nodeDisruptions.length} node disruption(s) active</span>`);
    if (highSev.length > 0)
      disLines.push(`<span class="health-warn">⚠ ${highSev.length} high-severity event(s) (≥60%) requiring priority repair</span>`);
    const minRemaining = Math.min(...disruptions.map(d => d.remaining || 0));
    if (minRemaining <= 2)
      disLines.push(`<span class="health-ok">↑ Earliest disruption expires in ${minRemaining}t</span>`);
  }
  if (disEl) disEl.innerHTML = disLines.join('<br>');

  // ── Risk & Outlook ──────────────────────────────────────────
  const totalBacklog = demand.reduce((s, r) => s + (r.backlog || 0), 0);
  const laborStrained = demand.filter(r => r.labor_capacity_base > 0 && r.labor_capacity / r.labor_capacity_base < 0.75);
  let riskLines = [];

  // Backlog trend
  if (totalBacklog === 0) {
    riskLines.push(`<span class="health-ok">✓ Zero backlog — demand fully fulfilled</span>`);
  } else if (totalBacklog < 50) {
    riskLines.push(`<span class="health-risk">↑ Backlog building: ${totalBacklog} units pending</span>`);
  } else {
    riskLines.push(`<span class="health-warn">⚠ High backlog: ${totalBacklog} units — fill rate at risk</span>`);
  }

  // Labor strain
  if (laborStrained.length > 0)
    riskLines.push(`<span class="health-risk">↓ Labor below 75% at ${laborStrained.map(r=>nodeLabel(r.node_id)).join(', ')} — throughput constrained</span>`);

  // Cost trend
  if (finHistory.length >= 3) {
    const last3 = finHistory.slice(-3);
    const cpuTrend = last3[2].cost_per_unit_shipped - last3[0].cost_per_unit_shipped;
    if (cpuTrend > 2)
      riskLines.push(`<span class="health-risk">↑ Cost/unit rising +€${cpuTrend.toFixed(2)} over last 3 ticks</span>`);
    else if (cpuTrend < -2)
      riskLines.push(`<span class="health-ok">↓ Cost/unit improving −€${Math.abs(cpuTrend).toFixed(2)} over last 3 ticks</span>`);
  }

  // IB buffer risk
  ibs.forEach(n => {
    const pct = n.capacity > 0 ? n.inventory / n.capacity : 1;
    if (pct < 0.25)
      riskLines.push(`<span class="health-warn">⚠ ${nodeLabel(n.id)} at ${(pct*100).toFixed(0)}% capacity — replenishment lag risk</span>`);
  });

  if (riskLines.length === 0)
    riskLines.push(`<span class="health-ok">✓ No foreseeable risks — network operating within normal parameters</span>`);
  if (riskEl) riskEl.innerHTML = riskLines.join('<br>');

  // ── Overall status ──────────────────────────────────────────
  let statusClass, statusText;
  if (fcCritical.length > 0 || totalBacklog >= 50 || highSev.length > 0) {
    statusClass = 'status-critical';
    statusText = '🔴 CRITICAL — Immediate intervention required';
  } else if (fcLow.length > 0 || disruptions.length > 0 || totalBacklog > 0 || laborStrained.length > 0) {
    statusClass = 'status-warning';
    statusText = '🟡 STRESSED — Network under pressure, monitor closely';
  } else {
    statusClass = 'status-healthy';
    statusText = '🟢 HEALTHY — All nodes and lanes operating nominally';
  }
  statusBar.className = `health-status ${statusClass}`;
  statusBar.textContent = statusText;
}


// ═══════════════════════════════════════════════════════════════
// Agent Log (with reasoning expand)
// ═══════════════════════════════════════════════════════════════

function updateLog(state) {
  const container = document.getElementById('log-feed');
  const countBadge = document.getElementById('log-count');
  if (!container) return;

  const entries = state.log || [];
  if (countBadge) countBadge.textContent = `${entries.length} entries`;

  let html = '';
  entries.forEach((e, idx) => {
    const ac = AGENT_COLORS[e.agent_name] || { css: '', hex: '#ccc' };
    const hasReasoning = e.reasoning && e.reasoning.trim().length > 0;

    html += `
      <div class="log-entry ${hasReasoning ? 'has-reasoning' : ''}" ${hasReasoning ? `onclick="toggleReasoning(${idx})"` : ''}>
        <span class="log-tick">T${e.tick}</span>
        <span class="log-agent ${ac.css}">${e.agent_name}</span>
        <span class="log-action">${escapeHtml(e.action_taken)}${hasReasoning ? ' <span class="reasoning-toggle">🧠</span>' : ''}</span>
      </div>`;
    if (hasReasoning) {
      html += `<div class="log-reasoning" id="reasoning-${idx}" style="display:none;">
        <pre>${escapeHtml(e.reasoning)}</pre>
      </div>`;
    }
  });
  container.innerHTML = html;
}

function toggleReasoning(idx) {
  const el = document.getElementById(`reasoning-${idx}`);
  if (el) {
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
  }
}


// ═══════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}


function initTickControls() {
  const btnAuto   = document.getElementById('btn-mode-auto');
  const btnManual = document.getElementById('btn-mode-manual');
  const btnTick   = document.getElementById('btn-tick');
  const statusEl  = document.getElementById('tick-status');
  if (!btnAuto) return;

  function setMode(mode) {
    if (socket) socket.emit('set_tick_mode', { mode });
    const isManual = mode === 'manual';
    btnAuto.classList.toggle('active', !isManual);
    btnManual.classList.toggle('active', isManual);
    btnTick.disabled = !isManual;
    btnTick.classList.toggle('manual-ready', isManual);
    btnTick.title = isManual ? 'Click to run the next tick' : 'Switch to Manual mode first';
    statusEl.textContent = isManual
      ? 'Mode: Manual — click button to advance'
      : 'Mode: Auto — ticks run automatically';
  }

  btnAuto.addEventListener('click',   () => setMode('auto'));
  btnManual.addEventListener('click', () => setMode('manual'));

  btnTick.addEventListener('click', () => {
    if (!socket) return;
    btnTick.disabled = true;
    btnTick.textContent = '⏳ Running…';
    statusEl.textContent = 'Executing tick…';
    socket.emit('trigger_tick');
  });

  if (socket) {
    socket.on('tick_trigger_ack', (data) => {
      if (data.status === 'done') {
        statusEl.textContent = `Tick ${data.tick} complete. Click to advance.`;
      } else if (data.status === 'busy') {
        statusEl.textContent = 'Tick still running, please wait.';
      } else if (data.status === 'error') {
        statusEl.textContent = `Error: ${data.msg}`;
      }
      btnTick.disabled = false;
      btnTick.textContent = '▶ Run Next Tick';
    });

    socket.on('tick_mode_ack', (data) => {
      // Server confirmed mode — no UI action needed, already set locally
    });
  }
}

function initManualControls() {
  const btnInject = document.getElementById('btn-inject');
  const btnDemand = document.getElementById('btn-demand-mod');

  if (btnInject) {
    btnInject.addEventListener('click', () => {
      if (replayMode) return alert("Switch to Live Mode first.");
      const type = document.getElementById('inject-type').value;
      const targetId = document.getElementById('inject-target').value;
      const severity = document.getElementById('inject-sev').value;
      if (!targetId) return alert("Please enter a Target ID.");
      socket.emit('inject_disruption', {
        type: type,
        target_id: parseInt(targetId),
        severity: parseFloat(severity),
        duration: 5,
        description: `Manual override event`
      });
    });
  }

  if (btnDemand) {
    btnDemand.addEventListener('click', () => {
      if (replayMode) return alert("Switch to Live Mode first.");
      const nodeId = document.getElementById('demand-node').value;
      const mult = document.getElementById('demand-mult').value;
      if (!nodeId) return alert("Please enter an FC ID.");
      socket.emit('set_demand_modifier', {
        node_id: parseInt(nodeId),
        multiplier: parseFloat(mult),
        description: `User-defined spike/drop`
      });
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  connectSocket();
  initReplay();
  initManualControls();
  // tick controls need socket, so init after connect
  setTimeout(initTickControls, 300);
});

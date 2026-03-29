/* ═══════════════════════════════════════════════════════════════
   SWARMCHAIN-SIM  ·  Frontend Application Logic
   WebSocket, Chart.js, SVG topology, Replay mode, Reasoning logs
   ═══════════════════════════════════════════════════════════════ */

// ── State ────────────────────────────────────────────────────
let socket = null;
let costChart = null;
let financialChart = null;
let prevState = null;
let replayMode = false;      // true = scrubbing historical ticks
let replayPlaying = false;   // true = auto-advancing replay
let replayInterval = null;

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
  width: 520, height: 320,
  nodes: {
    1: { x: 120, y: 100, label: 'IB1', type: 'ib' },
    2: { x: 120, y: 220, label: 'IB2', type: 'ib' },
    3: { x: 380, y:  60, label: 'FC3', type: 'fc' },
    4: { x: 380, y: 160, label: 'FC4', type: 'fc' },
    5: { x: 380, y: 260, label: 'FC5', type: 'fc' },
  },
  suppliers: [
    { x: 120, y: 30,  label: 'Supplier → IB1', node: 1 },
    { x: 120, y: 310, label: 'Supplier → IB2', node: 2 },
  ],
  lanes: {
    1: { from: 1, to: 3 },
    2: { from: 1, to: 4 },
    3: { from: 2, to: 4 },
    4: { from: 2, to: 5 },
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

  let html = `
    <defs>
      <filter id="glow-healthy" x="-20%" y="-20%" width="140%" height="140%">
        <feGaussianBlur stdDeviation="6" result="blur" />
        <feComposite in="SourceGraphic" in2="blur" operator="over" />
      </filter>
      <filter id="glow-warn" x="-20%" y="-20%" width="140%" height="140%">
        <feGaussianBlur stdDeviation="6" result="blur" />
        <feComposite in="SourceGraphic" in2="blur" operator="over" />
      </filter>
      <filter id="glow-crit" x="-20%" y="-20%" width="140%" height="140%">
        <feGaussianBlur stdDeviation="8" result="blur" />
        <feComposite in="SourceGraphic" in2="blur" operator="over" />
      </filter>
      
      <linearGradient id="grad-ib" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#448aff" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="#2962ff" stop-opacity="0.05"/>
      </linearGradient>
      
      <linearGradient id="grad-fc" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#1de9b6" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="#00bfa5" stop-opacity="0.05"/>
      </linearGradient>
    </defs>
  `;

  TOPO.suppliers.forEach(s => {
    const node = TOPO.nodes[s.node];
    html += `<line x1="${s.x}" y1="${s.y + 10}" x2="${node.x}" y2="${node.y - 30}"
                   stroke="rgba(99,115,175,0.4)" stroke-width="2" stroke-dasharray="3 4"/>`;
    html += `<text x="${s.x}" y="${s.y}" class="topo-supplier-label" fill="#8892a6">${s.label}</text>`;
  });

  Object.entries(TOPO.lanes).forEach(([laneId, lane]) => {
    const from = TOPO.nodes[lane.from];
    const to   = TOPO.nodes[lane.to];
    const sev  = laneDisruptions[parseInt(laneId)] || 0;
    const isDisrupted = sev > 0;
    const info = laneInfo[parseInt(laneId)];
    const tc   = info ? info.transport_cost : '?';
    const mx = (from.x + to.x) / 2;
    const my = (from.y + to.y) / 2;
    const cx = mx - 20;

    // Base background path
    html += `<path d="M${from.x + 28},${from.y} Q${cx},${my} ${to.x - 28},${to.y}"
                   fill="none" stroke="var(--border)" stroke-width="6" stroke-linecap="round"/>`;

    // Flow line path
    const laneClass = isDisrupted ? 'disrupted' : 'healthy';
    const strokeColor = isDisrupted ? 'var(--red)' : 'var(--blue)';
    html += `<path d="M${from.x + 28},${from.y} Q${cx},${my} ${to.x - 28},${to.y}"
                   class="topo-lane ${laneClass}" id="lane-path-${laneId}"
                   fill="none" stroke="${strokeColor}" stroke-linecap="round"/>`;

    const lx = mx - 10;
    const ly = my + (from.y < to.y ? -8 : 8);
    const sevLabel = sev > 0 ? ` ⚠${Math.round(sev * 100)}%` : '';
    
    // Lane badge background
    html += `<rect x="${lx - 25}" y="${ly - 10}" width="50" height="14" rx="4" fill="var(--bg-base)" stroke="var(--border)" stroke-width="1"/>`;
    
    html += `<text x="${lx}" y="${ly}" class="topo-lane-label">TC:${tc}${sevLabel}</text>`;

    if (!isDisrupted) {
      html += `<circle r="4" fill="var(--cyan)" filter="url(#glow-healthy)">
                 <animateMotion dur="${2.5 + (parseInt(laneId) * 0.5)}s" repeatCount="indefinite">
                   <mpath href="#lane-path-${laneId}"/>
                 </animateMotion>
               </circle>`;
    }
  });

  Object.entries(TOPO.nodes).forEach(([nodeId, pos]) => {
    const n = nodeMap[parseInt(nodeId)];
    let fill, stroke, filter;
    
    const isIB = pos.type === 'ib';
    const baseGradient = isIB ? 'url(#grad-ib)' : 'url(#grad-fc)';
    
    if (!n || n.inventory <= 0) {
      fill = 'rgba(255,82,82,0.2)'; stroke = 'var(--red)'; filter = 'url(#glow-crit)';
    } else if (n.inventory < n.safety_stock) {
      fill = 'rgba(255,171,64,0.15)'; stroke = 'var(--yellow)'; filter = 'url(#glow-warn)';
    } else {
      fill = baseGradient; 
      stroke = isIB ? 'var(--blue)' : 'var(--green)'; 
      filter = 'url(#glow-healthy)';
    }

    // Outer glow ring
    html += `<circle cx="${pos.x}" cy="${pos.y}" r="32" fill="none" stroke="${stroke}" stroke-width="1" opacity="0.3" filter="${filter}">
      <animate attributeName="r" values="32;36;32" dur="3s" repeatCount="indefinite" />
      <animate attributeName="opacity" values="0.3;0.1;0.3" dur="3s" repeatCount="indefinite" />
    </circle>`;

    // Main node circle
    html += `<g class="topo-node">
      <circle cx="${pos.x}" cy="${pos.y}" r="28" fill="${fill}" stroke="${stroke}" stroke-width="2.5" filter="${filter}"/>
      <text x="${pos.x}" y="${pos.y - 4}" class="topo-node-label">${pos.label}</text>
      <text x="${pos.x}" y="${pos.y + 12}" class="topo-node-inv">${n ? n.inventory : '?'}/${n ? n.capacity : '?'}</text>
    </g>`;
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
    const typeLabel = n.type === 'IBCenter' ? 'IB' : 'FC';

    html += `
      <div class="inv-node">
        <div class="inv-label">
          <div class="node-type">${typeLabel}</div>
          <div class="node-id">${n.id}</div>
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

    html += `<tr>
      <td>FC${r.node_id}</td>
      <td class="val-white">${r.orders}</td>
      <td class="${fcstClass}">${fcstVal}</td>
      <td class="${maeClass}">${maeVal}</td>
      <td class="${laborClass}">${r.labor_capacity}/${r.labor_capacity_base}</td>
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
    let targetLabel = d.type === 'lane' ? `Lane ${d.target_id}` : d.type === 'ib_node' ? `IB${d.target_id}` : `FC${d.target_id}`;

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
          x: { display: false },
          y: {
            display: true,
            position: 'left',
            grid: { color: 'rgba(99,115,175,0.08)' },
            ticks: { color: '#5c6380', font: { family: 'JetBrains Mono', size: 9 }, callback: v => '€' + v }
          },
          y1: {
            display: true,
            position: 'right',
            grid: { drawOnChartArea: false },
            ticks: { color: '#5c6380', font: { family: 'JetBrains Mono', size: 9 }, callback: v => '€' + v }
          }
        },
        animation: { duration: 300 }
      }
    });
  }
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
    const action = e.action_taken.length > 140
      ? e.action_taken.substring(0, 140) + '…'
      : e.action_taken;
    const hasReasoning = e.reasoning && e.reasoning.trim().length > 0;

    html += `
      <div class="log-entry ${hasReasoning ? 'has-reasoning' : ''}" ${hasReasoning ? `onclick="toggleReasoning(${idx})"` : ''}>
        <span class="log-tick">T${e.tick}</span>
        <span class="log-agent ${ac.css}">${e.agent_name}</span>
        <span class="log-action">${escapeHtml(action)}${hasReasoning ? ' <span class="reasoning-toggle">🧠</span>' : ''}</span>
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
});

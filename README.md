# SwarmChain-Sim

An adversarial supply chain simulation powered by AI agents. A swarm of rule-based agents manages a multi-tier fulfillment network in real time — handling demand forecasting, inventory replenishment, labor planning, transport routing, and probabilistic disruptions — while a single **Strategist LLM** reviews the full network state each tick and issues targeted overrides.

Designed to scale to **100+ nodes and 10,000+ lanes** with sub-10-second tick times.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    TICK ENGINE (v3)                     │
│                                                         │
│  Forecast → Staffing → Disruptor → Repair               │
│       ↓                                                 │
│  Demand → Supply → Planner                              │
│       ↓                                                 │
│  Strategist LLM ← reviews full state, issues overrides  │
│       ↓                                                 │
│  Snapshot + Financials                                  │
└──────────────────────┬──────────────────────────────────┘
                       │ SQLite (WAL)
         ┌─────────────┴──────────────┐
         │       sim_state.db         │
         │   (single source of truth) │
         └─────────────┬──────────────┘
                       │
          ┌────────────┴────────────┐
          │    Web Dashboard        │
          │  (Flask + WebSocket)    │
          │  http://localhost:5050  │
          └─────────────────────────┘
```

### How it works

Each **tick** represents one operational period (e.g. a shift). All agents communicate exclusively through SQLite — no direct agent-to-agent messaging.

| Agent | Role | Implementation |
|-------|------|---------------|
| **Forecast** | Predicts next-tick demand per FC using exponential smoothing + seasonal signal | Rule-based |
| **Staffing** | Plans labor capacity at each node and transport capacity on each lane for next tick | Rule-based |
| **Disruptor** | Probabilistically fires chaos events: lane closures, IB failures, FC outages | Rule-based |
| **Repair** | Auto-expires elapsed disruptions; resolves highest-severity active disruption | Rule-based |
| **Demand** | Executes customer orders; depletes FC inventory against labor capacity | Rule-based |
| **Supply** | Refills Inbound Centers from external suppliers using 3-tick forward coverage | Rule-based |
| **Planner** | Replenishes FCs below safety stock via cheapest active inbound lane | Rule-based |
| **Strategist** | Reviews the full network snapshot; issues extra supply orders, priority repairs, demand warnings | **1 LLM call** |

The Strategist is the only LLM call per tick. If it errors, the tick completes normally — it is non-fatal. Run with `--no-llm` for pure rule-based mode at ~0.0 seconds per tick.

### Network Topology (default)

```
External Suppliers
      │                 │
  IB1 (cap=1200)   IB2 (cap=1000)
    /       \          /       \
  L1(12.5) L2(15.0) L3(11.0) L4(14.0)
   │          \       /          │
  FC3         FC4 (shared)      FC5
(cap=800)    (cap=950)        (cap=700)
```

- **IBCenters** receive external supplier orders and dispatch stock to FulfillmentCenters
- **FulfillmentCenters** fulfill customer orders; replenishment triggers when inventory drops below `safety_stock`
- **Lanes** carry transport costs, per-tick capacity limits, and can be disrupted

---

## Quick Start

### 1. Install dependencies

```bash
pip install flask flask-socketio google-genai python-dotenv
```

For Claude models (optional):
```bash
pip install anthropic
```

### 2. Configure API keys

Create a `.env` file in the project root:

```env
# Gemini (required for Strategist LLM)
GOOGLE_API_KEY=your-key-here

# Optional: Vertex AI (auto-detected if set)
# GOOGLE_CLOUD_PROJECT=your-project-id
# GOOGLE_CLOUD_LOCATION=us-central1

# Optional: Claude (if using Claude as Strategist)
# ANTHROPIC_API_KEY=your-key-here
```

### 3. Initialize the database

```bash
python env.py
```

Creates `sim_state.db` with the default 5-node network. Re-run to wipe and reset.

### 4. Start the simulation

```bash
# Standard mode: rule-based agents + 1 Strategist LLM call per tick (~4s/tick)
python tick_loop.py

# Pure rule-based mode: no API calls, ~0.0s per tick
python tick_loop.py --no-llm

# Run a fixed number of ticks then stop
python tick_loop.py --ticks 20

# Deterministic run (reproducible with fixed seed)
python tick_loop.py --ticks 20 --seed 42

# Combine flags
python tick_loop.py --no-llm --ticks 100 --seed 99
```

### 5. Open the live dashboard

In a second terminal:

```bash
python web_monitor.py
```

Open **http://localhost:5050** in your browser.

---

## Web Dashboard

![Dashboard](https://img.shields.io/badge/dashboard-live-brightgreen)

Available at **http://localhost:5050**, provides:

- **Network topology** — live node inventory levels, lane statuses, active disruptions
- **Inventory sparklines** — 20-tick rolling history per node
- **Demand panel** — orders, shipped, backlog, labor utilization per FC
- **Financial analytics** — labor cost, transport cost, cost-per-unit over time
- **Token cost tracker** — cumulative LLM API spend
- **Agent log** — last 15 agent decisions with Strategist chain-of-thought reasoning
- **Replay scrubber** — rewind to any historical tick
- **God Mode controls** — manually inject disruptions, resolve disruptions, apply demand spikes/drops

---

## Benchmark Mode

Compare Strategist model performance across simulation KPIs:

```bash
python benchmark.py --models "gemini-2.0-flash,gemini-2.5-flash" --ticks 20 --seed 42
```

The benchmark resets the DB for each model, runs with an identical fixed seed (deterministic), and prints a side-by-side comparison table.

**Tracked KPIs:** Fill Rate, Total Shipped, Total Backlog, Peak Backlog, Stockout Events, Forecast MAE, Disruptions Triggered, Network Execution Cost, Cost Per Unit Shipped, API Token Cost.

---

## Configuration

### Model Config (`model_config.json`)

Controls which LLM the Strategist uses and pricing for token cost tracking:

```json
{
  "default_model": "gemini-2.5-flash",
  "agents": {
    "Strategist": { "model": "gemini-2.5-flash" }
  },
  "pricing": {
    "gemini-2.5-flash":      { "input_per_m_usd": 0.15, "output_per_m_usd": 0.60 },
    "gemini-2.5-flash-lite": { "input_per_m_usd": 0.08, "output_per_m_usd": 0.30 },
    "gemini-2.0-flash":      { "input_per_m_usd": 0.10, "output_per_m_usd": 0.40 },
    "gemini-1.5-flash":      { "input_per_m_usd": 0.075,"output_per_m_usd": 0.30 }
  },
  "eur_per_usd": 0.92
}
```

The Strategist automatically falls back through cheaper Gemini Flash variants if the primary model returns a 404 or hits quota limits.

To use Claude as the Strategist, set `"model": "claude-haiku-4-5-20251001"` (or any Claude model ID) and ensure `ANTHROPIC_API_KEY` is in your `.env`.

### Network Config (`network_config.json`)

Defines the full network topology. Modify this file to scale to larger networks — no code changes required.

```jsonc
{
  "nodes": [
    // Each node: id, type (IBCenter|FulfillmentCenter), capacity, safety_stock,
    //            initial_inventory, labor_capacity_base, hourly_labor_cost, units_per_hour
    {"id": 1, "type": "IBCenter", "capacity": 1200, "safety_stock": 200,
     "initial_inventory": 1200, "labor_capacity_base": 150,
     "hourly_labor_cost": 15.0, "units_per_hour": 20.0},
    ...
  ],
  "lanes": [
    // Each lane: id, origin (node id), destination (node id), transport_cost
    {"id": 1, "origin": 1, "destination": 3, "transport_cost": 12.5},
    ...
  ],
  // Maps lane_id → source IB node_id
  "lane_to_ib":     {"1": 1, "2": 1, "3": 2, "4": 2},
  // Maps FC node_id → list of inbound lane_ids
  "inbound_lanes":  {"3": [1], "4": [2, 3], "5": [4]},
  // Maps IB node_id → {FC node_id: weight} for supply distribution
  "ib_fc_weights":  {"1": {"3": 1.0, "4": 0.5}, "2": {"4": 0.5, "5": 1.0}},
  // Max external inflow per IB per tick
  "max_external_inflow": {"1": 350, "2": 280},
  // Base customer demand per FC per tick (before seasonal/random variation)
  "base_demand":    {"3": 70, "4": 90, "5": 55},
  // Physical transport capacity limits per lane
  "lane_tc_max":    {"1": 350, "2": 300, "3": 280, "4": 250},
  "lane_tc_base":   {"1": 200, "2": 180, "3": 160, "4": 150},
  // Disruption probability per tick (0.0–1.0)
  "disruption_probability": 0.30,
  // Max concurrent active disruptions
  "max_active_disruptions": 3
}
```

After editing `network_config.json`, re-run `python env.py` to reset the database.

### Tick Interval

Edit `INTERVAL` in `tick_loop.py` (default: `2` seconds between ticks).

---

## Database Schema

`sim_state.db` is the single source of truth. All agents read and write exclusively through it.

| Table | Purpose |
|-------|---------|
| `nodes` | Network topology: IBCenters + FulfillmentCenters with capacity and labor rates |
| `lanes` | Transport routes: costs, capacity, status (`active`/`disrupted`) |
| `demand` | Per-tick: orders, shipped, backlog, actual labor capacity per FC |
| `forecast` | Predicted vs actual demand with MAE tracking per node per tick |
| `labor_plan` | Staffing plans written for next tick (±15% execution flex) |
| `transport_plan` | Transport capacity bookings written for next tick (±15% execution flex) |
| `disruptions` | All disruption events: type, target, severity, duration, resolved flag |
| `transport` | Actual replenishment shipments executed per tick per lane |
| `snapshots` | Inventory history per node per tick (used by dashboard sparklines) |
| `financials` | Labor cost, transport cost, cost-per-unit-shipped per tick |
| `token_log` | LLM API usage and cost per agent per tick |
| `log` | Full agent decision audit trail with optional reasoning text |
| `demand_modifiers` | Manual demand multipliers set via the dashboard |

### Useful queries

```sql
-- Fill rate by tick
SELECT tick, ROUND(SUM(shipped)*100.0/SUM(orders+backlog), 1) AS fill_pct
FROM demand GROUP BY tick ORDER BY tick;

-- Inventory trend per node
SELECT tick, node_id, inventory FROM snapshots ORDER BY tick, node_id;

-- Active disruptions right now
SELECT * FROM disruptions WHERE resolved=0 ORDER BY severity DESC;

-- LLM spend summary
SELECT agent_name, COUNT(*) AS calls, ROUND(SUM(cost_eur), 5) AS total_eur
FROM token_log GROUP BY agent_name ORDER BY total_eur DESC;

-- Network execution cost over time
SELECT tick, total_labor_cost, total_transport_cost, cost_per_unit_shipped
FROM financials ORDER BY tick;

-- Forecast accuracy per node
SELECT node_id, ROUND(AVG(mae), 2) AS avg_mae, COUNT(*) AS samples
FROM forecast WHERE mae IS NOT NULL GROUP BY node_id;

-- Backlog build-up
SELECT tick, SUM(backlog) AS total_backlog FROM demand GROUP BY tick ORDER BY tick;
```

---

## File Structure

```
SwarmChain-Sim/
├── tick_loop.py          # Main simulation engine (v3): all 7 agents + Strategist
├── env.py                # DB schema, initialization, network config loader
├── token_utils.py        # Multi-model LLM client (Gemini + Claude, fallback chain)
├── web_monitor.py        # Web dashboard server (Flask + WebSocket)
├── benchmark.py          # Model comparison benchmark tool
├── model_config.json     # Strategist model selection and API pricing
├── network_config.json   # Network topology: nodes, lanes, demand, limits
├── .env                  # API keys (not committed)
├── sim_state.db          # SQLite simulation state (auto-created, not committed)
└── web/
    ├── index.html        # Dashboard UI
    ├── app.js            # WebSocket client + live charts
    └── style.css         # Dashboard styles
```

---

## Scaling to Large Networks

The v3 architecture handles large networks without code changes:

- **Rule-based agents** are pure Python — O(n) time complexity, zero API calls per node
- **Single DB connection per tick** — WAL mode, no lock contention
- **Strategist prompt is compact** — ~2 lines per node keeps token count manageable even at 100+ nodes
- **`--no-llm` mode** — run thousands of ticks instantly for backtesting and parameter tuning

To scale up: add nodes and lanes to `network_config.json`, update the topology maps (`lane_to_ib`, `inbound_lanes`, `ib_fc_weights`), then run `python env.py` to reset.

---

## Performance

| Mode | Tick Time | LLM Calls/Tick | Cost/Tick |
|------|-----------|---------------|-----------|
| `--no-llm` | **~0.0s** | 0 | $0.00 |
| Default (Strategist) | **~4s** | 1 | ~$0.0002 |
| v2 legacy (7 LLM agents) | 60–180s | 7 | ~$0.01 |

---

## License

MIT

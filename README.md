# Adversarial Supply Chain Swarm Simulator

A discrete-time supply chain simulation driven by autonomous AI agents that communicate exclusively through a shared SQLite database. Every agent calls **Claude Opus 4.6** to make its decision — no rule-based logic, no direct agent-to-agent communication. The network is subject to stochastic disruption events; agents must detect, absorb, and recover from them in real time.

---

## How to Launch

You need **two PowerShell terminals** open in the project folder.

### Step 0 — Configure your API key (one-time setup)

The simulation calls Claude Opus 4.6 via the Anthropic API. Create a `.env` file in the project folder with your key:

```powershell
cd "C:\Users\manum\Desktop\IA Projects\SupplyChain-Swarm-Sim"
echo "ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE" > .env
```

Or set it in your PowerShell session (lasts until the session closes):

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-YOUR_KEY_HERE"
```

Get a key at https://console.anthropic.com under **API Keys**.

### Step 1 — Initialize (run once, or to reset)

```powershell
cd "C:\Users\manum\Desktop\IA Projects\SupplyChain-Swarm-Sim"
& "C:\Users\manum\anaconda3\python.exe" env.py
```

This wipes `sim_state.db` and recreates it from scratch with the baseline network. Run this whenever you want to start a fresh simulation.

### Step 2 — Start the tick engine (Terminal 1)

```powershell
& "C:\Users\manum\anaconda3\python.exe" -X utf8 tick_loop.py
```

This is the simulation clock. Every 5 seconds it runs all seven agents in sequence and captures a state snapshot. Each agent makes a real Claude Opus 4.6 API call — expect ~5–15 seconds per tick (longer than before due to API latency). You will see each agent's decision printed to the console as it happens.

### Step 3 — Open the live dashboard (Terminal 2)

```powershell
& "C:\Users\manum\anaconda3\python.exe" -X utf8 monitor.py
```

This opens a full-screen Rich terminal dashboard that refreshes every 2 seconds. It reads directly from the database — it has no effect on the simulation, it only observes it.

### Stop

Press `Ctrl+C` in either terminal to stop that process. Stopping the tick engine pauses the simulation; stopping the monitor has no effect on it.

> **Note:** `-X utf8` is required on Windows to avoid encoding errors with the unicode box-drawing characters used in the dashboard and agent output.

---

## Prerequisites

```powershell
pip install rich anthropic
```

Python 3.12+ (confirmed working with Anaconda at `C:\Users\manum\anaconda3\python.exe`).

---

## What the Simulation Models

The simulation represents a simplified e-commerce fulfilment network. Customer orders arrive at **Fulfilment Centers**, which process and ship them using their available labor. **Inbound Centers** act as upstream buffer warehouses that replenish Fulfilment Centers when their inventory drops below a safety threshold. External suppliers refill Inbound Centers. The whole network is subject to real-world operational disruptions that hit nodes and transport lanes unpredictably.

Each "tick" represents one operating period (think: one week).

---

## Network Topology

```
  External Supplier
        |
        | (IB1: max +350/tick)
        v
  ┌───────────────┐
  │  IBCenter 1   │ ──── Lane 1 (TC=12.5) ──► FulfillmentCenter 3 (labor base: 100)
  │  cap:  1200   │
  │  safety: 200  │ ──── Lane 2 (TC=15.0) ──► FulfillmentCenter 4 (labor base: 125)
  └───────────────┘                                     ▲
                                                        │
  ┌───────────────┐                          Lane 3 (TC=11.0)
  │  IBCenter 2   │ ─────────────────────────────────────┘
  │  cap:  1000   │
  │  safety: 150  │ ──── Lane 4 (TC=14.0) ──► FulfillmentCenter 5 (labor base: 78)
  └───────────────┘
        ▲
        | (IB2: max +280/tick)
  External Supplier
```

**TC = transport cost per unit shipped.** This is a purely economic metric — the cost of moving one unit along that lane. It does NOT determine how likely a lane is to be disrupted (disruptions are random events). A high TC lane is expensive to use; it is not inherently more fragile.

### IBCenters (IB1, IB2)

IBCenters are inbound buffer warehouses. They:
- Receive stock from external suppliers each tick, up to a maximum inflow rate
- Hold inventory until the Planner agent decides to ship it downstream to an FC
- **Do not process customer orders** — they have no labor capacity
- Can be hit by node disruptions (dock breakdowns, receiving crew issues) that cap how much they can dispatch in a given tick

The amount the external supplier sends each tick is **not fixed**. The Supply agent computes a target inventory level based on the forecasted downstream demand over the next 3 ticks, weighted by how much of that demand flows through each IB. If demand forecasts rise, the IB requests more stock from the supplier.

### FulfillmentCenters (FC3, FC4, FC5)

FCs receive stock from IBCenters and ship it to customers. They:
- Experience stochastic customer demand each tick (seasonal + random variation)
- Process orders using their **labor capacity**, which varies ±30% around a baseline each tick
- Accumulate **backlog** when either stock or labor is insufficient to meet total demand
- Can be hit by node disruptions (strikes, power outages, equipment failures) that further reduce labor capacity

**Key insight:** shipments are constrained by the minimum of three things: available inventory, total demand (orders + carried backlog), and actual labor capacity. If demand is 100 but labor capacity is 85, only 85 units ship and 15 enter backlog — even if there is plenty of stock.

### Lanes

Lanes are transport connections between IBCenters and FulfillmentCenters. They can be disrupted by events such as carrier failures, road closures, port congestion, or severe weather. A disrupted lane reduces the throughput of replenishment shipments proportionally to the disruption severity (e.g. 60% severity means only 40% of the planned replenishment quantity gets through). A fully blocked lane (severity = 100%) stops replenishment completely on that route.

---

## Agent Roster

There are **seven agents**. Every agent calls Claude Opus 4.6 to make its decision. They run in a fixed sequence every tick.

### 1. Forecast
**File:** `forecast_action.py`

Claude receives the last 5 ticks of actual demand per FC, the seasonal signal for the next tick, and any active FC disruptions. It predicts next-tick order volumes and returns them as a JSON object. The forecast is stored in the `forecast` table; the Demand agent updates it with actual outcomes and computes MAE.

### 2. Staffing (new)
**File:** `staffing_action.py`

Runs immediately after Forecast, before demand is executed. Claude sees the just-computed forecasts, current FC/IB inventory, active backlogs, and disruptions, then decides two things:

- **Planned labor** per node (FC and IB) for the next tick — how many staff to schedule.
- **Planned transport capacity** per lane — how much carrier/truck capacity to book.

These plans are written to `labor_plan` and `transport_plan`. They constrain the Demand and Planner agents in the next tick with a **±15% flex** allowance (you can overshoot/undershoot the plan by at most 15% at execution time, simulating that you can't double staffing day-of but can add a small surge).

This models the real-world planning cycle: *forecast this week → plan staffing for next week → execute with limited day-of flexibility*.

### 3. Demand
**File:** `demand_action.py`

Executes customer order fulfilment. For each FC:

1. Draws a random order volume: `base_demand × seasonal_multiplier × noise(0.75–1.25)`.
2. Adds carried backlog from the previous tick.
3. Reads the **staffing plan** from the prior tick. Claude decides the flex multiplier (within ±15% of planned_labor) based on actual vs planned demand.
4. Applies any active FC node disruption penalty (reduces labor further by severity).
5. Ships: `min(inventory, total_demand, actual_labor)`
6. New backlog = total demand − shipped.

**IBCenters now have labor capacity too.** Their `labor_capacity_base` represents outbound dispatch capacity per tick. This is planned by the Staffing agent and enforced by the Planner.

### 4. Supply
**File:** `supply_action.py`

Claude decides how much stock to order from external suppliers for each IBCenter. It receives current IB inventory, average forecasts for the next 3 ticks (weighted by which FCs each IB serves), and any active supply-side disruptions. Returns quantities capped by max inflow rate and available storage space.

### 5. Disruptor
**File:** `disruptor_action.py`

The chaos engine. Python determines probabilistically (30% per tick) whether and what type of event fires. Claude writes the contextual narrative description of the event, grounding it in the current network state.

| Target type | Probability | Examples |
|---|---|---|
| `lane` | 50% of events | Carrier failure, road closure, port congestion, fuel shortage, customs delay |
| `ib_node` | 20% of events | Dock breakdown, inbound inspection backlog, receiving crew unavailability |
| `fc_node` | 30% of events | Labor strike, power outage, workplace accident, conveyor failure, IT outage |

Maximum 3 simultaneous active disruptions.

### 6. Repair
**File:** `repair_action.py`

Claude reviews all active prior-tick disruptions and decides which one to resolve based on network impact — prioritizing high severity, FC disruptions when backlogs are accumulating, and long-remaining events. Cannot repair same-tick disruptions. Auto-expires elapsed events.

### 7. Planner
**File:** `planner_action.py`

Claude decides replenishment shipments from IBCenters to FCs below `safety_stock`. It has full visibility of:
- FC inventory gaps and inbound lane options
- Transport plans (booked carrier capacity per lane, ±15% flex)
- IB inventory, IB node disruptions, and IB **labor plans** (shared dispatch capacity across all FCs served)
- Lane disruption throughput penalties

Claude must jointly optimize across all FC replenishments to respect the shared IB labor budget. The Planner writes a replenishment JSON; the agent then applies hard physical constraints before committing to the DB atomically.

---

## Tick Execution Order

```
Tick N begins:
  1. Forecast     → Claude predicts demand for tick N+1
  2. Staffing     → Claude plans labor & transport capacity for tick N+1
  3. Demand       → deplete FC inventory using tick N's labor plan (±15% flex)
  4. Supply       → Claude decides IBCenter refill from external suppliers
  5. Disruptor    → probabilistically trigger event; Claude writes narrative
  6. Repair       → Claude resolves the most impactful prior-tick disruption
  7. Planner      → Claude decides FC replenishments respecting transport plan & IB labor budget
     snapshot()   → record all node inventory levels for sparkline history
Tick N ends → sleep 5s → Tick N+1
```

The order is load-bearing:
- **Staffing runs after Forecast** so plans are based on the freshest forecast.
- **Demand runs after Staffing** so it has access to the labor plan for tick N.
- **Demand depletes before Planner replenishes**, so Planner always sees real post-demand inventory.
- **Disruptor fires before Repair** so a same-tick disruption is visible for at least one tick before being resolved.
- **Supply fires after Demand** so IB target calculations use the most recent actuals.

The labor_plan and transport_plan tables create a temporal coupling: **decisions made at tick N constrain execution at tick N+1**. This is the core new mechanic — you must plan ahead, and day-of flexibility is limited to ±15%.

---

## Live Dashboard

Five panels, refreshed every 2 seconds:

### Inventory Health
Per-node inventory bar chart and 20-tick sparkline trend. Color coding:
- **Green**: inventory above safety_stock threshold — healthy
- **Yellow**: inventory below safety_stock — Planner will replenish next tick
- **Red**: inventory at zero — stockout

### Network & Disruptions
**Top half:** Lane table showing transport cost (TC) per unit and current disruption severity if any lane is affected.

**Bottom half:** Active node disruptions (IBCenter and FulfillmentCenter events only, not lanes). Shows the target node, severity percentage, remaining duration in ticks, and the event description (e.g. "Labor strike at FC5").

### Demand + Forecast + Labor
Per-FC row showing:
- **Orders**: raw customer order volume this tick
- **Fcst**: forecast that was made last tick for this tick's demand. Dimmed if based on fallback/last-value rather than exponential smoothing.
- **MAE**: Mean Absolute Error of the forecast vs actual. Green < 10, yellow < 25, red ≥ 25.
- **Labor**: actual/base labor capacity this tick. Yellow if below 85% of base; red if below 50%.
- **Ship**: units actually shipped (constrained by inventory and labor)
- **Backlog**: cumulative unmet demand. Red if > 0.
- **Trend**: 20-tick sparkline of order history

### Token Cost
**Real** Claude Opus 4.6 API cost from actual API responses. Pricing: $5.00/M input tokens, $25.00/M output tokens, converted at 0.92 EUR/USD. Adaptive thinking tokens are included in the input count.

Shows this-tick cost, cumulative session cost, a cost trend sparkline, and a per-agent breakdown of the last 6 entries.

### Agent Log
Last 8 agent decisions, color-coded by agent:
- Red: Disruptor
- Green: Repair
- Yellow: Planner
- Cyan: Demand
- Blue: Supply
- Magenta: Forecast

---

## Database Schema

All simulation state lives in `sim_state.db`. Agents communicate exclusively by reading and writing this database.

| Table | Purpose |
|---|---|
| `nodes` | id, type, capacity, safety_stock, inventory, labor_capacity_base |
| `lanes` | id, origin, destination, transport_cost, status |
| `disruptions` | id, tick_started, duration, type, target_id, severity, description, resolved |
| `demand` | id, tick, node_id, orders, shipped, backlog, labor_capacity |
| `forecast` | id, tick_created, forecast_for_tick, node_id, forecast_demand, actual_demand, mae, method |
| `labor_plan` | id, tick_planned, tick_for, node_id, planned_labor, flex_pct |
| `transport_plan` | id, tick_planned, tick_for, lane_id, planned_capacity, flex_pct |
| `snapshots` | id, tick, node_id, inventory — inventory history for sparklines |
| `log` | id, tick, agent_name, action_taken — full decision audit trail |
| `token_log` | id, tick, agent_name, input_tokens, output_tokens, cost_eur |

### Key design decisions

**`disruptions` replaces the old `disruption_events` table.** The old table only tracked lane disruptions as binary on/off. The new table stores rich event metadata (type, severity, duration, description) and covers all three disruption target types.

**`lanes.transport_cost`** is the economic cost of shipping one unit along that lane. It is used by the Planner when selecting which inbound lane to use (it prefers cheaper routes). It is not related to disruption risk.

**`demand.labor_capacity`** records the actual labor capacity used during the tick, not the base. This lets you compare actual vs base in the dashboard and SQL queries.

---

## Useful SQL Queries

Run these with `sqlite3 sim_state.db` or from Python:

```sql
-- All disruption events ever (including resolved)
SELECT tick_started, duration, type, target_id, severity, description, resolved
FROM disruptions ORDER BY tick_started;

-- Currently active disruptions
SELECT type, target_id, severity, description,
       (tick_started + duration - (SELECT MAX(tick) FROM log)) AS ticks_remaining
FROM disruptions WHERE resolved=0
  AND (tick_started + duration) > (SELECT MAX(tick) FROM log);

-- FC backlog history (where did backlogs accumulate?)
SELECT tick, node_id, orders, labor_capacity, shipped, backlog
FROM demand WHERE backlog > 0 ORDER BY tick, node_id;

-- Forecast accuracy summary per FC
SELECT node_id,
       ROUND(AVG(mae), 1) AS avg_mae,
       ROUND(MIN(mae), 1) AS best_mae,
       ROUND(MAX(mae), 1) AS worst_mae,
       COUNT(*) AS forecasts_made
FROM forecast WHERE mae IS NOT NULL
GROUP BY node_id;

-- IBCenter drain/refill history
SELECT tick, node_id, inventory FROM snapshots
WHERE node_id IN (1,2) ORDER BY tick;

-- Labor plans vs actual deployment
SELECT lp.tick_for, lp.node_id,
       lp.planned_labor,
       d.labor_capacity AS actual_labor,
       ROUND((d.labor_capacity * 1.0 / lp.planned_labor - 1) * 100, 1) AS flex_pct_used
FROM labor_plan lp
JOIN demand d ON d.node_id = lp.node_id AND d.tick = lp.tick_for
ORDER BY lp.tick_for, lp.node_id;

-- Transport capacity utilisation (planned vs what Planner actually shipped)
SELECT tp.tick_for, tp.lane_id, tp.planned_capacity
FROM transport_plan tp ORDER BY tp.tick_for, tp.lane_id;

-- Sessions total token cost (real Opus 4.6 API usage)
SELECT SUM(cost_eur) AS total_eur,
       SUM(input_tokens) AS total_input,
       SUM(output_tokens) AS total_output
FROM token_log;

-- Cost per tick
SELECT tick, ROUND(SUM(cost_eur), 5) AS eur_this_tick
FROM token_log GROUP BY tick ORDER BY tick;

-- Most impactful disruptions (high severity × long duration)
SELECT type, target_id, severity, duration,
       ROUND(severity * duration, 2) AS impact_score,
       description
FROM disruptions ORDER BY impact_score DESC;
```

---

## Project Structure

```
SupplyChain-Swarm-Sim/
├── .env                     # ANTHROPIC_API_KEY=sk-ant-... (you create this)
├── sim_state.db             # Single source of truth — all state lives here
├── env.py                   # DB schema, topology constants, init_db(), snapshot_tick()
├── token_utils.py           # Shared Claude API client, JSON extractor, token logging
├── tick_loop.py             # Orchestrator: runs all agents in order every 5 seconds
├── monitor.py               # Rich live terminal dashboard (read-only observer)
├── forecast_action.py       # Agent 1: Claude forecasts next-tick demand
├── staffing_action.py       # Agent 2: Claude plans labor & transport capacity
├── demand_action.py         # Agent 3: execute orders using planned labor (Claude flex)
├── supply_action.py         # Agent 4: Claude decides IBCenter supplier orders
├── disruptor_action.py      # Agent 5: chaos engine (Python) + Claude narrative
├── repair_action.py         # Agent 6: Claude prioritizes disruption repair
└── planner_action.py        # Agent 7: Claude routes FC replenishments via IBCenters
```

---

## Reset

```powershell
& "C:\Users\manum\anaconda3\python.exe" env.py
```

Deletes and recreates `sim_state.db` with the original baseline configuration at tick 0. All history, forecasts, disruption records, and token logs are erased.

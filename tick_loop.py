"""
SwarmChain-Sim · Tick Engine (v3 — rule-based + single Strategist LLM)

All 7 agent roles execute as fast deterministic rules.
One LLM call per tick reviews the full state and returns targeted overrides.
Scales to 100+ nodes / 10,000+ lanes with sub-10s tick times.

Usage:
    python tick_loop.py                    # infinite loop
    python tick_loop.py --ticks 10         # run 10 ticks
    python tick_loop.py --seed 42 --ticks 5  # deterministic benchmark
    python tick_loop.py --no-llm           # pure rule-based (no API calls)
"""
import sqlite3
import time
import sys
import os
import argparse
import random
import math
import json

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as _env
from token_utils import call_llm, extract_json, log_tokens, get_db_conn
from env import (
    BASE_DEMAND, FC_IDS, IB_IDS, LANE_TC_MAX, LANE_TC_BASE,
    LANE_TO_IB, INBOUND_LANES, IB_FC_WEIGHTS, MAX_EXTERNAL_INFLOW,
    DISRUPTION_PROBABILITY, MAX_ACTIVE_DISRUPTIONS,
)

DB_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_state.db")
PAUSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tick_pause")
INTERVAL   = 2  # seconds between ticks
FLEX_PCT   = 0.15

# Disruption scenario tables (inlined to avoid import side-effects)
LANE_SCENARIOS = [
    ("Carrier network failure",        0.65, 3),
    ("Severe weather — road closure",  0.90, 2),
    ("Port congestion",                0.40, 4),
    ("Bridge weight restriction",      1.00, 2),
    ("Fuel/truck shortage",            0.50, 3),
    ("Customs clearance delay",        0.35, 5),
]
IB_SCENARIOS = [
    ("Dock equipment breakdown",       0.55, 2),
    ("Inbound inspection backlog",     0.30, 3),
    ("Loading bay incident",           0.80, 2),
    ("Receiving crew unavailability",  0.45, 3),
]
FC_SCENARIOS = [
    ("Labor strike",                   0.80, 4),
    ("Power outage",                   1.00, 1),
    ("Workplace accident — partial",   0.60, 2),
    ("Conveyor/sort equipment failure",0.50, 3),
    ("IT system outage",               0.70, 2),
    ("Severe staff illness",           0.40, 4),
]


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def qprint(msg):
    """Quiet print — always flushes."""
    print(msg, flush=True)


def active_severity(cur, d_type, target_id, tick):
    cur.execute("""
        SELECT COALESCE(MAX(severity), 0) FROM disruptions
        WHERE type=? AND target_id=? AND resolved=0 AND (tick_started + duration) > ?
    """, (d_type, target_id, tick))
    return cur.fetchone()[0]


# ═══════════════════════════════════════════════════════════════
# RULE-BASED AGENTS (deterministic, instant)
# ═══════════════════════════════════════════════════════════════

def agent_forecast(cur, tick):
    """Exponential smoothing + seasonal. Returns {node_id: forecast}."""
    forecast_for = tick + 1
    cur.execute("SELECT COUNT(*) FROM forecast WHERE tick_created=? AND forecast_for_tick=?",
                (tick, forecast_for))
    if cur.fetchone()[0] > 0:
        return None  # already done

    seasonal = round(1.0 + 0.35 * math.sin(forecast_for * 0.4), 3)
    forecasts = {}
    for node_id, base in BASE_DEMAND.items():
        cur.execute("SELECT orders FROM demand WHERE node_id=? AND tick<=? ORDER BY tick DESC LIMIT 5",
                    (node_id, tick))
        records = [r[0] for r in cur.fetchall()]
        if not records:
            forecasts[node_id] = max(1, round(base * seasonal))
        elif len(records) == 1:
            forecasts[node_id] = max(1, round(records[0] * seasonal / max(0.5, 1.0 + 0.35 * math.sin(tick * 0.4))))
        else:
            vals = list(reversed(records))
            s = float(vals[0])
            for v in vals[1:]:
                s = 0.4 * v + 0.6 * s
            # Adjust for seasonal shift
            forecasts[node_id] = max(1, round(s * seasonal / max(0.5, 1.0 + 0.35 * math.sin(tick * 0.4))))

    parts = []
    for node_id, fval in forecasts.items():
        cur.execute(
            "INSERT OR IGNORE INTO forecast "
            "(tick_created, forecast_for_tick, node_id, forecast_demand, actual_demand, mae, method) "
            "VALUES (?,?,?,?,NULL,NULL,?)",
            (tick, forecast_for, node_id, float(fval), "exp_smooth_seasonal"))
        parts.append(f"FC{node_id}:{fval}")

    # IB outbound forecasts: expected dispatch = weighted sum of downstream FC forecasts
    for ib_id in IB_IDS:
        fc_weights = IB_FC_WEIGHTS.get(ib_id, {})
        ib_fcast = max(1, round(sum(
            forecasts.get(fc_id, max(1, round(BASE_DEMAND.get(fc_id, 70) * seasonal))) * w
            for fc_id, w in fc_weights.items()
        )))
        cur.execute(
            "INSERT OR IGNORE INTO forecast "
            "(tick_created, forecast_for_tick, node_id, forecast_demand, actual_demand, mae, method) "
            "VALUES (?,?,?,?,NULL,NULL,?)",
            (tick, forecast_for, ib_id, float(ib_fcast), "ib_outbound_forecast"))
        parts.append(f"IB{ib_id}:{ib_fcast}")

    action = f"Forecast for tick {forecast_for}: {' | '.join(parts)} [exp_smooth_seasonal]"
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Forecast", action, None))
    qprint(f"  [FORECAST] {action}")
    return forecasts


def agent_staffing(cur, tick):
    """Plan labor + transport for next tick. Rule: forecast × 1.1 buffer, capped at 1.3× base."""
    plan_for = tick + 1
    cur.execute("SELECT COUNT(*) FROM labor_plan WHERE tick_for=?", (plan_for,))
    if cur.fetchone()[0] > 0:
        return

    cur.execute("SELECT node_id, forecast_demand FROM forecast WHERE forecast_for_tick=? ORDER BY node_id",
                (plan_for,))
    forecasts = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("SELECT id, type, labor_capacity_base, inventory, safety_stock FROM nodes ORDER BY id")
    nodes = {r[0]: {"type": r[1], "labor_base": r[2], "inv": r[3], "safety": r[4]}
             for r in cur.fetchall()}

    labor_parts, trans_parts = [], []
    for nid, n in nodes.items():
        if n["type"] == "FulfillmentCenter":
            fc_demand = forecasts.get(nid, n["labor_base"])
            planned = min(round(fc_demand * 1.1), round(n["labor_base"] * 1.3))
        else:
            planned = n["labor_base"]

        # Reduce if disrupted
        sev = active_severity(cur, "fc_node" if n["type"] == "FulfillmentCenter" else "ib_node", nid, tick)
        if sev > 0:
            planned = round(planned * (1.0 - sev * 0.5))
        planned = max(0, planned)

        cur.execute("INSERT OR IGNORE INTO labor_plan (tick_planned, tick_for, node_id, planned_labor, flex_pct) "
                    "VALUES (?,?,?,?,?)", (tick, plan_for, nid, planned, FLEX_PCT))
        prefix = "IB" if n["type"] == "IBCenter" else "FC"
        labor_parts.append(f"{prefix}{nid}={planned}")

    for lid, tc_base in LANE_TC_BASE.items():
        tc_max = LANE_TC_MAX.get(lid, 350)
        planned = min(tc_base, tc_max)
        cur.execute("INSERT OR IGNORE INTO transport_plan (tick_planned, tick_for, lane_id, planned_capacity, flex_pct) "
                    "VALUES (?,?,?,?,?)", (tick, plan_for, lid, planned, FLEX_PCT))
        trans_parts.append(f"L{lid}={planned}")

    action = f"Staffing plan for tick {plan_for} | Labor: {' '.join(labor_parts)} | Transport: {' '.join(trans_parts)}"
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Staffing", action, None))
    qprint(f"  [STAFFING] {action}")


def agent_demand(cur, tick):
    """Execute customer orders. Flex UP if backlog > 0, else 1.0."""
    cur.execute("SELECT COUNT(*) FROM demand WHERE tick=?", (tick,))
    if cur.fetchone()[0] > 0:
        return

    seasonal = 1.0 + 0.35 * math.sin(tick * 0.4)

    # Manual demand modifiers
    cur.execute("SELECT node_id, multiplier FROM demand_modifiers WHERE active=1")
    modifiers = {r[0]: r[1] for r in cur.fetchall()}

    summary = []
    for node_id in FC_IDS:
        base = BASE_DEMAND.get(node_id, 70)
        orders = max(1, round(base * seasonal * random.uniform(0.75, 1.25)))
        mod = modifiers.get(node_id, 1.0)
        if mod != 1.0:
            orders = max(1, round(orders * mod))

        cur.execute("SELECT backlog FROM demand WHERE node_id=? ORDER BY tick DESC LIMIT 1", (node_id,))
        row = cur.fetchone()
        prev_backlog = row[0] if row else 0
        total_demand = orders + prev_backlog

        cur.execute("SELECT inventory, labor_capacity_base FROM nodes WHERE id=?", (node_id,))
        inv_row = cur.fetchone()
        inventory, labor_base = inv_row[0], inv_row[1]

        cur.execute("SELECT planned_labor, flex_pct FROM labor_plan WHERE tick_for=? AND node_id=?",
                    (tick, node_id))
        plan_row = cur.fetchone()

        fc_severity = active_severity(cur, "fc_node", node_id, tick)

        # Flex decision: UP if backlog, else neutral
        if plan_row:
            planned_labor, flex_pct = plan_row
            flex_mult = (1.0 + flex_pct) if prev_backlog > 0 else 1.0
            actual_labor = round(planned_labor * flex_mult)
        else:
            actual_labor = round(labor_base * random.uniform(0.85, 1.10))

        if fc_severity > 0:
            actual_labor = round(actual_labor * (1.0 - fc_severity))
        actual_labor = max(0, actual_labor)

        shipped = min(inventory, total_demand, actual_labor)
        new_backlog = total_demand - shipped

        cur.execute("UPDATE nodes SET inventory = inventory - ? WHERE id=?", (shipped, node_id))
        cur.execute("INSERT INTO demand (tick, node_id, orders, shipped, backlog, labor_capacity) VALUES (?,?,?,?,?,?)",
                    (tick, node_id, orders, shipped, new_backlog, actual_labor))
        cur.execute("UPDATE forecast SET actual_demand=?, mae=ABS(forecast_demand-?) WHERE forecast_for_tick=? AND node_id=?",
                    (orders, orders, tick, node_id))

        notes = []
        if actual_labor == shipped < total_demand and inventory >= actual_labor:
            notes.append("LABOR_CAP")
        elif inventory < total_demand and shipped == inventory:
            notes.append("STOCKOUT")
        if fc_severity > 0:
            notes.append(f"DISRUPTION_sev={fc_severity:.0%}")
        if mod != 1.0:
            notes.append(f"DEMAND_MOD={mod:.0%}")
        note_str = f" [{';'.join(notes)}]" if notes else ""

        summary.append(
            f"FC{node_id}: orders={orders}+bl={prev_backlog} labor={actual_labor}/{labor_base}"
            f" -> shipped={shipped} backlog={new_backlog} inv={inventory - shipped}{note_str}")

    action = " | ".join(summary)
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Demand", action, None))
    for line in summary:
        qprint(f"  [DEMAND] {line}")


def agent_supply(cur, tick):
    """Refill IBCenters. Rule: target = 3× weighted downstream demand, gap-fill."""
    cur.execute("SELECT COUNT(*) FROM log WHERE agent_name='Supply' AND tick=?", (tick,))
    if cur.fetchone()[0] > 0:
        return

    COVERAGE_TICKS = 3
    cur.execute("SELECT id, type, inventory, capacity, safety_stock FROM nodes ORDER BY id")
    nodes = {r[0]: {"type": r[1], "inv": r[2], "cap": r[3], "safety": r[4]} for r in cur.fetchall()}

    parts = []
    for ib_id in IB_IDS:
        n = nodes[ib_id]
        space = n["cap"] - n["inv"]
        fc_weights = IB_FC_WEIGHTS.get(ib_id, {})
        total_wt = 0.0
        for fc_id, weight in fc_weights.items():
            cur.execute("SELECT AVG(forecast_demand) FROM forecast WHERE node_id=? AND forecast_for_tick > ? AND forecast_for_tick <= ?",
                        (fc_id, tick, tick + COVERAGE_TICKS))
            avg_row = cur.fetchone()
            avg = avg_row[0] if avg_row and avg_row[0] else BASE_DEMAND.get(fc_id, 70)
            total_wt += avg * weight
        target = round(total_wt * COVERAGE_TICKS)
        gap = target - n["inv"]
        qty = max(0, min(gap, MAX_EXTERNAL_INFLOW.get(ib_id, 300), space))

        if qty > 0:
            cur.execute("UPDATE nodes SET inventory = inventory + ? WHERE id=?", (qty, ib_id))
        parts.append(f"IB{ib_id}: +{qty} (inv {n['inv']} -> {n['inv'] + qty})")

    action = " | ".join(parts)
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Supply", action, None))
    for p in parts:
        qprint(f"  [SUPPLY] {p}")


def agent_disruptor(cur, tick):
    """Probabilistic disruption with pre-defined scenario strings (no LLM)."""
    cur.execute("SELECT COUNT(*) FROM log WHERE agent_name='Disruptor' AND tick=?", (tick,))
    if cur.fetchone()[0] > 0:
        return

    if random.random() > DISRUPTION_PROBABILITY:
        msg = f"No disruption event this tick (rolled > {DISRUPTION_PROBABILITY:.0%})."
        cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                    (tick, "Disruptor", msg, None))
        qprint(f"  [DISRUPTOR] {msg}")
        return

    cur.execute("SELECT COUNT(*) FROM disruptions WHERE resolved=0 AND (tick_started + duration) > ?", (tick,))
    if cur.fetchone()[0] >= MAX_ACTIVE_DISRUPTIONS:
        msg = f"Disruption cap reached ({MAX_ACTIVE_DISRUPTIONS}). No new event."
        cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                    (tick, "Disruptor", msg, None))
        qprint(f"  [DISRUPTOR] {msg}")
        return

    roll = random.random()
    category = "lane" if roll < 0.50 else ("ib_node" if roll < 0.70 else "fc_node")
    target_id, desc_base, severity, duration = None, None, None, None

    if category == "lane":
        cur.execute("SELECT id, origin, destination FROM lanes WHERE LOWER(status)='active'")
        candidates = cur.fetchall()
        if not candidates:
            category = "fc_node"
        else:
            t = random.choice(candidates)
            target_id = t[0]
            desc_base, severity, duration = random.choice(LANE_SCENARIOS)
            cur.execute("UPDATE lanes SET status='disrupted' WHERE id=?", (target_id,))

    if category == "ib_node":
        cur.execute("SELECT id FROM nodes WHERE type='IBCenter'")
        candidates = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT target_id FROM disruptions WHERE type='ib_node' AND resolved=0 AND (tick_started+duration)>?", (tick,))
        already = {r[0] for r in cur.fetchall()}
        candidates = [c for c in candidates if c not in already]
        if not candidates:
            category = "fc_node"
        else:
            target_id = random.choice(candidates)
            desc_base, severity, duration = random.choice(IB_SCENARIOS)

    if category == "fc_node":
        cur.execute("SELECT id FROM nodes WHERE type='FulfillmentCenter'")
        candidates = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT target_id FROM disruptions WHERE type='fc_node' AND resolved=0 AND (tick_started+duration)>?", (tick,))
        already = {r[0] for r in cur.fetchall()}
        candidates = [c for c in candidates if c not in already]
        if not candidates:
            msg = f"All targets already disrupted. No new event."
            cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                        (tick, "Disruptor", msg, None))
            qprint(f"  [DISRUPTOR] {msg}")
            return
        target_id = random.choice(candidates)
        desc_base, severity, duration = random.choice(FC_SCENARIOS)

    cur.execute("INSERT INTO disruptions (tick_started, duration, type, target_id, severity, description) VALUES (?,?,?,?,?,?)",
                (tick, duration, category, target_id, severity, desc_base))

    action = f"[{category.upper()}] {desc_base} — target={target_id} sev={severity:.0%} dur={duration}t (expires tick {tick + duration})"
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Disruptor", action, None))
    qprint(f"  [DISRUPTOR] {action}")


def agent_repair(cur, tick):
    """Auto-expire + resolve highest-severity prior-tick disruption."""
    cur.execute("SELECT COUNT(*) FROM log WHERE agent_name='Repair' AND tick=?", (tick,))
    if cur.fetchone()[0] > 0:
        return

    cur.execute("UPDATE disruptions SET resolved=1 WHERE resolved=0 AND (tick_started + duration) <= ?", (tick,))

    cur.execute("""
        SELECT id, type, target_id, severity, description, duration, tick_started,
               (tick_started + duration - ?) AS ticks_remaining
        FROM disruptions WHERE resolved=0 AND tick_started < ? AND (tick_started + duration) > ?
        ORDER BY severity DESC
    """, (tick, tick, tick))
    candidates = cur.fetchall()

    if not candidates:
        msg = f"No active prior-tick disruptions to repair."
        cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                    (tick, "Repair", msg, None))
        qprint(f"  [REPAIR] {msg}")
        return

    # Pick highest severity
    chosen = candidates[0]
    d_id, d_type, target_id, severity, description, duration, tick_started, ticks_remaining = chosen

    cur.execute("UPDATE disruptions SET resolved=1 WHERE id=?", (d_id,))
    if d_type == "lane":
        cur.execute("UPDATE lanes SET status='active' WHERE id=? AND LOWER(status)='disrupted'", (target_id,))

    rationale = "highest severity"
    action = f"Resolved [{d_type.upper()}] {description} (sev={severity:.0%} {ticks_remaining}t remaining) — {rationale}"
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Repair", action, None))
    qprint(f"  [REPAIR] {action}")


def agent_planner(cur, tick):
    """Replenish FCs below safety stock from IBCenters via cheapest active lane."""
    cur.execute("SELECT COUNT(*) FROM log WHERE agent_name='Planner' AND tick=?", (tick,))
    if cur.fetchone()[0] > 0:
        return

    cur.execute("SELECT id, inventory, capacity, safety_stock FROM nodes WHERE type='FulfillmentCenter' ORDER BY id")
    fcs = {r[0]: {"inv": r[1], "cap": r[2], "safety": r[3]} for r in cur.fetchall()}

    cur.execute("SELECT id, inventory, labor_capacity_base FROM nodes WHERE type='IBCenter' ORDER BY id")
    ibs = {r[0]: {"inv": r[1], "labor_base": r[2]} for r in cur.fetchall()}

    cur.execute("SELECT id, origin, destination, transport_cost, status FROM lanes ORDER BY id")
    lanes = {r[0]: {"origin": r[1], "dest": r[2], "tc": r[3], "status": r[4]} for r in cur.fetchall()}

    cur.execute("SELECT lane_id, planned_capacity, flex_pct FROM transport_plan WHERE tick_for=?", (tick,))
    tplan = {r[0]: {"cap": r[1], "flex": r[2]} for r in cur.fetchall()}

    cur.execute("SELECT node_id, planned_labor, flex_pct FROM labor_plan WHERE tick_for=? AND node_id IN ({})".format(
        ",".join(str(i) for i in IB_IDS)), (tick,))
    ib_lplan = {r[0]: {"labor": r[1], "flex": r[2]} for r in cur.fetchall()}

    fcs_below = {nid: d for nid, d in fcs.items() if d["inv"] < d["safety"]}
    if not fcs_below:
        msg = f"All FC nodes above safety_stock. No replenishment."
        cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                    (tick, "Planner", msg, None))
        qprint(f"  [PLANNER] {msg}")
        return

    ib_labor_used = {ib_id: 0 for ib_id in ibs}
    actions = []

    for nid in sorted(fcs_below.keys()):
        d = fcs_below[nid]
        gap = d["cap"] - d["inv"]
        max_repl = min(gap, round(d["cap"] * 0.30))

        # Find cheapest active inbound lane
        lane_ids = INBOUND_LANES.get(nid, [])
        active_lanes = [lid for lid in lane_ids if lanes.get(lid, {}).get("status", "").lower() == "active"]
        if active_lanes:
            best_lane = min(active_lanes, key=lambda l: lanes[l]["tc"])
        elif lane_ids:
            best_lane = lane_ids[0]
            max_repl = max_repl // 2  # halve if all lanes disrupted
        else:
            continue

        ib_id = LANE_TO_IB.get(best_lane)
        if ib_id is None or ibs[ib_id]["inv"] <= 0:
            continue

        qty = max_repl
        l_sev = active_severity(cur, "lane", best_lane, tick)
        if l_sev > 0:
            qty = round(qty * (1.0 - l_sev))

        tp = tplan.get(best_lane)
        if tp:
            tc_max = round(tp["cap"] * (1.0 + tp["flex"]))
            qty = min(qty, tc_max)

        ib_sev = active_severity(cur, "ib_node", ib_id, tick)
        ib_dispatch_max = round(ibs[ib_id]["inv"] * (1.0 - ib_sev))

        lp = ib_lplan.get(ib_id)
        if lp:
            ib_labor_max = round(lp["labor"] * (1.0 + lp["flex"]))
            ib_labor_remaining = ib_labor_max - ib_labor_used[ib_id]
            if ib_labor_remaining <= 0:
                continue
            qty = min(qty, ib_labor_remaining)

        qty = min(qty, ibs[ib_id]["inv"], ib_dispatch_max)
        if qty <= 0:
            continue

        cur.execute("UPDATE nodes SET inventory = inventory + ? WHERE id=?", (qty, nid))
        cur.execute("UPDATE nodes SET inventory = inventory - ? WHERE id=?", (qty, ib_id))
        cur.execute("INSERT INTO transport (tick, lane_id, qty) VALUES (?, ?, ?)", (tick, best_lane, qty))
        ibs[ib_id]["inv"] -= qty
        ib_labor_used[ib_id] += qty

        actions.append(f"FC{nid}: +{qty} from IB{ib_id} via L{best_lane} (inv {d['inv']} -> {d['inv'] + qty}, safety={d['safety']})")
        d["inv"] += qty

    if not actions:
        actions = ["No replenishments executed (all constrained)."]

    action = " | ".join(actions)
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Planner", action, None))
    for a in actions:
        qprint(f"  [PLANNER] {a}")


# ═══════════════════════════════════════════════════════════════
# STRATEGIST (single LLM call — reviews state, returns overrides)
# ═══════════════════════════════════════════════════════════════

def run_strategist(cur, tick):
    """Single LLM call per tick. Reviews network state after rule-based execution.
    Returns targeted overrides: extra supply orders, priority repairs, demand warnings.
    Applied NEXT tick via the override mechanism."""

    # Build compact state snapshot
    cur.execute("SELECT id, type, inventory, capacity, safety_stock FROM nodes ORDER BY id")
    nodes = cur.fetchall()

    cur.execute("SELECT id, status FROM lanes ORDER BY id")
    lane_states = cur.fetchall()

    cur.execute("SELECT node_id, orders, shipped, backlog FROM demand WHERE tick=? ORDER BY node_id", (tick,))
    demand_rows = cur.fetchall()

    cur.execute("""
        SELECT type, target_id, severity, description, (tick_started+duration-?) AS remaining
        FROM disruptions WHERE resolved=0 AND (tick_started+duration) > ?
    """, (tick, tick))
    disruptions = cur.fetchall()

    cur.execute("SELECT node_id, forecast_demand FROM forecast WHERE forecast_for_tick=? ORDER BY node_id", (tick + 1,))
    forecasts = cur.fetchall()

    # Compact text (scales well — ~2 lines per node, ~1 line per lane)
    node_lines = " | ".join(f"{'IB' if r[1]=='IBCenter' else 'FC'}{r[0]}:inv={r[2]}/{r[3]} ss={r[4]}" for r in nodes)
    lane_lines = " ".join(f"L{r[0]}:{r[1]}" for r in lane_states)
    demand_lines = " ".join(f"FC{r[0]}:ord={r[1]} ship={r[2]} bl={r[3]}" for r in demand_rows) if demand_rows else "none"
    disrup_lines = " | ".join(f"{r[0]} t={r[1]} sev={r[2]:.0%} rem={r[4]}t: {r[3]}" for r in disruptions) if disruptions else "none"
    forecast_lines = " ".join(f"FC{r[0]}:{r[1]:.0f}" for r in forecasts) if forecasts else "none"

    prompt = (
        f"Tick {tick} supply chain state. Review and suggest strategic overrides.\n\n"
        f"Nodes: {node_lines}\n"
        f"Lanes: {lane_lines}\n"
        f"Demand: {demand_lines}\n"
        f"Forecasts(t+1): {forecast_lines}\n"
        f"Disruptions: {disrup_lines}\n\n"
        f"You may return overrides as JSON. If no action needed, return empty object {{}}.\n"
        f"Possible overrides:\n"
        f'- "extra_supply": {{"IB<id>": <int>}} — additional external orders\n'
        f'- "priority_repair": <disruption_target_id> — override which disruption to fix next tick\n'
        f'- "demand_warning": "<text>" — flag for monitoring dashboard\n'
        f"Return ONLY valid JSON."
    )

    text, in_tok, out_tok, model_name, reasoning = call_llm(prompt, "Strategist", max_tokens=128, thinking_budget=0)

    try:
        overrides = extract_json(text)
    except Exception:
        overrides = {}

    # Apply extra_supply immediately
    extra = overrides.get("extra_supply", {})
    if extra:
        for key, qty in extra.items():
            try:
                ib_id = int(str(key).replace("IB", ""))
                qty = max(0, int(qty))
                if qty > 0:
                    cur.execute("SELECT capacity, inventory FROM nodes WHERE id=?", (ib_id,))
                    row = cur.fetchone()
                    if row:
                        space = row[0] - row[1]
                        qty = min(qty, MAX_EXTERNAL_INFLOW.get(ib_id, 300), space)
                        if qty > 0:
                            cur.execute("UPDATE nodes SET inventory = inventory + ? WHERE id=?", (qty, ib_id))
                            qprint(f"  [STRATEGIST] Extra supply: IB{ib_id} +{qty}")
            except (ValueError, TypeError):
                pass

    warning = overrides.get("demand_warning", "")
    if warning:
        qprint(f"  [STRATEGIST] Warning: {warning}")

    action = f"Overrides: {json.dumps(overrides)}" if overrides else "No overrides."
    cur.execute("INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
                (tick, "Strategist", action, reasoning or None))
    qprint(f"  [STRATEGIST] {action}")


# ═══════════════════════════════════════════════════════════════
# FINANCIALS
# ═══════════════════════════════════════════════════════════════

def record_financials(cur, tick):
    total_labor_cost = 0.0
    cur.execute("""
        SELECT d.labor_capacity, n.hourly_labor_cost, n.units_per_hour
        FROM demand d JOIN nodes n ON n.id = d.node_id WHERE d.tick = ?
    """, (tick,))
    for used_qty, hourly_rate, uph in cur.fetchall():
        if uph > 0:
            total_labor_cost += (used_qty / uph) * hourly_rate

    cur.execute("""
        SELECT SUM(t.qty), n.hourly_labor_cost, n.units_per_hour
        FROM transport t JOIN lanes l ON l.id = t.lane_id JOIN nodes n ON n.id = l.origin
        WHERE t.tick = ? GROUP BY l.origin
    """, (tick,))
    for shipped_qty, hourly_rate, uph in cur.fetchall():
        if shipped_qty and uph > 0:
            total_labor_cost += (shipped_qty / uph) * hourly_rate

    total_transport_cost = 0.0
    cur.execute("SELECT SUM(t.qty * l.transport_cost) FROM transport t JOIN lanes l ON l.id = t.lane_id WHERE t.tick = ?", (tick,))
    row = cur.fetchone()
    if row and row[0]:
        total_transport_cost = row[0]

    cur.execute("SELECT SUM(shipped) FROM demand WHERE tick = ?", (tick,))
    row = cur.fetchone()
    total_shipped = row[0] if row and row[0] else 0

    total_cost = total_labor_cost + total_transport_cost
    cpu = (total_cost / total_shipped) if total_shipped > 0 else 0.0

    cur.execute("INSERT OR IGNORE INTO financials (tick, total_labor_cost, total_transport_cost, total_units_shipped, cost_per_unit_shipped) VALUES (?,?,?,?,?)",
                (tick, total_labor_cost, total_transport_cost, total_shipped, cpu))


# ═══════════════════════════════════════════════════════════════
# TICK ORCHESTRATION
# ═══════════════════════════════════════════════════════════════

def get_next_tick():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(tick) FROM log")
    row = cur.fetchone()
    conn.close()
    return (row[0] + 1) if row[0] is not None else 1


def run_tick(tick, tick_seed, use_llm=True):
    """Execute one tick: all rule-based agents + optional Strategist LLM call.
    Single DB connection, single transaction — no lock contention."""
    if tick_seed is not None:
        random.seed(tick_seed)

    t0 = time.time()
    conn = get_db_conn()
    try:
        cur = conn.cursor()

        # All rule-based agents (instant, deterministic)
        agent_forecast(cur, tick)
        agent_staffing(cur, tick)
        agent_disruptor(cur, tick)
        agent_repair(cur, tick)
        conn.commit()  # Commit planning + events before demand reads them

        agent_demand(cur, tick)
        agent_supply(cur, tick)
        agent_planner(cur, tick)

        # Update IB actual outbound in forecast table (sum of transport qty dispatched this tick)
        ib_ids_sql = ",".join(str(i) for i in IB_IDS)
        cur.execute(f"""
            UPDATE forecast
            SET actual_demand = COALESCE((
                    SELECT SUM(t.qty) FROM transport t
                    JOIN lanes l ON l.id = t.lane_id
                    WHERE t.tick = forecast.forecast_for_tick AND l.origin = forecast.node_id
                ), 0),
                mae = ABS(forecast_demand - COALESCE((
                    SELECT SUM(t.qty) FROM transport t
                    JOIN lanes l ON l.id = t.lane_id
                    WHERE t.tick = forecast.forecast_for_tick AND l.origin = forecast.node_id
                ), 0))
            WHERE forecast_for_tick = ? AND node_id IN ({ib_ids_sql}) AND actual_demand IS NULL
        """, (tick,))
        conn.commit()  # Commit execution phase

        # Snapshot + financials
        cur.execute("INSERT OR IGNORE INTO snapshots (tick, node_id, inventory) SELECT ?, id, inventory FROM nodes", (tick,))
        record_financials(cur, tick)
        conn.commit()

        # Strategist LLM call (optional — only cost is ~4s latency)
        if use_llm:
            try:
                run_strategist(cur, tick)
                conn.commit()
            except Exception as e:
                qprint(f"  [STRATEGIST] LLM error (non-fatal): {str(e)[:100]}")

        elapsed = time.time() - t0
        qprint(f"  ── tick {tick} completed in {elapsed:.1f}s ──")
    finally:
        conn.close()


def print_separator(tick):
    qprint(f"\n{'='*54}")
    qprint(f"  TICK {tick:>4}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    qprint(f"{'='*54}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SwarmChain-Sim tick engine (v3 rule-based + strategist)")
    parser.add_argument("--seed", type=int, default=None, help="Fixed random seed for deterministic runs")
    parser.add_argument("--ticks", type=int, default=0, help="Run a fixed number of ticks then stop (0 = infinite)")
    parser.add_argument("--no-llm", action="store_true", help="Pure rule-based mode (no API calls)")
    args = parser.parse_args()

    use_llm = not args.no_llm
    mode = "rule-based + Strategist LLM" if use_llm else "pure rule-based (no LLM)"
    qprint(f"Tick loop started (v3) -- interval: {INTERVAL}s -- mode: {mode}")
    qprint(f"Order: Forecast > Staffing > Disruptor > Repair > Demand > Supply > Planner" + (" > Strategist" if use_llm else ""))
    if args.seed is not None:
        qprint(f"Seed: {args.seed} (deterministic mode)")
    if args.ticks > 0:
        qprint(f"Will run {args.ticks} ticks then stop.")
    qprint("Press Ctrl+C to stop.\n")

    tick_count = 0
    try:
        while True:
            # Pause when .tick_pause file exists (manual mode set via dashboard)
            if args.ticks == 0:
                while os.path.exists(PAUSE_FILE):
                    time.sleep(0.5)
            tick = get_next_tick()
            tick_seed = (args.seed + tick) if args.seed is not None else None
            print_separator(tick)
            try:
                run_tick(tick, tick_seed, use_llm=use_llm)
                tick_count += 1
            except Exception as e:
                qprint(f"\n  [TICK ERROR] Tick {tick} failed: {e}")
                qprint(f"  Retrying in 5s...")
                time.sleep(5)
                continue

            if args.ticks > 0 and tick_count >= args.ticks:
                qprint(f"\nCompleted {tick_count} ticks. Stopping.")
                break
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        qprint(f"\nTick loop stopped at tick {get_next_tick() - 1}.")

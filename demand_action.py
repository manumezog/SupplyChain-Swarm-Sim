"""
Demand agent: executes customer order fulfilment for this tick.
Labor deployment is constrained by the staffing plan from the prior tick (±15% flex).
Claude decides the flex direction based on actual vs planned demand signals.
shipped = min(inventory, total_demand, actual_labor)
"""
import sqlite3
import time
import random
import math
import sys

from token_utils import call_claude, extract_json, log_tokens

DB_PATH     = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
TICK        = int(sys.argv[1]) if len(sys.argv) > 1 else 1
AGENT       = "Demand"
MAX_RETRIES = 5

BASE_DEMAND = {3: 70, 4: 90, 5: 55}


def with_backoff(fn):
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                time.sleep(0.1 * (2 ** attempt) + random.uniform(0, 0.05))
            else:
                raise


def run():
    def execute():
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM demand WHERE tick=?", (TICK,))
            if cur.fetchone()[0] > 0:
                print(f"Demand already recorded at tick {TICK}. Skipping.")
                return

            seasonal = 1.0 + 0.35 * math.sin(TICK * 0.4)

            # --- Gather all FC data first ---
            fc_data = {}
            for node_id, base in BASE_DEMAND.items():
                orders = max(1, round(base * seasonal * random.uniform(0.75, 1.25)))

                cur.execute(
                    "SELECT backlog FROM demand WHERE node_id=? ORDER BY tick DESC LIMIT 1",
                    (node_id,),
                )
                row = cur.fetchone()
                prev_backlog = row[0] if row else 0
                total_demand = orders + prev_backlog

                cur.execute(
                    "SELECT inventory, labor_capacity_base FROM nodes WHERE id=?", (node_id,)
                )
                inv_row = cur.fetchone()
                inventory, labor_base = inv_row[0], inv_row[1]

                # Get staffing plan for this tick (written by Staffing at tick-1)
                cur.execute(
                    "SELECT planned_labor, flex_pct FROM labor_plan WHERE tick_for=? AND node_id=?",
                    (TICK, node_id),
                )
                plan_row = cur.fetchone()

                # Active fc_node disruption
                cur.execute("""
                    SELECT COALESCE(MAX(severity), 0) FROM disruptions
                    WHERE type='fc_node' AND target_id=? AND resolved=0
                      AND (tick_started + duration) > ?
                """, (node_id, TICK))
                fc_severity = cur.fetchone()[0]

                fc_data[node_id] = {
                    "orders": orders,
                    "prev_backlog": prev_backlog,
                    "total_demand": total_demand,
                    "inventory": inventory,
                    "labor_base": labor_base,
                    "plan_row": plan_row,   # (planned_labor, flex_pct) or None
                    "fc_severity": fc_severity,
                }

            # --- Ask Claude to decide labor flex per FC ---
            fc_context_lines = []
            for nid, d in fc_data.items():
                plan_info = (
                    f"planned_labor={d['plan_row'][0]} flex_pct={d['plan_row'][1]:.0%}"
                    if d["plan_row"] else "no_plan"
                )
                fc_context_lines.append(
                    f"FC{nid}: orders={d['orders']} backlog={d['prev_backlog']} "
                    f"total_demand={d['total_demand']} inventory={d['inventory']} "
                    f"{plan_info} disruption_sev={d['fc_severity']:.0%}"
                )

            prompt = (
                f"Tick {TICK}. Decide labor deployment flex for each FC.\n\n"
                + "\n".join(fc_context_lines) + "\n\n"
                f"For FCs with a staffing plan: choose flex multiplier within "
                f"[1-flex_pct, 1+flex_pct] of planned_labor. "
                f"Flex UP if total_demand > planned_labor or backlog > 0. "
                f"Flex DOWN if total_demand << planned_labor.\n"
                f"For FCs with no_plan: use 1.0 (labor_base will be used with random variation).\n\n"
                f"Return ONLY valid JSON: "
                f'{{ "3": <float>, "4": <float>, "5": <float> }}'
            )

            text, in_tok, out_tok = call_claude(prompt, max_tokens=64)

            try:
                flex_decisions = extract_json(text)
                flex_map = {int(k): float(v) for k, v in flex_decisions.items()}
            except Exception:
                flex_map = {}

            # --- Execute demand fulfilment ---
            summary = []
            for node_id, d in fc_data.items():
                flex_mult = flex_map.get(node_id, 1.0)

                if d["plan_row"] is not None:
                    planned_labor, flex_pct = d["plan_row"]
                    # Clamp flex to allowed range
                    flex_mult = max(1.0 - flex_pct, min(1.0 + flex_pct, flex_mult))
                    actual_labor = round(planned_labor * flex_mult)
                else:
                    # No plan yet (early ticks) — random variation around base
                    actual_labor = round(d["labor_base"] * random.uniform(0.70, 1.10))

                # Apply disruption penalty on top
                if d["fc_severity"] > 0:
                    actual_labor = round(actual_labor * (1.0 - d["fc_severity"]))
                actual_labor = max(0, actual_labor)

                shipped = min(d["inventory"], d["total_demand"], actual_labor)
                new_backlog = d["total_demand"] - shipped

                cur.execute(
                    "UPDATE nodes SET inventory = inventory - ? WHERE id=?",
                    (shipped, node_id),
                )
                cur.execute(
                    "INSERT INTO demand (tick, node_id, orders, shipped, backlog, labor_capacity) "
                    "VALUES (?,?,?,?,?,?)",
                    (TICK, node_id, d["orders"], shipped, new_backlog, actual_labor),
                )
                cur.execute(
                    "UPDATE forecast SET actual_demand=?, mae=ABS(forecast_demand-?) "
                    "WHERE forecast_for_tick=? AND node_id=?",
                    (d["orders"], d["orders"], TICK, node_id),
                )

                notes = []
                if actual_labor == shipped < d["total_demand"] and d["inventory"] >= actual_labor:
                    notes.append("LABOR_CAP")
                elif d["inventory"] < d["total_demand"] and shipped == d["inventory"]:
                    notes.append("STOCKOUT")
                if d["fc_severity"] > 0:
                    notes.append(f"DISRUPTION_sev={d['fc_severity']:.0%}")

                note_str = f" [{';'.join(notes)}]" if notes else ""
                summary.append(
                    f"FC{node_id}: orders={d['orders']}+bl={d['prev_backlog']}"
                    f" labor={actual_labor}/{d['labor_base']}"
                    f" -> shipped={shipped} backlog={new_backlog}"
                    f" inv={d['inventory'] - shipped}{note_str}"
                )

            action_text = " | ".join(summary)
            cur.execute(
                "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                (TICK, AGENT, action_text),
            )
            log_tokens(cur, TICK, AGENT, in_tok, out_tok)
            conn.commit()
            for line in summary:
                print(f"  {line}")

        finally:
            conn.close()

    with_backoff(execute)


if __name__ == "__main__":
    run()

"""
Staffing agent: plans labor and transport capacity for the NEXT tick.
Runs after Forecast. Claude sees demand forecasts and current network state,
then decides how much labor to staff at each node and how much transport
capacity to book on each lane. This plan constrains Demand and Planner
in the next tick (with ±15% flex allowed at execution time).
"""
import sqlite3
import time
import random
import sys

from token_utils import call_claude, extract_json, log_tokens

DB_PATH     = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
TICK        = int(sys.argv[1]) if len(sys.argv) > 1 else 1
AGENT       = "Staffing"
MAX_RETRIES = 5
FLEX_PCT    = 0.15   # ±15% allowed at execution vs plan

# Physical upper bounds on transport capacity per lane
LANE_TC_MAX = {1: 350, 2: 300, 3: 280, 4: 250}
# Baseline reference (used when Claude falls back)
LANE_TC_BASE = {1: 200, 2: 180, 3: 160, 4: 150}


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
            plan_for = TICK + 1

            cur.execute("SELECT COUNT(*) FROM labor_plan WHERE tick_for=?", (plan_for,))
            if cur.fetchone()[0] > 0:
                print(f"Staffing plan already exists for tick {plan_for}. Skipping.")
                return

            # Forecasts for plan_for
            cur.execute(
                "SELECT node_id, forecast_demand FROM forecast "
                "WHERE forecast_for_tick=? ORDER BY node_id",
                (plan_for,),
            )
            forecasts = {r[0]: r[1] for r in cur.fetchall()}

            # Node state
            cur.execute(
                "SELECT id, type, labor_capacity_base, inventory, safety_stock FROM nodes ORDER BY id"
            )
            nodes = {
                r[0]: {"type": r[1], "labor_base": r[2], "inventory": r[3], "safety": r[4]}
                for r in cur.fetchall()
            }

            # Latest backlogs per FC
            cur.execute("SELECT node_id, backlog FROM demand WHERE tick=?", (TICK,))
            backlogs = {r[0]: r[1] for r in cur.fetchall()}

            # Active disruptions
            cur.execute("""
                SELECT type, target_id, severity, description,
                       (tick_started + duration - ?) AS remaining
                FROM disruptions
                WHERE resolved=0 AND (tick_started + duration) > ?
            """, (TICK, TICK))
            disruptions = cur.fetchall()
            disrup_text = (
                "; ".join(
                    f"[{r[0]}] target={r[1]} sev={r[2]:.0%} rem={r[4]}t: {r[3]}"
                    for r in disruptions
                ) or "none"
            )

            fc_lines, ib_lines = [], []
            for nid in [3, 4, 5]:
                n = nodes[nid]
                fc_lines.append(
                    f"FC{nid}: forecast={forecasts.get(nid,'N/A')} "
                    f"backlog={backlogs.get(nid, 0)} "
                    f"inv={n['inventory']}/{nodes[nid]['type'][:2]} "
                    f"labor_base={n['labor_base']}"
                )
            for nid in [1, 2]:
                n = nodes[nid]
                ib_lines.append(
                    f"IB{nid}: inv={n['inventory']} safety={n['safety']} "
                    f"labor_base={n['labor_base']}"
                )

            lane_info = (
                f"L1(IB1->FC3) max={LANE_TC_MAX[1]}, "
                f"L2(IB1->FC4) max={LANE_TC_MAX[2]}, "
                f"L3(IB2->FC4) max={LANE_TC_MAX[3]}, "
                f"L4(IB2->FC5) max={LANE_TC_MAX[4]}"
            )

            prompt = (
                f"Tick {TICK}. Plan labor and transport capacity for tick {plan_for}.\n\n"
                f"Fulfillment Centers:\n" + "\n".join(fc_lines) + "\n\n"
                f"Inbound Centers (IB labor = max outbound dispatch units/tick):\n"
                + "\n".join(ib_lines) + "\n\n"
                f"Lanes (physical max): {lane_info}\n"
                f"Active disruptions: {disrup_text}\n\n"
                f"Guidelines:\n"
                f"- FC labor: units that can be shipped per tick. "
                f"Plan >= forecast + 10% buffer. Max ~1.3x labor_base.\n"
                f"- IB labor: total outbound units that IB can dispatch per tick across all lanes.\n"
                f"- Transport capacity: units per lane per tick. "
                f"Plan based on expected replenishment needs.\n"
                f"- Reduce plans proportionally for nodes with active disruptions.\n"
                f"- Plans can be adjusted ±{int(FLEX_PCT*100)}% at execution time.\n\n"
                f"Return ONLY valid JSON:\n"
                f'{{ "labor": {{"1": <IB1>, "2": <IB2>, "3": <FC3>, "4": <FC4>, "5": <FC5>}}, '
                f'"transport": {{"1": <L1>, "2": <L2>, "3": <L3>, "4": <L4>}} }}'
            )

            text, in_tok, out_tok = call_claude(prompt, max_tokens=2048)

            try:
                data = extract_json(text)
                labor_plan = {int(k): int(v) for k, v in data.get("labor", {}).items()}
                transport_plan = {int(k): int(v) for k, v in data.get("transport", {}).items()}
            except Exception:
                # Fallback: 110% of forecast for FC, baseline for IB and transport
                labor_plan = {
                    1: nodes[1]["labor_base"],
                    2: nodes[2]["labor_base"],
                    3: round((forecasts.get(3, nodes[3]["labor_base"]) or nodes[3]["labor_base"]) * 1.1),
                    4: round((forecasts.get(4, nodes[4]["labor_base"]) or nodes[4]["labor_base"]) * 1.1),
                    5: round((forecasts.get(5, nodes[5]["labor_base"]) or nodes[5]["labor_base"]) * 1.1),
                }
                transport_plan = dict(LANE_TC_BASE)

            labor_parts, trans_parts = [], []
            for node_id, planned in labor_plan.items():
                planned = max(0, planned)
                cur.execute(
                    "INSERT OR IGNORE INTO labor_plan "
                    "(tick_planned, tick_for, node_id, planned_labor, flex_pct) "
                    "VALUES (?,?,?,?,?)",
                    (TICK, plan_for, node_id, planned, FLEX_PCT),
                )
                prefix = "IB" if node_id <= 2 else "FC"
                labor_parts.append(f"{prefix}{node_id}={planned}")

            for lane_id, planned in transport_plan.items():
                capped = min(LANE_TC_MAX.get(lane_id, 350), max(0, planned))
                cur.execute(
                    "INSERT OR IGNORE INTO transport_plan "
                    "(tick_planned, tick_for, lane_id, planned_capacity, flex_pct) "
                    "VALUES (?,?,?,?,?)",
                    (TICK, plan_for, lane_id, capped, FLEX_PCT),
                )
                trans_parts.append(f"L{lane_id}={capped}")

            action_text = (
                f"Staffing plan for tick {plan_for} | "
                f"Labor: {' '.join(labor_parts)} | "
                f"Transport: {' '.join(trans_parts)}"
            )
            cur.execute(
                "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                (TICK, AGENT, action_text),
            )
            log_tokens(cur, TICK, AGENT, in_tok, out_tok)
            conn.commit()
            print(f"  {action_text}")

        finally:
            conn.close()

    with_backoff(execute)


if __name__ == "__main__":
    run()

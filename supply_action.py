"""
Supply agent: asks Claude Opus 4.6 to decide how much stock to order from external
suppliers for each IBCenter. Claude sees current IB inventory, demand forecasts,
downstream FC inventory levels, and active disruptions.
"""
import sqlite3
import time
import random
import sys

from token_utils import call_claude, extract_json, log_tokens
from env import IB_FC_WEIGHTS

DB_PATH     = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
TICK        = int(sys.argv[1]) if len(sys.argv) > 1 else 1
AGENT       = "Supply"
MAX_RETRIES = 5

MAX_EXTERNAL_INFLOW = {1: 350, 2: 280}
FC_BASE_DEMAND = {3: 70, 4: 90, 5: 55}
COVERAGE_TICKS = 3


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

            cur.execute("SELECT COUNT(*) FROM log WHERE agent_name=? AND tick=?", (AGENT, TICK))
            if cur.fetchone()[0] > 0:
                print(f"Supply already ran at tick {TICK}. Skipping.")
                return

            # Node state
            cur.execute("SELECT id, type, inventory, capacity, safety_stock FROM nodes ORDER BY id")
            nodes = {r[0]: {"type": r[1], "inv": r[2], "cap": r[3], "safety": r[4]}
                     for r in cur.fetchall()}

            # Demand forecasts for next 3 ticks
            forecast_lines = []
            for fc_id in [3, 4, 5]:
                cur.execute("""
                    SELECT AVG(forecast_demand) FROM forecast
                    WHERE node_id=? AND forecast_for_tick > ? AND forecast_for_tick <= ?
                """, (fc_id, TICK, TICK + COVERAGE_TICKS))
                avg_row = cur.fetchone()
                avg_demand = (avg_row[0] if avg_row and avg_row[0] else FC_BASE_DEMAND[fc_id])
                forecast_lines.append(f"FC{fc_id}: avg_forecast_next3={avg_demand:.1f}")

            # Active disruptions on IB nodes or lanes
            cur.execute("""
                SELECT type, target_id, severity, description,
                       (tick_started + duration - ?) AS remaining
                FROM disruptions WHERE resolved=0 AND (tick_started+duration) > ?
                  AND type IN ('ib_node', 'lane')
            """, (TICK, TICK))
            disrup = cur.fetchall()
            disrup_text = (
                "; ".join(f"[{r[0]}] t={r[1]} sev={r[2]:.0%} rem={r[4]}t: {r[3]}" for r in disrup)
                or "none"
            )

            ib_lines = []
            for ib_id in [1, 2]:
                n = nodes[ib_id]
                space = n["cap"] - n["inv"]
                max_in = MAX_EXTERNAL_INFLOW[ib_id]
                fc_weights = IB_FC_WEIGHTS[ib_id]
                served = ", ".join(f"FC{fc}(wt={w})" for fc, w in fc_weights.items())
                ib_lines.append(
                    f"IB{ib_id}: inv={n['inv']}/{n['cap']} safety={n['safety']} "
                    f"space_avail={space} max_inflow={max_in} serves={served}"
                )

            prompt = (
                f"Tick {TICK}. Decide external supplier order quantities for each IBCenter.\n\n"
                f"IBCenter state:\n" + "\n".join(ib_lines) + "\n\n"
                f"FC demand forecasts (avg next {COVERAGE_TICKS} ticks):\n"
                + "\n".join(forecast_lines) + "\n\n"
                f"Active supply-side disruptions: {disrup_text}\n\n"
                f"Guidelines:\n"
                f"- Target IB inventory = {COVERAGE_TICKS}x weighted downstream demand.\n"
                f"- Order = max(0, target - current_inv), capped at max_inflow and space_avail.\n"
                f"- If IB disruption is active, consider building extra buffer at other IB.\n"
                f"- Don't over-order (capacity cap applies).\n\n"
                f"Return ONLY valid JSON with integer order quantities:\n"
                f'{{ "IB1": <int>, "IB2": <int> }}'
            )

            text, in_tok, out_tok = call_claude(prompt, max_tokens=1024)

            try:
                data = extract_json(text)
                orders = {
                    1: int(data.get("IB1", 0)),
                    2: int(data.get("IB2", 0)),
                }
            except Exception:
                # Fallback: rule-based
                orders = {}
                for ib_id, fc_weights in IB_FC_WEIGHTS.items():
                    n = nodes[ib_id]
                    space = n["cap"] - n["inv"]
                    total_wt_fc = 0.0
                    for fc_id, weight in fc_weights.items():
                        cur.execute("""
                            SELECT AVG(forecast_demand) FROM forecast
                            WHERE node_id=? AND forecast_for_tick > ? AND forecast_for_tick <= ?
                        """, (fc_id, TICK, TICK + COVERAGE_TICKS))
                        avg_row = cur.fetchone()
                        avg = avg_row[0] if avg_row and avg_row[0] else FC_BASE_DEMAND[fc_id]
                        total_wt_fc += avg * weight
                    target = round(total_wt_fc * COVERAGE_TICKS)
                    gap = target - n["inv"]
                    orders[ib_id] = max(0, min(gap, MAX_EXTERNAL_INFLOW[ib_id], space))

            parts = []
            for ib_id, qty in orders.items():
                n = nodes[ib_id]
                space = n["cap"] - n["inv"]
                # Enforce hard caps
                qty = max(0, min(qty, MAX_EXTERNAL_INFLOW[ib_id], space))
                if qty > 0:
                    cur.execute(
                        "UPDATE nodes SET inventory = inventory + ? WHERE id=?",
                        (qty, ib_id),
                    )
                parts.append(
                    f"IB{ib_id}: +{qty} (inv {n['inv']} -> {n['inv'] + qty}, "
                    f"max_in={MAX_EXTERNAL_INFLOW[ib_id]} space={space})"
                )

            action_text = " | ".join(parts)
            cur.execute(
                "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                (TICK, AGENT, action_text),
            )
            log_tokens(cur, TICK, AGENT, in_tok, out_tok)
            conn.commit()
            for p in parts:
                print(f"  {p}")

        finally:
            conn.close()

    with_backoff(execute)


if __name__ == "__main__":
    run()

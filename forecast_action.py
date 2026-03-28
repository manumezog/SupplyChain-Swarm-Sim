"""
Forecast agent: calls Claude Opus 4.6 to predict next-tick demand for each FC.
Uses last 5 ticks of demand history + seasonal context + active disruptions.
"""
import sqlite3
import time
import random
import math
import sys

from token_utils import call_claude, extract_json, log_tokens

DB_PATH     = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
TICK        = int(sys.argv[1]) if len(sys.argv) > 1 else 1
AGENT       = "Forecast"
MAX_RETRIES = 5

FC_BASE_DEMAND = {3: 70, 4: 90, 5: 55}


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
            forecast_for = TICK + 1

            cur.execute(
                "SELECT COUNT(*) FROM forecast WHERE tick_created=? AND forecast_for_tick=?",
                (TICK, forecast_for),
            )
            if cur.fetchone()[0] > 0:
                print(f"Forecast already made at tick {TICK} for tick {forecast_for}. Skipping.")
                return

            history_lines = []
            for node_id in [3, 4, 5]:
                cur.execute(
                    "SELECT tick, orders FROM demand WHERE node_id=? AND tick<=? "
                    "ORDER BY tick DESC LIMIT 5",
                    (node_id, TICK),
                )
                rows = cur.fetchall()
                if rows:
                    vals = ", ".join(f"t{r[0]}:{r[1]}" for r in reversed(rows))
                else:
                    vals = f"no history (base={FC_BASE_DEMAND[node_id]})"
                history_lines.append(f"FC{node_id}: {vals}")

            cur.execute("""
                SELECT type, target_id, severity, description FROM disruptions
                WHERE resolved=0 AND (tick_started+duration) > ? AND type='fc_node'
            """, (TICK,))
            disrup = cur.fetchall()
            disrup_text = (
                ", ".join(f"FC{r[1]} {r[3]} sev={r[2]:.0%}" for r in disrup)
                or "none"
            )

            # Seasonal signal for next tick
            seasonal_next = round(1.0 + 0.35 * math.sin(forecast_for * 0.4), 3)

            prompt = (
                f"Tick {TICK}. Forecast customer order volumes for tick {forecast_for}.\n\n"
                f"Demand history:\n" + "\n".join(history_lines) + "\n\n"
                f"Seasonal multiplier for tick {forecast_for}: {seasonal_next} "
                f"(base×seasonal gives expected range; orders also vary ±25% randomly)\n"
                f"Active FC disruptions: {disrup_text}\n\n"
                f"Return ONLY valid JSON with integer forecasts:\n"
                f'{{ "FC3": <int>, "FC4": <int>, "FC5": <int> }}'
            )

            text, in_tok, out_tok = call_claude(prompt, max_tokens=1024)

            try:
                data = extract_json(text)
                forecasts = {
                    3: max(1, int(data.get("FC3", FC_BASE_DEMAND[3]))),
                    4: max(1, int(data.get("FC4", FC_BASE_DEMAND[4]))),
                    5: max(1, int(data.get("FC5", FC_BASE_DEMAND[5]))),
                }
                method = "claude_opus_4_6"
            except Exception:
                # Fallback: exponential smoothing
                forecasts = {}
                for node_id, base in FC_BASE_DEMAND.items():
                    cur.execute(
                        "SELECT orders FROM demand WHERE node_id=? AND tick<=? "
                        "ORDER BY tick DESC LIMIT 5", (node_id, TICK),
                    )
                    records = [r[0] for r in cur.fetchall()]
                    if not records:
                        forecasts[node_id] = base
                    elif len(records) == 1:
                        forecasts[node_id] = records[0]
                    else:
                        vals = list(reversed(records))
                        s = float(vals[0])
                        for v in vals[1:]:
                            s = 0.4 * v + 0.6 * s
                        forecasts[node_id] = round(s)
                method = "exp_smooth_fallback"

            parts = []
            for node_id, forecast in forecasts.items():
                cur.execute(
                    "INSERT OR IGNORE INTO forecast "
                    "(tick_created, forecast_for_tick, node_id, forecast_demand, actual_demand, mae, method) "
                    "VALUES (?,?,?,?,NULL,NULL,?)",
                    (TICK, forecast_for, node_id, float(forecast), method),
                )
                parts.append(f"FC{node_id}:{forecast}")

            action_text = f"Forecast for tick {forecast_for}: {' | '.join(parts)} [{method}]"
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

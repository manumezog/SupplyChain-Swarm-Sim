"""
Repair agent: Claude Opus 4.6 reviews all active prior-tick disruptions and
decides which one to prioritize for repair based on network impact.
Cannot resolve disruptions that started this tick.
"""
import sqlite3
import time
import random
import sys

from token_utils import call_claude, extract_json, log_tokens

DB_PATH     = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
MAX_RETRIES = 5
TICK        = int(sys.argv[1]) if len(sys.argv) > 1 else 1
AGENT       = "Repair"


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
                print(f"Repair already ran at tick {TICK}. Skipping.")
                return None

            # Auto-expire elapsed disruptions
            cur.execute("""
                UPDATE disruptions SET resolved=1
                WHERE resolved=0 AND (tick_started + duration) <= ?
            """, (TICK,))

            # All active prior-tick disruptions
            cur.execute("""
                SELECT id, type, target_id, severity, description, duration, tick_started,
                       (tick_started + duration - ?) AS ticks_remaining
                FROM disruptions
                WHERE resolved=0 AND tick_started < ? AND (tick_started + duration) > ?
                ORDER BY severity DESC
            """, (TICK, TICK, TICK))
            candidates = cur.fetchall()

            if not candidates:
                msg = f"No active prior-tick disruptions to repair at tick {TICK}."
                cur.execute(
                    "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                    (TICK, AGENT, msg),
                )
                log_tokens(cur, TICK, AGENT, 80, 20)
                conn.commit()
                print(f"  {msg}")
                return None

            # Get FC backlog context to help Claude prioritize
            cur.execute("SELECT node_id, backlog FROM demand WHERE tick=?", (TICK,))
            backlogs = {r[0]: r[1] for r in cur.fetchall()}

            candidates_text = "\n".join(
                f"id={r[0]} type={r[1]} target={r[2]} sev={r[3]:.0%} "
                f"remaining={r[7]}t: {r[4]}"
                for r in candidates
            )
            backlog_text = ", ".join(f"FC{k}={v}" for k, v in backlogs.items()) or "none"

            prompt = (
                f"Tick {TICK}. Choose ONE disruption to resolve (repair resources are limited to 1/tick).\n\n"
                f"Active disruptions from prior ticks:\n{candidates_text}\n\n"
                f"Current FC backlogs: {backlog_text}\n\n"
                f"Prioritize: high severity > FC disruptions when backlog is rising > long-remaining.\n"
                f"If multiple equal severity, prefer the one affecting nodes with high backlogs.\n\n"
                f'Return ONLY valid JSON: {{ "resolve_id": <disruption_id>, "rationale": "<short reason>" }}'
            )

            text, in_tok, out_tok = call_claude(prompt, max_tokens=512)

            try:
                data = extract_json(text)
                resolve_id = int(data["resolve_id"])
                rationale = str(data.get("rationale", "highest severity"))
            except Exception:
                # Fallback: pick highest severity
                resolve_id = candidates[0][0]
                rationale = "highest severity (fallback)"

            # Verify the chosen id is valid
            valid_ids = {r[0] for r in candidates}
            if resolve_id not in valid_ids:
                resolve_id = candidates[0][0]
                rationale = "highest severity (corrected invalid choice)"

            # Find the chosen disruption details
            chosen = next(r for r in candidates if r[0] == resolve_id)
            d_id, d_type, target_id, severity, description, duration, tick_started, ticks_remaining = chosen

            cur.execute("UPDATE disruptions SET resolved=1 WHERE id=?", (d_id,))

            if d_type == "lane":
                cur.execute(
                    "UPDATE lanes SET status='active' WHERE id=? AND LOWER(status)='disrupted'",
                    (target_id,),
                )

            action_text = (
                f"Resolved [{d_type.upper()}] {description} "
                f"(sev={severity:.0%} {ticks_remaining}t remaining) — {rationale}"
            )
            cur.execute(
                "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                (TICK, AGENT, action_text),
            )
            log_tokens(cur, TICK, AGENT, in_tok, out_tok)
            conn.commit()
            print(f"  {action_text}")
            return d_id

        finally:
            conn.close()

    return with_backoff(execute)


if __name__ == "__main__":
    result = run()
    if result is None:
        print("REPAIR — nothing to resolve this tick.")

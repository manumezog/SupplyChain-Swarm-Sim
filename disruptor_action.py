"""
Disruption agent: probabilistic chaos engine.
Python determines IF a disruption fires and selects the target/category.
Claude Opus 4.6 writes a contextual narrative description of the event,
grounding it in the current network state.
"""
import sqlite3
import time
import random
import sys

from token_utils import call_claude, log_tokens

DB_PATH               = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
TICK                  = int(sys.argv[1]) if len(sys.argv) > 1 else 1
AGENT                 = "Disruptor"
MAX_RETRIES           = 5
TRIGGER_PROBABILITY   = 0.30
MAX_ACTIVE_DISRUPTIONS = 3

# (severity, duration_ticks)
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
                print(f"Disruptor already ran at tick {TICK}. Skipping.")
                return None

            if random.random() > TRIGGER_PROBABILITY:
                msg = f"No disruption event this tick (rolled > {TRIGGER_PROBABILITY:.0%})."
                cur.execute(
                    "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                    (TICK, AGENT, msg),
                )
                in_tok, out_tok = 80, 20
                log_tokens(cur, TICK, AGENT, in_tok, out_tok)
                conn.commit()
                print(f"  {msg}")
                return None

            cur.execute("""
                SELECT COUNT(*) FROM disruptions
                WHERE resolved=0 AND (tick_started + duration) > ?
            """, (TICK,))
            active_count = cur.fetchone()[0]
            if active_count >= MAX_ACTIVE_DISRUPTIONS:
                msg = f"Disruption cap reached ({active_count}/{MAX_ACTIVE_DISRUPTIONS}). No new event."
                cur.execute(
                    "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                    (TICK, AGENT, msg),
                )
                in_tok, out_tok = 80, 20
                log_tokens(cur, TICK, AGENT, in_tok, out_tok)
                conn.commit()
                print(f"  {msg}")
                return None

            # Probabilistic category selection
            roll = random.random()
            if roll < 0.50:
                category = "lane"
            elif roll < 0.70:
                category = "ib_node"
            else:
                category = "fc_node"

            target_id, desc_base, severity, duration = None, None, None, None
            target_label = ""

            if category == "lane":
                cur.execute("SELECT id, origin, destination FROM lanes WHERE LOWER(status)='active'")
                candidates = cur.fetchall()
                if not candidates:
                    category = "fc_node"
                else:
                    t = random.choice(candidates)
                    target_id = t[0]
                    desc_base, severity, duration = random.choice(LANE_SCENARIOS)
                    target_label = f"Lane {target_id} ({t[1]}->{t[2]})"
                    cur.execute(
                        "UPDATE lanes SET status='disrupted' WHERE id=? AND LOWER(status)='active'",
                        (target_id,),
                    )

            if category == "ib_node":
                cur.execute("SELECT id FROM nodes WHERE type='IBCenter'")
                candidates = [r[0] for r in cur.fetchall()]
                cur.execute("""
                    SELECT DISTINCT target_id FROM disruptions
                    WHERE type='ib_node' AND resolved=0 AND (tick_started+duration)>?
                """, (TICK,))
                already = {r[0] for r in cur.fetchall()}
                candidates = [c for c in candidates if c not in already]
                if not candidates:
                    category = "fc_node"
                else:
                    target_id = random.choice(candidates)
                    desc_base, severity, duration = random.choice(IB_SCENARIOS)
                    target_label = f"IB{target_id}"

            if category == "fc_node":
                cur.execute("SELECT id FROM nodes WHERE type='FulfillmentCenter'")
                candidates = [r[0] for r in cur.fetchall()]
                cur.execute("""
                    SELECT DISTINCT target_id FROM disruptions
                    WHERE type='fc_node' AND resolved=0 AND (tick_started+duration)>?
                """, (TICK,))
                already = {r[0] for r in cur.fetchall()}
                candidates = [c for c in candidates if c not in already]
                if not candidates:
                    msg = f"All targets already disrupted. No new event at tick {TICK}."
                    cur.execute(
                        "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                        (TICK, AGENT, msg),
                    )
                    in_tok, out_tok = 80, 20
                    log_tokens(cur, TICK, AGENT, in_tok, out_tok)
                    conn.commit()
                    print(f"  {msg}")
                    return None
                target_id = random.choice(candidates)
                desc_base, severity, duration = random.choice(FC_SCENARIOS)
                target_label = f"FC{target_id}"

            # Get network context for Claude's narrative
            cur.execute("SELECT inventory, capacity FROM nodes WHERE id=?", (target_id,))
            node_row = cur.fetchone()
            node_context = (
                f"inv={node_row[0]}/{node_row[1]}" if node_row else "N/A"
            )

            prompt = (
                f"Write a 1-sentence operational alert for a supply chain disruption event.\n\n"
                f"Event type: {desc_base}\n"
                f"Target: {target_label} (current state: {node_context})\n"
                f"Severity: {severity:.0%} capacity loss\n"
                f"Duration: {duration} operating periods\n\n"
                f"Write a concise, realistic operational description (max 12 words). "
                f"No JSON needed — just the plain text description."
            )

            narrative_text, in_tok, out_tok = call_claude(prompt, max_tokens=128)
            description = narrative_text.strip().rstrip('.')[:120]
            if not description:
                description = f"{desc_base} at {target_label}"

            cur.execute(
                "INSERT INTO disruptions "
                "(tick_started, duration, type, target_id, severity, description) "
                "VALUES (?,?,?,?,?,?)",
                (TICK, duration, category, target_id, severity, description),
            )
            action_text = (
                f"[{category.upper()}] {description} — "
                f"sev={severity:.0%} dur={duration}t "
                f"(expires tick {TICK + duration})"
            )
            cur.execute(
                "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                (TICK, AGENT, action_text),
            )
            log_tokens(cur, TICK, AGENT, in_tok, out_tok)
            conn.commit()
            print(f"  {action_text}")
            return target_id

        finally:
            conn.close()

    return with_backoff(execute)


if __name__ == "__main__":
    result = run()
    if result is None:
        print("DISRUPTOR — no event this tick.")

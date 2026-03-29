"""
Planner agent: Claude Opus 4.6 decides replenishment shipments from IBCenters to FCs.
Constraints:
  - Lane disruption: reduces throughput (severity = % capacity lost)
  - Transport plan: booked capacity per lane this tick (±15% flex at execution)
  - IB node disruption: limits IB outbound dispatch
  - IB labor plan: limits total units IB can dispatch this tick (shared across all FCs)
"""
import sqlite3
import time
import random
import sys

from token_utils import call_claude, extract_json, log_tokens
from env import LANE_TO_IB, INBOUND_LANES

DB_PATH     = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
TICK        = int(sys.argv[1]) if len(sys.argv) > 1 else 1
AGENT_NAME  = "Planner"
MAX_RETRIES = 3


def get_conn():
    for attempt in range(MAX_RETRIES):
        try:
            return sqlite3.connect(DB_PATH, timeout=10)
        except sqlite3.OperationalError:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(0.1 * (2 ** attempt))


def active_severity(cur, d_type, target_id):
    cur.execute("""
        SELECT COALESCE(MAX(severity), 0) FROM disruptions
        WHERE type=? AND target_id=? AND resolved=0 AND (tick_started + duration) > ?
    """, (d_type, target_id, TICK))
    return cur.fetchone()[0]


def run():
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            conn = get_conn()
            cur  = conn.cursor()

            cur.execute(
                "SELECT COUNT(*) FROM log WHERE agent_name=? AND tick=?",
                (AGENT_NAME, TICK),
            )
            if cur.fetchone()[0] > 0:
                print(f"Planner already ran at tick {TICK}. Skipping.")
                conn.close()
                return

            # FC state
            cur.execute(
                "SELECT id, inventory, capacity, safety_stock FROM nodes "
                "WHERE type='FulfillmentCenter' ORDER BY id"
            )
            fcs = {r[0]: {"inv": r[1], "cap": r[2], "safety": r[3]} for r in cur.fetchall()}

            # IB state
            cur.execute(
                "SELECT id, inventory, labor_capacity_base FROM nodes "
                "WHERE type='IBCenter' ORDER BY id"
            )
            ibs = {r[0]: {"inv": r[1], "labor_base": r[2]} for r in cur.fetchall()}

            # Lane state
            cur.execute("SELECT id, origin, destination, transport_cost, status FROM lanes ORDER BY id")
            lanes = {r[0]: {"origin": r[1], "dest": r[2], "tc": r[3], "status": r[4]}
                     for r in cur.fetchall()}

            # Transport plans for this tick
            cur.execute(
                "SELECT lane_id, planned_capacity, flex_pct FROM transport_plan WHERE tick_for=?",
                (TICK,),
            )
            tplan = {r[0]: {"cap": r[1], "flex": r[2]} for r in cur.fetchall()}

            # IB labor plans for this tick
            cur.execute(
                "SELECT node_id, planned_labor, flex_pct FROM labor_plan "
                "WHERE tick_for=? AND node_id IN (1,2)",
                (TICK,),
            )
            ib_lplan = {r[0]: {"labor": r[1], "flex": r[2]} for r in cur.fetchall()}

            # Disruption severities
            lane_sev = {lid: active_severity(cur, "lane", lid) for lid in lanes}
            ib_sev   = {ib_id: active_severity(cur, "ib_node", ib_id) for ib_id in ibs}

            # FCs below safety_stock
            fcs_below = {nid: d for nid, d in fcs.items() if d["inv"] < d["safety"]}

            if not fcs_below:
                msg = f"All FC nodes above safety_stock at tick {TICK}. No replenishment."
                cur.execute(
                    "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                    (TICK, AGENT_NAME, msg),
                )
                log_tokens(cur, TICK, AGENT_NAME, 80, 20)
                conn.commit()
                conn.close()
                print(f"  {msg}")
                return

            # Build context for Claude
            fc_lines = []
            for nid, d in fcs_below.items():
                gap = d["cap"] - d["inv"]
                max_repl = min(gap, round(d["cap"] * 0.30))
                lane_options = []
                for lid in INBOUND_LANES.get(nid, []):
                    l = lanes[lid]
                    sev = lane_sev[lid]
                    tc_plan = tplan.get(lid)
                    tc_str = (
                        f"transport_cap={tc_plan['cap']}(flex±{tc_plan['flex']:.0%})"
                        if tc_plan else "no_transport_plan"
                    )
                    lane_options.append(
                        f"L{lid}(IB{LANE_TO_IB[lid]} tc={l['tc']} "
                        f"status={l['status']} sev={sev:.0%} {tc_str})"
                    )
                fc_lines.append(
                    f"FC{nid}: inv={d['inv']}/{d['cap']} safety={d['safety']} "
                    f"max_repl={max_repl} lanes=[{', '.join(lane_options)}]"
                )

            ib_lines = []
            for ib_id, d in ibs.items():
                lp = ib_lplan.get(ib_id)
                labor_str = (
                    f"labor_plan={lp['labor']}(flex±{lp['flex']:.0%})"
                    if lp else f"no_plan(base={d['labor_base']})"
                )
                dispatch_max = round(d["inv"] * (1.0 - ib_sev[ib_id]))
                ib_lines.append(
                    f"IB{ib_id}: inv={d['inv']} sev={ib_sev[ib_id]:.0%} "
                    f"dispatch_cap={dispatch_max} {labor_str}"
                )

            prompt = (
                f"Tick {TICK}. Plan replenishment shipments from IBCenters to FulfillmentCenters.\n\n"
                f"FCs below safety stock:\n" + "\n".join(fc_lines) + "\n\n"
                f"IBCenter state:\n" + "\n".join(ib_lines) + "\n\n"
                f"Rules:\n"
                f"- For each FC, choose one inbound lane (prefer lowest transport_cost among active).\n"
                f"- Shipment qty <= min(max_repl, IB_inv, IB_dispatch_cap, transport_cap*(1+flex)).\n"
                f"- Lane disruption (sev>0): effective throughput = planned_qty * (1-sev).\n"
                f"- IB labor is shared: total dispatched from each IB across all FCs <= labor_plan*(1+flex).\n"
                f"- If all lanes blocked: halve replenishment quantity.\n\n"
                f"Return ONLY valid JSON. Use 0 if an FC should not be replenished this tick:\n"
                f'{{ "replenishments": [{{'
                f'"fc_id": <int>, "lane_id": <int>, "quantity": <int>'
                f'}}] }}'
            )

            text, in_tok, out_tok = call_claude(prompt, max_tokens=256)

            try:
                data = extract_json(text)
                replenishments = data.get("replenishments", [])
            except Exception:
                replenishments = []

            # Fallback: if Claude returns nothing useful, apply rule-based
            if not replenishments:
                replenishments = []
                for nid, d in fcs_below.items():
                    gap = d["cap"] - d["inv"]
                    max_repl = min(gap, round(d["cap"] * 0.30))
                    lane_ids = INBOUND_LANES.get(nid, [])
                    active_lanes = [
                        lid for lid in lane_ids
                        if lanes[lid]["status"].lower() == "active"
                    ]
                    if active_lanes:
                        best_lane = min(active_lanes, key=lambda l: lanes[l]["tc"])
                    elif lane_ids:
                        best_lane = lane_ids[0]
                        max_repl = max_repl // 2
                    else:
                        continue
                    replenishments.append({
                        "fc_id": nid, "lane_id": best_lane, "quantity": max_repl
                    })

            # Track IB labor used this tick
            ib_labor_used = {ib_id: 0 for ib_id in ibs}

            actions = []
            for r in replenishments:
                try:
                    fc_id   = int(r["fc_id"])
                    lane_id = int(r["lane_id"])
                    qty     = int(r["quantity"])
                except (KeyError, ValueError, TypeError):
                    continue

                if fc_id not in fcs or lane_id not in lanes:
                    continue
                if qty <= 0:
                    continue

                ib_id = LANE_TO_IB.get(lane_id)
                if ib_id is None:
                    continue

                fc_d  = fcs[fc_id]
                ib_d  = ibs[ib_id]
                notes = []

                # Lane disruption: reduce throughput
                l_sev = lane_sev[lane_id]
                if l_sev > 0:
                    qty = round(qty * (1.0 - l_sev))
                    notes.append(f"lane_sev={l_sev:.0%}")

                # Transport plan cap (with flex)
                tp = tplan.get(lane_id)
                if tp:
                    tc_max = round(tp["cap"] * (1.0 + tp["flex"]))
                    if qty > tc_max:
                        qty = tc_max
                        notes.append(f"transport_cap={tp['cap']}")

                # IB dispatch cap (disruption-based)
                ib_dispatch_max = round(ib_d["inv"] * (1.0 - ib_sev[ib_id]))

                # IB labor remaining this tick
                lp = ib_lplan.get(ib_id)
                if lp:
                    ib_labor_max = round(lp["labor"] * (1.0 + lp["flex"]))
                    ib_labor_remaining = ib_labor_max - ib_labor_used[ib_id]
                    if ib_labor_remaining <= 0:
                        notes.append(f"IB{ib_id} labor_exhausted")
                        continue
                    if qty > ib_labor_remaining:
                        qty = ib_labor_remaining
                        notes.append(f"IB{ib_id}_labor_cap={lp['labor']}")
                else:
                    ib_labor_remaining = ib_d["inv"]  # no plan: unconstrained by labor

                # Final quantity bounded by IB stock and dispatch cap
                qty = min(qty, ib_d["inv"], ib_dispatch_max)

                if ib_d["inv"] == 0:
                    notes.append(f"IB{ib_id}_STOCKOUT")
                    qty = 0

                if qty <= 0:
                    continue

                cur.execute(
                    "UPDATE nodes SET inventory = inventory + ? WHERE id=?",
                    (qty, fc_id),
                )
                cur.execute(
                    "UPDATE nodes SET inventory = inventory - ? WHERE id=?",
                    (qty, ib_id),
                )
                ib_d["inv"] -= qty
                ib_labor_used[ib_id] += qty

                note_str = f" [{'; '.join(notes)}]" if notes else ""
                msg = (
                    f"FC{fc_id}: inv {fc_d['inv']} -> {fc_d['inv'] + qty} "
                    f"(+{qty} from IB{ib_id} via L{lane_id}, "
                    f"threshold={fc_d['safety']}){note_str}"
                )
                fc_d["inv"] += qty
                actions.append(msg)

            if not actions:
                actions = ["No replenishments executed (all constrained or zero qty)."]

            action_text = " | ".join(actions)
            cur.execute(
                "INSERT INTO log (tick, agent_name, action_taken) VALUES (?,?,?)",
                (TICK, AGENT_NAME, action_text),
            )
            log_tokens(cur, TICK, AGENT_NAME, in_tok, out_tok)
            conn.commit()
            conn.close()
            for a in actions:
                print(f"  {a}")
            return

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < MAX_RETRIES:
                time.sleep(1)
            else:
                raise


if __name__ == "__main__":
    run()

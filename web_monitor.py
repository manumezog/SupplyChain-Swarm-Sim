"""
SwarmChain-Sim · Web Dashboard Server
Flask + Flask-SocketIO backend that serves the dashboard UI
and pushes live state updates via WebSocket.
Supports replay mode: clients can request state for any historical tick.

Usage:
    python web_monitor.py

Then open http://localhost:5050 in your browser.
"""

import os
import sys
import sqlite3
import time
import threading
from flask import Flask, send_from_directory
from flask_socketio import SocketIO, emit

# Project modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from token_utils import get_db_conn, DB_PATH

WEB_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT      = 5050
POLL_INTERVAL = 2


# ── Flask Setup ───────────────────────────────────────────────
app = Flask(__name__, static_folder=WEB_DIR)
app.config['SECRET_KEY'] = 'swarmchain-sim-dashboard'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


# ── Static File Serving ──────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(WEB_DIR, 'index.html')

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(WEB_DIR, filename)


# ── Database Queries ──────────────────────────────────────────

def db_query(sql, params=()):
    try:
        conn = get_db_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[DB] Query error: {e}")
        return []


def db_scalar(sql, params=()):
    try:
        conn = get_db_conn()
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_current_tick():
    v = db_scalar("SELECT MAX(tick) FROM log")
    return v or 0


def get_tick_range():
    min_t = db_scalar("SELECT MIN(tick) FROM log") or 0
    max_t = db_scalar("SELECT MAX(tick) FROM log") or 0
    return {"min_tick": min_t, "max_tick": max_t}


# ── State Assembly ────────────────────────────────────────────

def build_state(tick=None):
    """Build dashboard state. If tick is None, uses the latest tick."""
    if tick is None:
        tick = get_current_tick()
    is_live = (tick == get_current_tick())

    # ── Inventory ─────────────────────────────────────────────
    if is_live:
        inventory = db_query(
            "SELECT id, type, capacity, safety_stock, inventory, labor_capacity_base "
            "FROM nodes ORDER BY id"
        )
    else:
        # Historical: use snapshot data for inventory, current schema for structure
        inventory = db_query(
            "SELECT n.id, n.type, n.capacity, n.safety_stock, n.labor_capacity_base, "
            "COALESCE(s.inventory, n.inventory) as inventory "
            "FROM nodes n LEFT JOIN snapshots s ON s.node_id = n.id AND s.tick = ? "
            "ORDER BY n.id",
            (tick,)
        )

    # ── Inventory history (last 20 ticks up to requested tick) ─
    history = {}
    for node in inventory:
        snaps = db_query(
            "SELECT inventory FROM snapshots WHERE node_id=? AND tick<=? ORDER BY tick DESC LIMIT 20",
            (node['id'], tick)
        )
        history[node['id']] = list(reversed([s['inventory'] for s in snaps]))

    # ── Lanes ─────────────────────────────────────────────────
    lanes = db_query("SELECT id, origin, destination, transport_cost, status FROM lanes ORDER BY id")

    # ── Active disruptions at this tick ───────────────────────
    disruptions = db_query("""
        SELECT type, target_id, severity, description,
               (tick_started + duration - ?) AS remaining
        FROM disruptions
        WHERE resolved = 0 AND tick_started <= ? AND (tick_started + duration) > ?
        ORDER BY severity DESC
    """, (tick, tick, tick))
    # Also include disruptions that were resolved AFTER this tick
    disruptions += db_query("""
        SELECT type, target_id, severity, description,
               (tick_started + duration - ?) AS remaining
        FROM disruptions
        WHERE resolved = 1 AND tick_started <= ? AND (tick_started + duration) > ?
        ORDER BY severity DESC
    """, (tick, tick, tick))
    # Deduplicate
    seen = set()
    unique_disruptions = []
    for d in disruptions:
        key = (d['type'], d['target_id'], d['severity'])
        if key not in seen:
            seen.add(key)
            unique_disruptions.append(d)
    disruptions = unique_disruptions

    # ── Demand at this tick ───────────────────────────────────
    demand = db_query("""
        SELECT d.node_id, d.orders, d.shipped, d.backlog, d.labor_capacity,
               n.labor_capacity_base,
               f.forecast_demand, f.mae, f.method
        FROM demand d
        JOIN nodes n ON n.id = d.node_id
        LEFT JOIN forecast f ON f.node_id = d.node_id AND f.forecast_for_tick = d.tick
        WHERE d.tick = ?
        ORDER BY d.node_id
    """, (tick,))

    # ── Token cost ────────────────────────────────────────────
    tick_cost_row = db_query(
        "SELECT SUM(cost_eur) as c FROM token_log WHERE tick = ?", (tick,)
    )
    total_cost_row = db_query(
        "SELECT SUM(cost_eur) as c FROM token_log WHERE tick <= ?", (tick,)
    )
    cost_history = db_query(
        "SELECT SUM(cost_eur) as c FROM token_log WHERE tick <= ? GROUP BY tick ORDER BY tick",
        (tick,)
    )
    breakdown = db_query(
        "SELECT tick, agent_name, input_tokens, output_tokens, cost_eur, model_name "
        "FROM token_log WHERE tick <= ? ORDER BY id DESC LIMIT 8",
        (tick,)
    )

    tokens = {
        'tick_cost':    (tick_cost_row[0]['c'] or 0) if tick_cost_row else 0,
        'total_cost':   (total_cost_row[0]['c'] or 0) if total_cost_row else 0,
        'cost_history': [r['c'] for r in cost_history],
        'breakdown':    breakdown,
    }

    # ── Financials ────────────────────────────────────────────
    financials_history = db_query(
        "SELECT tick, total_labor_cost, total_transport_cost, total_units_shipped, cost_per_unit_shipped "
        "FROM financials WHERE tick <= ? ORDER BY tick",
        (tick,)
    )
    if financials_history:
        latest_fin = financials_history[-1]
    else:
        latest_fin = {"tick": tick, "total_labor_cost": 0, "total_transport_cost": 0, "total_units_shipped": 0, "cost_per_unit_shipped": 0}

    # ── Agent log (with reasoning) ────────────────────────────
    log = db_query(
        "SELECT tick, agent_name, action_taken, reasoning FROM log "
        "WHERE tick <= ? ORDER BY id DESC LIMIT 15",
        (tick,)
    )

    # ── Tick range for replay scrubber ────────────────────────
    tick_range = get_tick_range()

    return {
        'tick':        tick,
        'is_live':     is_live,
        'timestamp':   time.strftime('%Y-%m-%d %H:%M:%S'),
        'tick_range':  tick_range,
        'inventory':   inventory,
        'history':     history,
        'lanes':       lanes,
        'disruptions': disruptions,
        'demand':      demand,
        'tokens':      tokens,
        'log':         log,
        'financials':  latest_fin,
        'financials_history': financials_history,
    }


# ── WebSocket Event Handlers ─────────────────────────────────

@socketio.on('request_tick_state')
def handle_tick_request(data):
    """Client requests state for a specific historical tick."""
    tick = data.get('tick')
    if tick is not None:
        state = build_state(tick=int(tick))
        emit('state_update', state)


@socketio.on('request_tick_range')
def handle_tick_range():
    """Client requests the available tick range for the scrubber."""
    emit('tick_range', get_tick_range())


@socketio.on('inject_disruption')
def handle_inject_disruption(data):
    """Manual trigger to inject a disruption."""
    tick = get_current_tick()
    target_id = data.get('target_id')
    category = data.get('type')  # 'lane', 'ib_node', 'fc_node'
    severity = float(data.get('severity', 0.5))
    duration = int(data.get('duration', 5))
    desc = data.get('description', f"Manual disruption on {category} {target_id}")

    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO disruptions (tick_started, duration, type, target_id, severity, description) "
            "VALUES (?,?,?,?,?,?)",
            (tick, duration, category, target_id, severity, desc)
        )
        if category == 'lane':
            cur.execute("UPDATE lanes SET status='disrupted' WHERE id=?", (target_id,))
            
        cur.execute(
            "INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
            (tick, "User", f"Manual inject: {desc} (sev={severity:.0%}, dur={duration}t)", "Dashboard control")
        )
        conn.commit()
        conn.close()
        # Force a state push
        state = build_state()
        emit('state_update', state, broadcast=True)
    except Exception as e:
        print(f"[Error] Failed to inject disruption: {e}")


@socketio.on('remove_disruption')
def handle_remove_disruption(data):
    """Manual trigger to resolve an active disruption."""
    d_id = data.get('id')
    if d_id is None:
        return
        
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        
        # Get info before removing
        cur.execute("SELECT type, target_id, description FROM disruptions WHERE id=?", (d_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return
            
        d_type, target_id, desc = row
        
        cur.execute("UPDATE disruptions SET resolved=1 WHERE id=?", (d_id,))
        if d_type == 'lane':
            cur.execute("UPDATE lanes SET status='active' WHERE id=?", (target_id,))
            
        tick = get_current_tick()
        cur.execute(
            "INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
            (tick, "User", f"Manual resolve: {desc}", "Dashboard control")
        )
        conn.commit()
        conn.close()
        
        state = build_state()
        emit('state_update', state, broadcast=True)
    except Exception as e:
        print(f"[Error] Failed to remove disruption: {e}")


@socketio.on('set_demand_modifier')
def handle_set_demand_modifier(data):
    """Manual trigger to spike/drop demand for an FC."""
    node_id = data.get('node_id')
    multiplier = float(data.get('multiplier', 1.0))
    desc = data.get('description', f"Manual modifier: {multiplier}x")
    
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        
        # Deactivate old modifiers for this node
        cur.execute("UPDATE demand_modifiers SET active=0 WHERE node_id=?", (node_id,))
        
        if multiplier != 1.0:
            cur.execute(
                "INSERT INTO demand_modifiers (node_id, multiplier, description, active) VALUES (?,?,?,1)",
                (node_id, multiplier, desc)
            )
            
        tick = get_current_tick()
        action = "Reset demand" if multiplier == 1.0 else f"Applied {multiplier}x demand spike"
        cur.execute(
            "INSERT INTO log (tick, agent_name, action_taken, reasoning) VALUES (?,?,?,?)",
            (tick, "User", f"{action} for FC{node_id}", "Dashboard control")
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Error] Failed to set modifier: {e}")


# ── Background Poller ─────────────────────────────────────────

_last_tick = None

def poll_and_push():
    global _last_tick
    while True:
        try:
            state = build_state()
            socketio.emit('state_update', state)
            if state['tick'] != _last_tick:
                _last_tick = state['tick']
                print(f"[Dashboard] Pushed tick {state['tick']}")
        except Exception as e:
            print(f"[Dashboard] Poll error: {e}")
        time.sleep(POLL_INTERVAL)


# ── Entry Point ───────────────────────────────────────────────

if __name__ == '__main__':
    if not os.path.exists(DB_PATH):
        print(f"[!] Database not found at {DB_PATH}")
        print(f"    Run `python env.py` first to initialize the simulation.")
        exit(1)

    print(f"╔═══════════════════════════════════════════════╗")
    print(f"║  SwarmChain-Sim · Web Dashboard               ║")
    print(f"║  http://localhost:{PORT}                       ║")
    print(f"║  Reading from: {os.path.basename(DB_PATH):<24}    ║")
    print(f"║  Press Ctrl+C to stop                         ║")
    print(f"╚═══════════════════════════════════════════════╝")

    poller = threading.Thread(target=poll_and_push, daemon=True)
    poller.start()

    socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)

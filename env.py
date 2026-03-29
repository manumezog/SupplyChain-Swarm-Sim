import sqlite3
import time
import os
import json

DB_PATH = "sim_state.db"
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load network topology from config (or use defaults) ──────
_net_config_path = os.path.join(_PROJECT_DIR, "network_config.json")
if os.path.exists(_net_config_path):
    with open(_net_config_path) as _f:
        _NET = json.load(_f)
else:
    _NET = None

# IBCenter = Inbound Center: receives stock, ships to FulfillmentCenters
if _NET:
    LANE_TO_IB    = {int(k): v for k, v in _NET["lane_to_ib"].items()}
    INBOUND_LANES = {int(k): v for k, v in _NET["inbound_lanes"].items()}
    IB_FC_WEIGHTS = {int(k): {int(fk): fv for fk, fv in v.items()}
                     for k, v in _NET["ib_fc_weights"].items()}
    MAX_EXTERNAL_INFLOW = {int(k): v for k, v in _NET["max_external_inflow"].items()}
    BASE_DEMAND   = {int(k): v for k, v in _NET["base_demand"].items()}
    LANE_TC_MAX   = {int(k): v for k, v in _NET.get("lane_tc_max", {}).items()}
    LANE_TC_BASE  = {int(k): v for k, v in _NET.get("lane_tc_base", {}).items()}
    DISRUPTION_PROBABILITY  = _NET.get("disruption_probability", 0.30)
    MAX_ACTIVE_DISRUPTIONS  = _NET.get("max_active_disruptions", 3)

    NODES = [
        (n["id"], n["type"], n["capacity"], n["safety_stock"],
         n["initial_inventory"], n["labor_capacity_base"],
         n.get("hourly_labor_cost", 20.0), n.get("units_per_hour", 10.0))
        for n in _NET["nodes"]
    ]
    LANES = [
        (l["id"], l["origin"], l["destination"], l["transport_cost"], "active")
        for l in _NET["lanes"]
    ]
else:
    # Hardcoded defaults (backward compatibility)
    LANE_TO_IB    = {1: 1, 2: 1, 3: 2, 4: 2}
    INBOUND_LANES = {3: [1], 4: [2, 3], 5: [4]}
    IB_FC_WEIGHTS = {1: {3: 1.0, 4: 0.5}, 2: {4: 0.5, 5: 1.0}}
    MAX_EXTERNAL_INFLOW = {1: 350, 2: 280}
    BASE_DEMAND   = {3: 70, 4: 90, 5: 55}
    LANE_TC_MAX   = {1: 350, 2: 300, 3: 280, 4: 250}
    LANE_TC_BASE  = {1: 200, 2: 180, 3: 160, 4: 150}
    DISRUPTION_PROBABILITY  = 0.30
    MAX_ACTIVE_DISRUPTIONS  = 3

    NODES = [
        (1, "IBCenter",           1200, 200, 1200, 150, 15.0, 20.0),
        (2, "IBCenter",           1000, 150, 1000, 120,  9.0, 12.0),
        (3, "FulfillmentCenter",   800, 100,  800, 100, 22.5, 15.0),
        (4, "FulfillmentCenter",   950, 120,  950, 125, 20.0, 18.0),
        (5, "FulfillmentCenter",   700,  80,  700,  78, 12.0, 10.0),
    ]
    LANES = [
        (1, 1, 3, 12.5, "active"),
        (2, 1, 4, 15.0, "active"),
        (3, 2, 4, 11.0, "active"),
        (4, 2, 5, 14.0, "active"),
    ]

# Derived: FC and IB node IDs
FC_IDS = [n[0] for n in NODES if n[1] == "FulfillmentCenter"]
IB_IDS = [n[0] for n in NODES if n[1] == "IBCenter"]


def get_conn(retries=5, base_delay=0.1):
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError:
            if attempt == retries - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id                   INTEGER PRIMARY KEY,
            type                 TEXT    NOT NULL,
            capacity             INTEGER NOT NULL,
            safety_stock         INTEGER NOT NULL,
            inventory            INTEGER NOT NULL,
            labor_capacity_base  INTEGER NOT NULL DEFAULT 0,
            hourly_labor_cost    REAL    NOT NULL DEFAULT 20.0,
            units_per_hour       REAL    NOT NULL DEFAULT 10.0
        );
        CREATE TABLE IF NOT EXISTS lanes (
            id             INTEGER PRIMARY KEY,
            origin         INTEGER NOT NULL REFERENCES nodes(id),
            destination    INTEGER NOT NULL REFERENCES nodes(id),
            transport_cost REAL    NOT NULL,
            status         TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tick         INTEGER NOT NULL,
            agent_name   TEXT    NOT NULL,
            action_taken TEXT    NOT NULL,
            reasoning    TEXT
        );
        CREATE TABLE IF NOT EXISTS demand (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tick           INTEGER NOT NULL,
            node_id        INTEGER NOT NULL REFERENCES nodes(id),
            orders         INTEGER NOT NULL,
            shipped        INTEGER NOT NULL,
            backlog        INTEGER NOT NULL,
            labor_capacity INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS disruptions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_started INTEGER NOT NULL,
            duration     INTEGER NOT NULL,
            type         TEXT    NOT NULL,
            target_id    INTEGER NOT NULL,
            severity     REAL    NOT NULL,
            description  TEXT    NOT NULL,
            resolved     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS labor_plan (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_planned  INTEGER NOT NULL,
            tick_for      INTEGER NOT NULL,
            node_id       INTEGER NOT NULL REFERENCES nodes(id),
            planned_labor INTEGER NOT NULL,
            flex_pct      REAL    NOT NULL DEFAULT 0.15,
            UNIQUE(tick_for, node_id)
        );
        CREATE TABLE IF NOT EXISTS transport_plan (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_planned     INTEGER NOT NULL,
            tick_for         INTEGER NOT NULL,
            lane_id          INTEGER NOT NULL REFERENCES lanes(id),
            planned_capacity INTEGER NOT NULL,
            flex_pct         REAL    NOT NULL DEFAULT 0.15,
            UNIQUE(tick_for, lane_id)
        );
        CREATE TABLE IF NOT EXISTS token_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tick          INTEGER NOT NULL,
            agent_name    TEXT    NOT NULL,
            input_tokens  INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_eur      REAL    NOT NULL,
            model_name    TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            tick      INTEGER NOT NULL,
            node_id   INTEGER NOT NULL REFERENCES nodes(id),
            inventory INTEGER NOT NULL,
            UNIQUE(tick, node_id)
        );
        CREATE TABLE IF NOT EXISTS transport (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            tick      INTEGER NOT NULL,
            lane_id   INTEGER NOT NULL REFERENCES lanes(id),
            qty       INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS demand_modifiers (
            node_id    INTEGER PRIMARY KEY REFERENCES nodes(id),
            multiplier REAL    NOT NULL DEFAULT 1.0,
            active     INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS financials (
            tick                  INTEGER PRIMARY KEY,
            total_labor_cost      REAL    NOT NULL,
            total_transport_cost  REAL    NOT NULL,
            total_units_shipped   INTEGER NOT NULL,
            cost_per_unit_shipped REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS forecast (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_created      INTEGER NOT NULL,
            forecast_for_tick INTEGER NOT NULL,
            node_id           INTEGER NOT NULL REFERENCES nodes(id),
            forecast_demand   REAL    NOT NULL,
            actual_demand     INTEGER,
            mae               REAL,
            method            TEXT    NOT NULL,
            UNIQUE(forecast_for_tick, node_id)
        );
        CREATE TABLE IF NOT EXISTS demand_modifiers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id    INTEGER NOT NULL REFERENCES nodes(id),
            multiplier REAL    NOT NULL DEFAULT 1.0,
            description TEXT,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_node_tick ON snapshots(node_id, tick);
        CREATE INDEX IF NOT EXISTS idx_token_log_tick      ON token_log(tick);
        CREATE INDEX IF NOT EXISTS idx_forecast_node_tick  ON forecast(forecast_for_tick, node_id);
        CREATE INDEX IF NOT EXISTS idx_disruptions_active  ON disruptions(resolved, tick_started);
    """)
    cur.execute("SELECT COUNT(*) FROM nodes")
    if cur.fetchone()[0] == 0:
        cur.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)", NODES)
        cur.executemany("INSERT INTO lanes VALUES (?,?,?,?,?)", LANES)
    conn.commit()
    conn.close()


def snapshot_tick(tick: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO snapshots (tick, node_id, inventory)
        SELECT ?, id, inventory FROM nodes
    """, (tick,))
    conn.commit()
    conn.close()


def run_tick(tick_number):
    conn = get_conn()
    cur = conn.cursor()
    print(f"\n{'='*68}")
    print(f"  TICK {tick_number:>4}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*68}")
    print(f"\n{'ID':<4} {'TYPE':<20} {'CAP':>6} {'SAFETY':>7} {'INV':>7} {'LABOR_BASE':>11}")
    print(f"{'-'*4} {'-'*20} {'-'*6} {'-'*7} {'-'*7} {'-'*11}")
    for row in cur.execute("SELECT * FROM nodes ORDER BY id"):
        print(f"{row['id']:<4} {row['type']:<20} {row['capacity']:>6} "
              f"{row['safety_stock']:>7} {row['inventory']:>7} {row['labor_capacity_base']:>11}")
    print(f"\n{'ID':<4} {'ORIGIN':>6} {'DEST':>6} {'TC':>8} {'STATUS':<10}")
    print(f"{'-'*4} {'-'*6} {'-'*6} {'-'*8} {'-'*10}")
    for row in cur.execute("SELECT * FROM lanes ORDER BY id"):
        print(f"{row['id']:<4} {row['origin']:>6} {row['destination']:>6} "
              f"{row['transport_cost']:>8.2f} {row['status']:<10}")
    cur.execute("""
        SELECT node_id, orders, shipped, backlog, labor_capacity FROM demand
        WHERE tick = (SELECT MAX(tick) FROM demand) ORDER BY node_id
    """)
    rows = cur.fetchall()
    if rows:
        print(f"\n{'NODE':>5} {'ORDERS':>8} {'SHIPPED':>8} {'BACKLOG':>8} {'LABOR_CAP':>10}")
        print(f"{'-'*5} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")
        for row in rows:
            print(f"{row['node_id']:>5} {row['orders']:>8} {row['shipped']:>8} "
                  f"{row['backlog']:>8} {row['labor_capacity']:>10}")
    cur.execute("SELECT COUNT(*) FROM log WHERE tick=?", (tick_number,))
    print(f"\nLog entries this tick: {cur.fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    run_tick(0)
    print(f"\nDatabase initialized at: {os.path.abspath(DB_PATH)}\n")

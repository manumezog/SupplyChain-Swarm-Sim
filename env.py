import sqlite3
import time
import os

DB_PATH = "sim_state.db"

# IBCenter = Inbound Center (formerly SortCenter): receives stock, ships to FulfillmentCenters
LANE_TO_IB    = {1: 1, 2: 1, 3: 2, 4: 2}    # lane_id -> source IBCenter node_id
INBOUND_LANES = {3: [1], 4: [2, 3], 5: [4]}  # FC node_id -> list of inbound lane ids
# Weighted share of each FC's demand borne by each IB (used by supply agent for target inventory)
IB_FC_WEIGHTS = {1: {3: 1.0, 4: 0.5}, 2: {4: 0.5, 5: 1.0}}

# (id, type, capacity, safety_stock, inventory, labor_capacity_base)
# IBCenter labor_capacity_base = outbound dispatch capacity (units/tick they can load)
NODES = [
    (1, "IBCenter",           1200, 200, 1200, 150),
    (2, "IBCenter",           1000, 150, 1000, 120),
    (3, "FulfillmentCenter",   800, 100,  800, 100),
    (4, "FulfillmentCenter",   950, 120,  950, 125),
    (5, "FulfillmentCenter",   700,  80,  700,  78),
]

# transport_cost = cost per unit shipped on this lane (economic efficiency metric)
# Higher cost = more expensive route, NOT more disruptible
LANES = [
    (1, 1, 3, 12.5, "active"),
    (2, 1, 4, 15.0, "active"),
    (3, 2, 4, 11.0, "active"),
    (4, 2, 5, 14.0, "active"),
]


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
            labor_capacity_base  INTEGER NOT NULL DEFAULT 0
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
            action_taken TEXT    NOT NULL
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
            cost_eur      REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            tick      INTEGER NOT NULL,
            node_id   INTEGER NOT NULL REFERENCES nodes(id),
            inventory INTEGER NOT NULL,
            UNIQUE(tick, node_id)
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
        CREATE INDEX IF NOT EXISTS idx_snapshots_node_tick ON snapshots(node_id, tick);
        CREATE INDEX IF NOT EXISTS idx_token_log_tick      ON token_log(tick);
        CREATE INDEX IF NOT EXISTS idx_forecast_node_tick  ON forecast(forecast_for_tick, node_id);
        CREATE INDEX IF NOT EXISTS idx_disruptions_active  ON disruptions(resolved, tick_started);
    """)
    cur.execute("SELECT COUNT(*) FROM nodes")
    if cur.fetchone()[0] == 0:
        cur.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?)", NODES)
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

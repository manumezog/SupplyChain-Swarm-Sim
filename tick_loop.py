import sqlite3
import subprocess
import time
import sys
import os

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add project dir to path so env.py is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as _env

DB_PATH   = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
PYTHON    = sys.executable
INTERVAL  = 15  # seconds between ticks

# Execution order is strictly enforced — see README for rationale
AGENTS = [
    "forecast_action.py",    # 1. forecast demand for next tick
    "staffing_action.py",    # 2. plan labor & transport capacity for next tick
    "demand_action.py",      # 3. consume FC inventory (uses this tick's labor plan)
    "supply_action.py",      # 4. replenish IBCenters from external suppliers
    "disruptor_action.py",   # 5. probabilistic disruption event
    "repair_action.py",      # 6. resolve highest-priority prior-tick disruption
    "planner_action.py",     # 7. replenish FCs from IBCenters (uses transport plan)
]


def get_next_tick():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur  = conn.cursor()
    cur.execute("SELECT MAX(tick) FROM log")
    row  = cur.fetchone()
    conn.close()
    last = row[0]
    return (last + 1) if last is not None else 1


def run_agent(script, tick):
    agent_env = os.environ.copy()
    agent_env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [PYTHON, script, str(tick)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=agent_env,
    )
    label = os.path.basename(script).replace("_action.py", "").upper()
    for line in result.stdout.strip().splitlines():
        print(f"  [{label}] {line}")
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            print(f"  [{label}][ERR] {line}")


def print_separator(tick):
    print(f"\n{'='*54}")
    print(f"  TICK {tick:>4}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*54}")


if __name__ == "__main__":
    print(f"Tick loop started -- interval: {INTERVAL}s")
    print(f"Order: {' > '.join(a.replace('_action.py','') for a in AGENTS)}")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            tick = get_next_tick()
            print_separator(tick)
            for agent in AGENTS:
                run_agent(agent, tick)
            _env.snapshot_tick(tick)   # capture per-node inventory after all agents settle
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print(f"\nTick loop stopped at tick {get_next_tick() - 1}.")

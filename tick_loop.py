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
INTERVAL  = 2   # seconds between ticks

# Agents run in dependency order; supply+disruptor are parallelized (independent after demand)
SEQUENTIAL_PRE  = ["forecast_action.py", "staffing_action.py", "demand_action.py"]
PARALLEL_MIDDLE = ["supply_action.py", "disruptor_action.py"]   # run concurrently
SEQUENTIAL_POST = ["repair_action.py", "planner_action.py"]


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


def run_agent_parallel(scripts, tick):
    """Launch multiple agents concurrently, wait for all, then print output."""
    agent_env = os.environ.copy()
    agent_env["PYTHONIOENCODING"] = "utf-8"
    procs = []
    for script in scripts:
        p = subprocess.Popen(
            [PYTHON, script, str(tick)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=agent_env,
        )
        procs.append((script, p))
    for script, p in procs:
        stdout, stderr = p.communicate()
        label = os.path.basename(script).replace("_action.py", "").upper()
        for line in stdout.strip().splitlines():
            print(f"  [{label}] {line}")
        if stderr.strip():
            for line in stderr.strip().splitlines():
                print(f"  [{label}][ERR] {line}")


def print_separator(tick):
    print(f"\n{'='*54}")
    print(f"  TICK {tick:>4}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*54}")


if __name__ == "__main__":
    order_desc = (
        " > ".join(a.replace("_action.py", "") for a in SEQUENTIAL_PRE)
        + " > [supply||disruptor] > "
        + " > ".join(a.replace("_action.py", "") for a in SEQUENTIAL_POST)
    )
    print(f"Tick loop started -- interval: {INTERVAL}s")
    print(f"Order: {order_desc}")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            tick = get_next_tick()
            print_separator(tick)
            for agent in SEQUENTIAL_PRE:
                run_agent(agent, tick)
            run_agent_parallel(PARALLEL_MIDDLE, tick)
            for agent in SEQUENTIAL_POST:
                run_agent(agent, tick)
            _env.snapshot_tick(tick)
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print(f"\nTick loop stopped at tick {get_next_tick() - 1}.")

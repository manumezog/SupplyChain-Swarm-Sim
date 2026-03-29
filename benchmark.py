"""
SwarmChain-Sim · Benchmark Mode
Run the simulation with a fixed seed across different model configs,
then compare KPIs to see if cheaper models make worse decisions.

Usage:
    python benchmark.py --models "gemini-2.0-flash,claude-3-5-sonnet-20241022" --ticks 10 --seed 42
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(PROJECT_DIR, "sim_state.db")
CONFIG_PATH = os.path.join(PROJECT_DIR, "model_config.json")
PYTHON      = sys.executable


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def reset_db():
    """Re-initialize the database from scratch."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    subprocess.run(
        [PYTHON, os.path.join(PROJECT_DIR, "env.py")],
        capture_output=True, text=True, cwd=PROJECT_DIR, encoding="utf-8",
    )


def run_simulation(ticks: int, seed: int):
    """Run tick_loop.py for a fixed number of ticks with a seed."""
    result = subprocess.run(
        [PYTHON, os.path.join(PROJECT_DIR, "tick_loop.py"),
         "--seed", str(seed), "--ticks", str(ticks)],
        capture_output=True, text=True, cwd=PROJECT_DIR, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
    if result.stderr.strip():
        # Only print errors that aren't just warnings
        for line in result.stderr.strip().splitlines():
            if "Error" in line or "Exception" in line:
                print(f"  [ERR] {line}")


def collect_kpis() -> dict:
    """Read KPIs from the current sim_state.db."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()

    # Total backlog
    cur.execute("SELECT COALESCE(SUM(backlog), 0) FROM demand")
    total_backlog = cur.fetchone()[0]

    # Peak backlog
    cur.execute("SELECT COALESCE(MAX(backlog), 0) FROM demand")
    peak_backlog = cur.fetchone()[0]

    # Stockout events (ticks where any FC had zero inventory)
    cur.execute("""
        SELECT COUNT(*) FROM snapshots
        WHERE inventory = 0 AND node_id IN (
            SELECT id FROM nodes WHERE type='FulfillmentCenter'
        )
    """)
    stockout_events = cur.fetchone()[0]

    # Average forecast MAE
    cur.execute("SELECT COALESCE(AVG(mae), 0) FROM forecast WHERE mae IS NOT NULL")
    avg_mae = round(cur.fetchone()[0], 2)

    # Disruptions triggered
    cur.execute("SELECT COUNT(*) FROM disruptions")
    disruptions_total = cur.fetchone()[0]

    # Total shipped
    cur.execute("SELECT COALESCE(SUM(shipped), 0) FROM demand")
    total_shipped = cur.fetchone()[0]

    # Total orders
    cur.execute("SELECT COALESCE(SUM(orders), 0) FROM demand")
    total_orders = cur.fetchone()[0]

    # Fill rate
    fill_rate = round((total_shipped / total_orders * 100) if total_orders > 0 else 0, 1)

    # API Cost
    cur.execute("SELECT COALESCE(SUM(cost_eur), 0) FROM token_log")
    total_cost_eur = round(cur.fetchone()[0], 5)

    # Total tokens
    cur.execute("SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) FROM token_log")
    tok = cur.fetchone()
    total_input_tokens = tok[0]
    total_output_tokens = tok[1]

    # Ticks completed
    cur.execute("SELECT COALESCE(MAX(tick), 0) FROM log")
    ticks_completed = cur.fetchone()[0]

    # Network Execution Cost (Labor + Transport)
    cur.execute("SELECT SUM(total_labor_cost + total_transport_cost), AVG(cost_per_unit_shipped) FROM financials")
    fin = cur.fetchone()
    total_execution_cost = round(fin[0] or 0, 2)
    avg_cpu = round(fin[1] or 0, 2)

    conn.close()

    return {
        "ticks_completed": ticks_completed,
        "total_backlog": total_backlog,
        "peak_backlog": peak_backlog,
        "stockout_events": stockout_events,
        "avg_forecast_mae": avg_mae,
        "fill_rate_pct": fill_rate,
        "total_shipped": total_shipped,
        "total_orders": total_orders,
        "disruptions_total": disruptions_total,
        "total_cost_eur": total_cost_eur,
        "total_execution_cost": total_execution_cost,
        "avg_cpu": avg_cpu,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }


def print_comparison(results: list[dict]):
    """Print a comparison table of all benchmark runs."""
    print("\n" + "=" * 80)
    print("  BENCHMARK RESULTS")
    print("=" * 80)

    metrics = [
        ("Ticks Completed",  "ticks_completed"),
        ("Fill Rate",        "fill_rate_pct", "%"),
        ("Total Shipped",    "total_shipped"),
        ("Total Backlog",    "total_backlog"),
        ("Peak Backlog",     "peak_backlog"),
        ("Stockout Events",  "stockout_events"),
        ("Avg Forecast MAE", "avg_forecast_mae"),
        ("Disruptions",      "disruptions_total"),
        ("Network Exec Cost", "total_execution_cost", " EUR"),
        ("Cost Per Unit",    "avg_cpu", " EUR"),
        ("API Token Cost",   "total_cost_eur", " EUR"),
        ("Input Tokens",     "total_input_tokens"),
        ("Output Tokens",    "total_output_tokens"),
    ]

    # Header
    model_names = [r["model"] for r in results]
    col_width = max(16, max(len(m) for m in model_names) + 2)
    header = f"{'Metric':<22}" + "".join(f"{m:>{col_width}}" for m in model_names)
    print(f"\n{header}")
    print("-" * len(header))

    for metric in metrics:
        label = metric[0]
        key = metric[1]
        suffix = metric[2] if len(metric) > 2 else ""
        row = f"{label:<22}"
        for r in results:
            val = r["kpis"][key]
            if isinstance(val, float):
                row += f"{val:>{col_width - len(suffix)}.4f}{suffix}"
            else:
                row += f"{val:>{col_width - len(suffix)}}{suffix}"
        print(row)

    # Winner summary
    if len(results) > 1:
        print(f"\n{'─' * 40}")
        best_fill = max(results, key=lambda r: r["kpis"]["fill_rate_pct"])
        best_cost = min(results, key=lambda r: r["kpis"]["total_cost_eur"])
        best_mae  = min(results, key=lambda r: r["kpis"]["avg_forecast_mae"])

        print(f"  Best Fill Rate:    {best_fill['model']} ({best_fill['kpis']['fill_rate_pct']}%)")
        print(f"  Lowest Cost:       {best_cost['model']} (€{best_cost['kpis']['total_cost_eur']:.5f})")
        print(f"  Best Forecast MAE: {best_mae['model']} ({best_mae['kpis']['avg_forecast_mae']})")


def main():
    parser = argparse.ArgumentParser(description="SwarmChain-Sim Benchmark")
    parser.add_argument("--models", type=str, required=True,
                        help="Comma-separated model names, e.g. 'gemini-2.0-flash,claude-3-5-sonnet-20241022'")
    parser.add_argument("--ticks", type=int, default=10,
                        help="Number of ticks per run (default: 10)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for deterministic runs (default: 42)")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",")]
    original_config = load_config()
    results = []

    print(f"╔═══════════════════════════════════════════════════════╗")
    print(f"║  SwarmChain-Sim · Benchmark Mode                      ║")
    print(f"║  Models: {', '.join(models):<45} ║")
    print(f"║  Ticks: {args.ticks:<10} Seed: {args.seed:<26} ║")
    print(f"╚═══════════════════════════════════════════════════════╝\n")

    for model in models:
        print(f"\n{'─' * 60}")
        print(f"  Running benchmark: {model} ({args.ticks} ticks, seed={args.seed})")
        print(f"{'─' * 60}")

        # Set all agents to this model
        cfg = load_config()
        for agent in cfg.get("agents", {}):
            cfg["agents"][agent]["model"] = model
        cfg["default_model"] = model
        save_config(cfg)

        # Reset and run
        reset_db()
        t_start = time.time()
        run_simulation(args.ticks, args.seed)
        elapsed = time.time() - t_start

        # Collect KPIs
        kpis = collect_kpis()
        results.append({
            "model": model,
            "seed": args.seed,
            "ticks": args.ticks,
            "elapsed_seconds": round(elapsed, 1),
            "kpis": kpis,
        })

        print(f"\n  ✓ {model} completed in {elapsed:.1f}s")

    # Restore original config
    save_config(original_config)

    # Print comparison
    print_comparison(results)

    # Save to file
    output_path = os.path.join(PROJECT_DIR, "benchmark_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()

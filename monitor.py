import sqlite3
import time
import sys

try:
    from rich.live    import Live
    from rich.table   import Table
    from rich.panel   import Panel
    from rich.layout  import Layout
    from rich.text    import Text
    from rich.console import Console, Group
    RICH = True
except ImportError:
    RICH = False
    print("rich not installed. Run: pip install rich\nFalling back to plain output.\n")

DB_PATH = "C:/Users/manum/Desktop/IA Projects/SupplyChain-Swarm-Sim/sim_state.db"
REFRESH = 2

AGENT_COLORS = {
    "Disruptor": "red",
    "Repair":    "green",
    "Planner":   "yellow",
    "Demand":    "cyan",
    "Supply":    "blue",
    "Forecast":  "magenta",
    "Staffing":  "bright_blue",
}
DISRUPTION_TYPE_COLORS = {
    "lane":    "orange3",
    "ib_node": "dark_orange",
    "fc_node": "red",
}
SPARK_CHARS = ["\u2581","\u2582","\u2583","\u2584","\u2585","\u2586","\u2587","\u2588"]


def query(sql, params=()):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def sparkline(values, width=15, scale_max=None):
    if not values:
        return " " * width
    values = values[-width:]
    mx = scale_max if (scale_max and scale_max > 0) else (max(values) or 1)
    rng = mx or 1
    chars = [SPARK_CHARS[min(7, int(v / rng * 8))] for v in values]
    return " " * (width - len(chars)) + "".join(chars)


def inv_bar(inventory, capacity, safety_stock, bar_width=14):
    pct    = inventory / capacity if capacity else 0
    filled = round(pct * bar_width)
    bar    = "\u2588" * filled + "\u2591" * (bar_width - filled)
    pct_s  = f"{pct*100:5.1f}%"
    if inventory <= 0:        style = "red bold"
    elif inventory < safety_stock: style = "yellow"
    else:                         style = "green"
    return Text(f"{bar} {pct_s}", style=style)


def get_current_tick():
    rows = query("SELECT MAX(tick) FROM log")
    v = rows[0][0] if rows else None
    return v or 0


# ── panels ───────────────────────────────────────────────────────────────────

def build_inventory_panel():
    t = Table(title="Inventory Health", box=None, header_style="bold white", expand=True)
    t.add_column("ID",    width=3,  justify="right")
    t.add_column("Type",  width=7)
    t.add_column("Inv",   width=5,  justify="right")
    t.add_column("Cap",   width=5,  justify="right")
    t.add_column("Safe",  width=5,  justify="right")
    t.add_column("Bar",   width=22)
    t.add_column("20t Trend", width=17)

    for r in query("SELECT id, type, inventory, capacity, safety_stock FROM nodes ORDER BY id"):
        snaps = query(
            "SELECT inventory FROM snapshots WHERE node_id=? ORDER BY tick DESC LIMIT 20",
            (r["id"],),
        )
        snap_vals = list(reversed([s["inventory"] for s in snaps]))
        spark = sparkline(snap_vals, width=15, scale_max=r["capacity"])
        short = "IB" if r["type"] == "IBCenter" else "Fulfil"
        t.add_row(
            str(r["id"]), short,
            str(r["inventory"]), str(r["capacity"]), str(r["safety_stock"]),
            inv_bar(r["inventory"], r["capacity"], r["safety_stock"]),
            Text(spark, style="white"),
        )
    return Panel(t, border_style="dim")


def build_network_panel():
    tick = get_current_tick()

    # Lanes table — show transport_cost and current disruption severity if any
    tl = Table(title="Lanes (TC = transport cost/unit)", box=None, header_style="bold white", expand=True)
    tl.add_column("ID",    width=3, justify="right")
    tl.add_column("Route", width=7)
    tl.add_column("TC",    width=5, justify="right")
    tl.add_column("Status",width=10)
    tl.add_column("Sev",   width=5, justify="right")

    for r in query("""
        SELECT l.id, l.origin, l.destination, l.transport_cost, l.status,
               COALESCE(
                   (SELECT MAX(severity) FROM disruptions
                    WHERE type='lane' AND target_id=l.id AND resolved=0
                      AND (tick_started+duration) > ?), 0
               ) AS severity
        FROM lanes l ORDER BY l.id
    """, (tick,)):
        status_style = "green" if r["status"] == "active" else "red bold"
        sev_str = f"{r['severity']:.0%}" if r["severity"] > 0 else "--"
        sev_style = "red" if r["severity"] > 0.6 else ("yellow" if r["severity"] > 0 else "dim")
        tl.add_row(
            str(r["id"]),
            f"{r['origin']}->{r['destination']}",
            f"{r['transport_cost']:.1f}",
            Text(r["status"], style=status_style),
            Text(sev_str, style=sev_style),
        )

    # Active node disruptions
    td = Table(title="Active Node Disruptions", box=None, header_style="bold white", expand=True)
    td.add_column("Target", width=6)
    td.add_column("Sev",    width=5, justify="right")
    td.add_column("Rem",    width=4, justify="right")
    td.add_column("Event",  width=30, no_wrap=False, overflow="fold")

    node_events = query("""
        SELECT type, target_id, severity, description,
               (tick_started + duration - ?) AS remaining
        FROM disruptions
        WHERE resolved=0 AND type IN ('ib_node','fc_node')
          AND (tick_started + duration) > ?
        ORDER BY severity DESC
    """, (tick, tick))

    if not node_events:
        td.add_row("[dim]none[/dim]", "", "", "")
    for r in node_events:
        node_label = f"IB{r['target_id']}" if r["type"] == "ib_node" else f"FC{r['target_id']}"
        color = DISRUPTION_TYPE_COLORS.get(r["type"], "white")
        td.add_row(
            Text(node_label, style=color),
            f"{r['severity']:.0%}",
            str(max(0, r["remaining"])),
            r["description"],
        )

    return Panel(Group(tl, td), title="Network & Disruptions", border_style="dim")


def build_demand_panel():
    tick = get_current_tick()
    t = Table(title=f"Demand + Forecast + Labor (tick {tick})", box=None, header_style="bold white", expand=True)
    t.add_column("FC",      width=3,  justify="right")
    t.add_column("Orders",  width=7,  justify="right")
    t.add_column("Fcst",    width=6,  justify="right")
    t.add_column("MAE",     width=5,  justify="right")
    t.add_column("Labor",   width=8,  justify="right")  # actual/base
    t.add_column("Ship",    width=5,  justify="right")
    t.add_column("Backlog", width=7,  justify="right")
    t.add_column("20t Trend", width=17)

    rows = query("""
        SELECT d.node_id, d.orders, d.shipped, d.backlog, d.labor_capacity,
               n.labor_capacity_base,
               f.forecast_demand, f.mae, f.method
        FROM demand d
        JOIN nodes n ON n.id = d.node_id
        LEFT JOIN forecast f ON f.node_id=d.node_id AND f.forecast_for_tick=d.tick
        WHERE d.tick = (SELECT MAX(tick) FROM demand)
        ORDER BY d.node_id
    """)

    if not rows:
        t.add_row("[dim]--[/dim]", "", "", "", "", "", "", "")

    for r in rows:
        demand_hist = query(
            "SELECT orders FROM demand WHERE node_id=? ORDER BY tick DESC LIMIT 20",
            (r["node_id"],),
        )
        dvals = list(reversed([x["orders"] for x in demand_hist]))
        spark = sparkline(dvals, width=15)

        mae_val   = f"{r['mae']:.1f}" if r["mae"] is not None else "--"
        mae_style = "white" if r["mae"] is None else ("green" if r["mae"] < 10 else ("yellow" if r["mae"] < 25 else "red"))

        fcst       = f"{r['forecast_demand']:.0f}" if r["forecast_demand"] is not None else "--"
        fcst_style = "dim" if (r["method"] in ("fallback","last_value")) else "white"

        backlog_style = "red bold" if r["backlog"] > 0 else "green"

        labor_str   = f"{r['labor_capacity']}/{r['labor_capacity_base']}"
        labor_style = "yellow" if r["labor_capacity"] < r["labor_capacity_base"] * 0.85 else "white"
        if r["labor_capacity"] < r["labor_capacity_base"] * 0.50:
            labor_style = "red bold"

        t.add_row(
            str(r["node_id"]),
            str(r["orders"]),
            Text(fcst, style=fcst_style),
            Text(mae_val, style=mae_style),
            Text(labor_str, style=labor_style),
            str(r["shipped"]),
            Text(str(r["backlog"]), style=backlog_style),
            Text(spark, style="white"),
        )
    return Panel(t, border_style="dim")


def build_token_panel():
    tick = get_current_tick()
    this_tick  = query("SELECT SUM(cost_eur),SUM(input_tokens),SUM(output_tokens) FROM token_log WHERE tick=?", (tick,))
    cumulative = query("SELECT SUM(cost_eur),SUM(input_tokens),SUM(output_tokens) FROM token_log")
    tc  = this_tick[0]  if this_tick  else (0,0,0)
    cum = cumulative[0] if cumulative else (0,0,0)
    tick_cost  = tc[0]  or 0.0
    total_cost = cum[0] or 0.0

    cost_hist  = query("SELECT SUM(cost_eur) as c FROM token_log GROUP BY tick ORDER BY tick DESC LIMIT 20")
    cost_vals  = list(reversed([r["c"] for r in cost_hist]))
    cost_spark = sparkline(cost_vals, width=15)

    t = Table(box=None, header_style="bold white", expand=True)
    t.add_column("Metric", width=14)
    t.add_column("Value",  width=22)
    t.add_row("This tick",  Text(f"EUR {tick_cost:.5f}", style="cyan"))
    t.add_row("Cumulative", Text(f"EUR {total_cost:.4f}", style="bold cyan"))
    t.add_row("Cost trend", Text(cost_spark, style="cyan"))
    t.add_row("", "")

    t2 = Table(box=None, header_style="bold white", show_header=True, expand=True)
    t2.add_column("T",     width=4, justify="right")
    t2.add_column("Agent", width=10)
    t2.add_column("In",    width=5, justify="right")
    t2.add_column("Out",   width=5, justify="right")
    t2.add_column("EUR",   width=10, justify="right")
    for r in query("SELECT tick,agent_name,input_tokens,output_tokens,cost_eur FROM token_log ORDER BY id DESC LIMIT 6"):
        color = AGENT_COLORS.get(r["agent_name"], "white")
        t2.add_row(str(r["tick"]), Text(r["agent_name"], style=color),
                   str(r["input_tokens"]), str(r["output_tokens"]), f"{r['cost_eur']:.5f}")

    return Panel(Group(t, t2), title="Token Cost (Opus 4.6 — real API)", border_style="dim")


def build_log_panel():
    t = Table(title="Recent Agent Actions", box=None, header_style="bold white", expand=True)
    t.add_column("T",      width=4, justify="right")
    t.add_column("Agent",  width=10)
    t.add_column("Action", no_wrap=False, overflow="fold")
    for r in query("SELECT tick, agent_name, action_taken FROM log ORDER BY id DESC LIMIT 8"):
        color  = AGENT_COLORS.get(r["agent_name"], "white")
        action = r["action_taken"][:115] + "..." if len(r["action_taken"]) > 115 else r["action_taken"]
        t.add_row(str(r["tick"]), Text(r["agent_name"], style=color), action)
    return Panel(t, border_style="dim")


# ── layout ───────────────────────────────────────────────────────────────────

def build_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="top",    ratio=5),
        Layout(name="mid",    ratio=5),
        Layout(name="bottom", ratio=6),
    )
    layout["top"].split_row(
        Layout(name="inventory", ratio=6),
        Layout(name="network",   ratio=3),
    )
    layout["mid"].split_row(
        Layout(name="demand",     ratio=5),
        Layout(name="token_cost", ratio=4),
    )
    return layout


def refresh_layout(layout):
    tick = get_current_tick()
    layout["header"].update(Panel(
        Text(f"  SWARMCHAIN-SIM  |  Tick {tick}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}", style="bold white"),
        style="blue",
    ))
    layout["inventory"].update(build_inventory_panel())
    layout["network"].update(build_network_panel())
    layout["demand"].update(build_demand_panel())
    layout["token_cost"].update(build_token_panel())
    layout["bottom"].update(build_log_panel())


# ── fallback ─────────────────────────────────────────────────────────────────

def fallback_monitor():
    import os as _os
    while True:
        _os.system("cls" if _os.name == "nt" else "clear")
        tick = get_current_tick()
        print(f"=== SWARMCHAIN-SIM | Tick {tick} | {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        for r in query("SELECT id, type, inventory, capacity FROM nodes ORDER BY id"):
            print(f"  {r['type'][:2]}{r['id']}: inv={r['inventory']}/{r['capacity']}")
        print()
        for r in query("SELECT id, origin, destination, transport_cost, status FROM lanes ORDER BY id"):
            print(f"  Lane {r['id']} ({r['origin']}->{r['destination']}) TC={r['transport_cost']}: {r['status']}")
        print()
        active_d = query("SELECT type,target_id,severity,description FROM disruptions WHERE resolved=0 AND (tick_started+duration)>?", (tick,))
        if active_d:
            print("  Active disruptions:")
            for r in active_d:
                print(f"    [{r['type']}] target={r['target_id']} sev={r['severity']:.0%}: {r['description']}")
        print()
        rows = query("""
            SELECT d.node_id,d.orders,d.shipped,d.backlog,d.labor_capacity,n.labor_capacity_base,
                   f.forecast_demand,f.mae
            FROM demand d JOIN nodes n ON n.id=d.node_id
            LEFT JOIN forecast f ON f.node_id=d.node_id AND f.forecast_for_tick=d.tick
            WHERE d.tick=(SELECT MAX(tick) FROM demand) ORDER BY d.node_id
        """)
        for r in rows:
            mae = f"{r['mae']:.1f}" if r["mae"] else "--"
            print(f"  FC{r['node_id']}: orders={r['orders']} fcst={r['forecast_demand']} mae={mae}"
                  f" labor={r['labor_capacity']}/{r['labor_capacity_base']} shipped={r['shipped']} bl={r['backlog']}")
        print()
        cum = query("SELECT SUM(cost_eur) FROM token_log")
        print(f"  Cumulative cost: EUR {(cum[0][0] or 0):.4f}")
        for r in query("SELECT tick,agent_name,action_taken FROM log ORDER BY id DESC LIMIT 5"):
            print(f"  [T{r['tick']}] {r['agent_name']}: {r['action_taken'][:80]}")
        time.sleep(REFRESH)


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not RICH:
        fallback_monitor()
    else:
        console = Console()
        layout  = build_layout()
        with Live(layout, console=console, refresh_per_second=1, screen=True) as live:
            try:
                while True:
                    refresh_layout(layout)
                    time.sleep(REFRESH)
            except KeyboardInterrupt:
                pass

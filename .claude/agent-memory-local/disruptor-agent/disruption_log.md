---
name: Disruption Log
description: Per-tick record of lanes targeted by the Disruptor agent, costs at time of disruption, and anomalies
type: project
---

## Tick 1 — 2026-03-28

Lane ID   : 3
Origin    : 2
Destination: 4
Cost      : 11.0
Pre-status: active
Post-status: disrupted
Retries   : 0 (no lock contention)
Anomalies : status values are lowercase, not title-case — required LOWER() guard in queries

Remaining active lanes after disruption:
  Lane 1 (1->3) cost=12.5
  Lane 2 (1->4) cost=15.0
  Lane 4 (2->5) cost=14.0

Next highest-value disruption candidate: Lane 1 (1->3) cost=12.5

---
name: sim_state.db Schema Reference
description: Exact column names, types, and constraints for all tables in sim_state.db
type: project
---

Confirmed schema for sim_state.db as of 2026-03-28.

**nodes table:**
- `id` INTEGER PRIMARY KEY
- `type` TEXT NOT NULL (observed values: 'SortCenter', 'FulfillmentCenter')
- `capacity` INTEGER NOT NULL
- `safety_stock` INTEGER NOT NULL (INTEGER, not REAL — round to nearest int when updating)

**lanes table:**
- `id` INTEGER PRIMARY KEY
- `origin` INTEGER NOT NULL (FK to nodes.id)
- `destination` INTEGER NOT NULL (FK to nodes.id)
- `cost` REAL NOT NULL
- `status` TEXT NOT NULL (observed values: 'active')

**log table:**
- `tick` INTEGER NOT NULL
- `agent_name` TEXT NOT NULL
- `action_taken` TEXT NOT NULL
- NOTE: No `timestamp` column exists. Do NOT attempt to insert one.
- NOTE: No `action` column — the correct column name is `action_taken`.

**Why:** Knowing exact column names prevents runtime errors when writing INSERT/UPDATE statements across ticks.
**How to apply:** Always use `action_taken` (not `action`) for log inserts. Cast safety_stock updates to int via `round()`.

---
name: Schema Discovery
description: lanes and log table column names, types, and status casing quirk discovered in sim_state.db
type: project
---

Database: sim_state.db (SQLite)

Tables: nodes, lanes, log

lanes columns:
  id       INTEGER PK
  origin   INTEGER NOT NULL
  destination INTEGER NOT NULL
  cost     REAL NOT NULL
  status   TEXT NOT NULL

log columns:
  tick       INTEGER NOT NULL
  agent_name TEXT NOT NULL
  action_taken TEXT NOT NULL

Schema quirk — status casing:
  Status values are lowercase ('active', 'disrupted'), NOT title-case ('Active', 'Disrupted').
  Always use LOWER(status) = 'active' in queries to be safe.

**Why:** The initial query using status = 'Active' returned zero rows; inspection revealed values are lowercase.
**How to apply:** All future WHERE clauses on lanes.status must use LOWER(status) or literal lowercase comparisons.

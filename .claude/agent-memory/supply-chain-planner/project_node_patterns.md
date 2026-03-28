---
name: Node Safety Stock Baseline — Tick 1
description: Baseline safety_stock values for all nodes before Planner interventions began
type: project
---

Initial safety_stock values at simulation start (before tick 1 Planner action):

| node id | type              | safety_stock |
|---------|-------------------|--------------|
| 1       | SortCenter        | 200          |
| 2       | SortCenter        | 150          |
| 3       | FulfillmentCenter | 100          |
| 4       | FulfillmentCenter | 120          |
| 5       | FulfillmentCenter | 80           |  <- lowest, targeted at tick 1

After tick 1 Planner action: node 5 safety_stock updated from 80 to 96 (+20%).

Node 5 (FulfillmentCenter) was the chronic lowest-stock node at simulation start.

**Why:** Tracking which nodes are historically lowest helps prioritize future interventions.
**How to apply:** If node 5 continues to be the lowest after other ticks, it may warrant structural capacity changes rather than repeated stock bumps.

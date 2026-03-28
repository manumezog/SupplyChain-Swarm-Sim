# Project: Adversarial Supply Chain Swarm (Token-Optimized)

## Architecture
- **State Machine:** SQLite (`sim_state.db`). All agents communicate ONLY by reading/writing to this database. No direct agent-to-agent chatter.
- **Environment:** Python 3.12+ script (`env.py`) using `sqlite3` and `pandas`.
- **Agents:** Claude Code sub-agents spawned via CLI.

## Rules for Claude Code
1. **Frugality First:** Write dense, modular code. Do not output conversational filler like "Here is the code..." or "Let me know if you need changes."
2. **No Mock APIs:** Do not use external APIs. Generate synthetic data locally using standard Python libraries (`random`, `math`, `datetime`).
3. **Database Schema Strictness:** Never alter the SQLite schema without explicit permission.
4. **Error Handling:** If an agent encounters a database lock (`sqlite3.OperationalError`), implement exponential backoff rather than failing or looping infinitely.
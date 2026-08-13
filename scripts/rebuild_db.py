#!/usr/bin/env python3
"""
Rebuild the local SQLite snapshot (data/crm.db) FROM the CSVs in data/ — fully offline.

Why this exists: the CSVs are the durable, editable source of truth. Edit a number in a
CSV, run this, and the agent picks it up — no live warehouse, no credentials.

The actual rebuild logic lives in src/data.py (`rebuild_from_csv`) so the agent can also
self-heal a stale snapshot on startup; this is just the manual CLI entrypoint.

Usage:  .venv/bin/python scripts/rebuild_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import data  # noqa: E402

if __name__ == "__main__":
    n = data.rebuild_from_csv()
    print(f"Rebuilt {data.DB_PATH} from {n} CSVs — fully offline, no live warehouse needed.")

"""
Data-access layer over the local synthetic CRM snapshot (SQLite).

Everything the agent knows about an account is read through here — one small,
auditable surface. No SQL lives anywhere else, so the grounding story is simple:
every fact the agent states comes from one of these functions.

The snapshot is built from the CSVs in data/ (see ../data/MANIFEST.md); swapping
back to a live warehouse would only mean changing `_connect()`.
"""
from __future__ import annotations

import csv as _csv
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Project root = parent of src/.  DB path overridable via env for tests / live swap.
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "crm.db"
_DATA_DIR = _DEFAULT_DB.parent
DB_PATH = Path(os.environ.get("APP_DB", _DEFAULT_DB))

# "Today" is frozen to the data horizon, NOT wall-clock.  Activities end 2026-05-31
# and usage ends 2026-05; anchoring recency / close-proximity to real time would make
# every account look stale and Onyx Partners' "closes in 3 weeks" false.
ASOF: date = date(2026, 6, 1)


# --- rebuild the snapshot FROM the CSVs (no live warehouse) -------------------
# The CSVs in data/ are the durable source of truth; these helpers turn a CSV
# edit into a fresh DB with zero credentials. Column types are inferred
# per-column so numbers come back int/float and IDs/dates stay text.

def _looks_int(s: str) -> bool:
    try:
        int(s); return True
    except ValueError:
        return False


def _looks_float(s: str) -> bool:
    try:
        float(s); return True
    except ValueError:
        return False


def _column_type(values: list[str]) -> str:
    """One type for the whole column from its non-empty cells (never mixed)."""
    seen = [v for v in values if v != ""]
    if not seen:
        return "text"
    if all(_looks_int(v) for v in seen):
        return "int"
    if all(_looks_float(v) for v in seen):     # ints already caught above -> this is decimals
        return "float"
    return "text"


def _coerce_cell(value: str, coltype: str):
    if value == "":
        return None                            # empty cell -> SQL NULL
    if coltype == "int":
        return int(value)
    if coltype == "float":
        return float(value)
    return value


def rebuild_from_csv(db_path: Path | None = None, data_dir: Path | None = None) -> int:
    """(Re)build the SQLite snapshot from every CSV in data_dir. Returns table count.
    One table per CSV (ACCOUNTS.csv -> "ACCOUNTS"), dynamic-typed columns, quoted
    identifiers ("USAGE" is reserved)."""
    db_path = Path(db_path or DB_PATH)
    data_dir = Path(data_dir or _DATA_DIR)
    csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSVs in {data_dir} to rebuild from.")
    if db_path.exists():
        db_path.unlink()                       # start clean, like snapshot.py
    con = sqlite3.connect(db_path)
    try:
        for path in csv_files:
            with open(path, newline="") as f:
                reader = _csv.reader(f)
                header = next(reader)
                rows = list(reader)
            coltypes = [_column_type([r[i] for r in rows]) for i in range(len(header))]
            coldefs = ", ".join(f'"{c}"' for c in header)
            con.execute(f'CREATE TABLE "{path.stem}" ({coldefs})')
            placeholders = ", ".join("?" for _ in header)
            typed = [[_coerce_cell(cell, coltypes[i]) for i, cell in enumerate(r)] for r in rows]
            con.executemany(f'INSERT INTO "{path.stem}" VALUES ({placeholders})', typed)
        con.commit()
    finally:
        con.close()
    return len(csv_files)


_freshness_checked = False


def _ensure_fresh() -> None:
    """Auto-rebuild the DB when a CSV is newer than it (or it's missing), so editing a
    CSV never leaves a stale snapshot behind — demo-safe even if you forget to rebuild.
    Only touches the DEFAULT db location (alongside the CSVs); a custom APP_DB is
    left alone. Runs once per process. If a rebuild fails but a DB already exists, warn
    and keep the old one rather than crashing a live demo."""
    global _freshness_checked
    if _freshness_checked:
        return
    _freshness_checked = True
    if DB_PATH.resolve() != _DEFAULT_DB.resolve():     # injected/custom DB -> don't manage it
        return
    csvs = list(_DATA_DIR.glob("*.csv"))
    if not csvs:
        return
    db_exists = DB_PATH.exists()
    newest_csv = max(p.stat().st_mtime for p in csvs)
    if db_exists and newest_csv <= DB_PATH.stat().st_mtime:
        return                                          # DB already up to date
    try:
        n = rebuild_from_csv()
        if db_exists:
            print(f"[data] a CSV changed — rebuilt snapshot from {n} CSVs.", file=sys.stderr)
    except Exception as e:                              # noqa: BLE001 (demo-safe fallback)
        if db_exists:
            print(f"[data] WARNING: CSV rebuild failed ({e}); using existing snapshot.", file=sys.stderr)
        else:
            raise


def _connect() -> sqlite3.Connection:
    _ensure_fresh()                                     # self-heal a stale/missing snapshot first
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Snapshot not found at {DB_PATH} and no CSVs to rebuild from. "
            f"Restore data/*.csv and run scripts/rebuild_db.py.")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


# --- account resolution -------------------------------------------------------

def accounts_by_owner(owner: str) -> list[dict]:
    """Every account in one AE's book — for the 'list my accounts' / triage view, so the AE
    can ask what's in their book without naming an account first."""
    return _rows(
        "SELECT ACCOUNT_ID, COMPANY_NAME, STATUS, SEGMENT, REGION, INDUSTRY "
        "FROM ACCOUNTS WHERE OWNER_AE = ? ORDER BY COMPANY_NAME", (owner,))


def resolve_account(name: str, owner: str | None = None) -> list[dict]:
    """Find accounts by (partial, case-insensitive) name. Exact match wins.

    Returns a list so the caller can disambiguate ("Meridian" -> two accounts) rather
    than silently guessing — which would be a trust failure.

    If `owner` (an AE email) is given, the search is restricted to that AE's book. The
    assistant is scoped to a single AE, so this is how "only my accounts" is enforced.
    """
    name = name.strip()
    cols = "SELECT ACCOUNT_ID, COMPANY_NAME, STATUS, SEGMENT, OWNER_AE FROM ACCOUNTS WHERE "
    owner_clause, owner_param = (" AND OWNER_AE = ?", (owner,)) if owner else ("", ())
    exact = _rows(cols + "LOWER(COMPANY_NAME) = LOWER(?)" + owner_clause, (name,) + owner_param)
    if exact:
        return exact
    return _rows(cols + "COMPANY_NAME LIKE ?" + owner_clause + " ORDER BY COMPANY_NAME",
                 (f"%{name}%",) + owner_param)


# --- per-account reads (all keyed by ACCOUNT_ID) ------------------------------

def get_account(account_id: str) -> dict | None:
    return _row("SELECT * FROM ACCOUNTS WHERE ACCOUNT_ID = ?", (account_id,))


def get_opportunities(account_id: str) -> list[dict]:
    return _rows(
        "SELECT NAME, STAGE, TYPE, AMOUNT_EUR, CLOSE_DATE, DAYS_IN_STAGE, "
        "WON_LOST_REASON FROM OPPORTUNITIES WHERE ACCOUNT_ID = ? "
        "ORDER BY (WON_LOST_REASON IS NOT NULL), CLOSE_DATE",
        (account_id,),
    )


def get_usage(account_id: str) -> list[dict]:
    """Monthly usage time-series. Empty for prospects/churned (no rows) — the
    caller must treat 'no usage' as 'no data', never as 'usage dropped'."""
    return _rows(
        'SELECT MONTH, MONTHLY_ACTIVE_USERS, LOGINS, WORKFLOW_RUNS, '
        'AUTOMATION_MODULE_ACTIVE, ANALYTICS_MODULE_ACTIVE '
        'FROM "USAGE" WHERE ACCOUNT_ID = ? ORDER BY MONTH',
        (account_id,),
    )


def get_tickets(account_id: str) -> list[dict]:
    return _rows(
        "SELECT PRIORITY, STATUS, SUBJECT, SUMMARY, CREATED_DATE, RESOLVED_DATE "
        "FROM TICKETS WHERE ACCOUNT_ID = ? ORDER BY CREATED_DATE DESC",
        (account_id,),
    )


def get_contacts(account_id: str) -> list[dict]:
    return _rows(
        "SELECT FULL_NAME, ROLE_TITLE, PERSONA_TYPE, LAST_INTERACTION "
        "FROM CONTACTS WHERE ACCOUNT_ID = ? ORDER BY LAST_INTERACTION DESC",
        (account_id,),
    )


def get_activities(account_id: str, limit: int = 15) -> list[dict]:
    return _rows(
        "SELECT ACTIVITY_DATE, ACTIVITY_TYPE, SUBJECT, SUMMARY "
        "FROM ACTIVITIES WHERE ACCOUNT_ID = ? ORDER BY ACTIVITY_DATE DESC LIMIT ?",
        (account_id, limit),
    )


def ticket_subject_rarity() -> dict[str, int]:
    """How many distinct accounts each ticket SUBJECT appears on. Systemic subjects
    (SSO across 7 accounts) are near-noise; a subject on a single account is a real,
    account-specific signal. Used to weight the ticket signal (see signals.py)."""
    rows = _rows(
        "SELECT SUBJECT, COUNT(DISTINCT ACCOUNT_ID) AS n FROM TICKETS GROUP BY SUBJECT"
    )
    return {r["SUBJECT"]: r["n"] for r in rows}

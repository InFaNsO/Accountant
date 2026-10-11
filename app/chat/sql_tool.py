"""Read-only SQL, for the questions the fixed tools can't express.

Ninety tools cover the common shapes, but "invoices whose amount matches this
Tally line within a rupee" or "clients who paid inside seven days last quarter"
aren't among them. This gives the model a way to ask anything of the data while
staying structurally unable to change it:

  * its own connection, opened read-only and pinned with PRAGMA query_only
  * one statement, and it must start with SELECT or WITH
  * a wall-clock interrupt, so a cartesian join can't pin a worker
  * a row cap, so a wide result can't blow out the context window

The permission gate (policy.py) restricts it to users with view on every
module, because a query sees every table.
"""

import re
import sqlite3
import time

from flask import current_app

from .tools import ToolError, local_tool

MAX_ROWS = 500
TIMEOUT_SECONDS = 5.0
MAX_CELL = 200

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|begin|commit|rollback)\b",
    re.IGNORECASE,
)


def _connect():
    path = current_app.config["DATABASE"].replace("\\", "/")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def _guard(sql):
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ToolError("Empty query.")
    if ";" in stripped:
        raise ToolError("One statement per call — remove the ';'.")
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        raise ToolError("Only SELECT (or WITH … SELECT) queries are allowed.")
    # Comments could hide a second verb from the prefix check above.
    if "--" in stripped or "/*" in stripped:
        raise ToolError("Remove SQL comments from the query.")
    if _FORBIDDEN.search(stripped):
        raise ToolError("This tool is read-only: no writes, PRAGMA or ATTACH.")
    return stripped


@local_tool(
    "describe_schema",
    "Show the CREATE statements for the database tables so you can write a "
    "correct query. Call with no arguments for a list of tables plus their "
    "columns, or with a table name for that table's full DDL and indexes. "
    "Always check the schema before writing SQL — do not guess column names.",
    {
        "type": "object",
        "properties": {
            "table": {"type": "string",
                      "description": "Table name. Omit for all tables."},
        },
    },
)
def describe_schema(user=None, table=None):
    conn = _connect()
    try:
        if table:
            rows = conn.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE tbl_name = ? AND sql IS NOT NULL ORDER BY type DESC, name",
                (table,),
            ).fetchall()
            if not rows:
                raise ToolError(f"No table named '{table}'.")
            return "\n\n".join(r["sql"] for r in rows)

        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        lines = []
        for r in rows:
            cols = conn.execute(f"PRAGMA table_info({r['name']})").fetchall()
            lines.append(f"{r['name']}({', '.join(c['name'] for c in cols)})")
        return "\n".join(lines)
    finally:
        conn.close()


@local_tool(
    "query_sql",
    "Run one read-only SQL SELECT against the ledger database and get the rows "
    "back as a table. Use this only when no other tool answers the question — "
    "the dedicated tools format currency and running balances correctly and "
    "are cheaper. Call describe_schema first. SQLite syntax. Returns at most "
    f"{MAX_ROWS} rows; add LIMIT and aggregate in SQL rather than pulling "
    "everything back.",
    {
        "type": "object",
        "properties": {
            "sql": {"type": "string",
                    "description": "A single SELECT or WITH…SELECT statement."},
            "purpose": {"type": "string",
                        "description": "One line on what you are looking for; "
                                       "shown to the user."},
        },
        "required": ["sql"],
    },
)
def query_sql(user=None, sql="", purpose=None):
    statement = _guard(sql)
    conn = _connect()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    # Fires every few thousand VM steps; returning non-zero aborts the query.
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 4000)
    try:
        cur = conn.execute(statement)
        rows = cur.fetchmany(MAX_ROWS + 1)
        columns = [d[0] for d in (cur.description or [])]
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise ToolError(
                f"Query took longer than {TIMEOUT_SECONDS:g}s and was stopped. "
                f"Add a WHERE clause, aggregate, or narrow the date range."
            )
        raise ToolError(f"SQL error: {e}")
    except sqlite3.Error as e:
        raise ToolError(f"SQL error: {e}")
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    if not rows:
        return "0 rows."

    header = " | ".join(columns)
    body = "\n".join(
        " | ".join(_cell(r[c]) for c in columns) for r in rows
    )
    note = (f"\n\n{len(rows)} rows shown; more were available — add LIMIT or "
            f"aggregate." if truncated else f"\n\n{len(rows)} row(s).")
    return f"{header}\n{'-' * len(header)}\n{body}{note}"


def _cell(value):
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"

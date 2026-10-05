"""Assistant API balances must equal the Ledger screen's balance for every client.

Regression test: /api/clients/<id> and /api/clients/summary once used their own
formula that double-counted payments (all payments have invoice_id NULL), so the
assistant disagreed with the ledger for most clients.

Runs on a snapshot copy of data/ledger.db; the real file is never written.
Run with the repo venv:  .venv/Scripts/python.exe test_api_balance.py
"""
import os
import re
import sqlite3
import sys
import tempfile

os.environ.setdefault("MCP_API_KEY", "test-key")

import app as app_pkg                                   # noqa: E402
import app.services.scheduler as scheduler              # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ledger.db")
tmp = os.path.join(tempfile.mkdtemp(), "copy.db")
s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
d = sqlite3.connect(tmp)
s.backup(d)
s.close()
d.close()

_orig_init = app_pkg.init_db


def _init(flask_app):
    flask_app.config["DATABASE"] = tmp
    return _orig_init(flask_app)


app_pkg.init_db = _init
scheduler.start_scheduler = lambda *a, **k: None

from app.services.client_service import get_client_balance   # noqa: E402

flask_app = app_pkg.create_app()
client = flask_app.test_client()
H = {"X-MCP-Key": "test-key"}


def inr(text):
    return float(re.search(r"₹([\d,]+(?:\.\d+)?)", text).group(1).replace(",", ""))


def signed(text, owes, credit):
    if "settled" in text:
        return 0.0
    v = inr(text)
    return -v if owes in text else (v if credit in text else 0.0)


fails = 0
with flask_app.app_context():
    from app.database import get_db
    ids = [r["id"] for r in get_db().execute("SELECT id FROM clients")]
    summary = client.get("/api/clients/summary", headers=H).get_json()["result"]
    for cid in ids:
        want = get_client_balance(cid)
        det = client.get(f"/api/clients/{cid}", headers=H).get_json()["result"]
        line = next(l for l in det.splitlines() if l.startswith("Balance:"))
        got = signed(line, "(owes us)", "(credit)")
        srow = next(l for l in summary.splitlines() if l.startswith(f"ID {cid}:"))
        got_s = signed(srow, "owes ₹", "credit ₹")
        # outputs are rounded to whole rupees by _inr
        for label, g in (("details", got), ("summary", got_s)):
            if abs(g - want) > 1.0:
                fails += 1
                print(f"FAIL client {cid} {label}: api {g:,.2f} vs ledger {want:,.2f}")

print(f"{len(ids)} clients checked, {fails} mismatches")
sys.exit(1 if fails else 0)

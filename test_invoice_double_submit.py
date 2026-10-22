"""One invoice form can only make one invoice; likely duplicates get a prompt.

Regression test for INV-0543 / INV-0545 (16 Oct 26): an invoice was saved as
issued, the browser's Back button brought the filled form back, and saving it
again as a draft created a second copy that was later issued.

Runs on a snapshot copy of data/ledger.db; the real file is never written.
Run with the repo venv:  .venv/Scripts/python.exe test_invoice_double_submit.py
"""
import os
import re
import sqlite3
import sys
import tempfile
import threading

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

from app.services import invoice_service, client_service   # noqa: E402

flask_app = app_pkg.create_app()
create_app_again = app_pkg.create_app     # migrations must be safe to re-run

fails = 0


def check(cond, label):
    global fails
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails += 1


def db():
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    GOD = c.execute("SELECT id FROM users WHERE role='god' ORDER BY id LIMIT 1").fetchone()["id"]
    SUB = c.execute("SELECT id, product_id, name FROM sub_products WHERE is_active=1 "
                    "ORDER BY id LIMIT 1").fetchone()
with flask_app.app_context():
    CLIENT = next(r["id"] for r in client_service.get_all_clients()
                  if not client_service.get_client_lock_status(r["id"])["locked"])


def logged_in_client():
    cl = flask_app.test_client()
    with cl.session_transaction() as sess:
        sess["_user_id"] = str(GOD)
        sess["_fresh"] = True
    return cl


def new_token(cl):
    html = cl.get("/invoices/new").get_data(as_text=True)
    m = re.search(r'name="form_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def form(token, status="issued", qty=3, price=111.0):
    f = {
        "client_id": str(CLIENT), "issue_date": "2026-10-16", "due_date": "2026-11-15",
        "status": status, "notes": "double-submit test", "discount_amount": "0",
        "discount_type": "value",
        "items[0][product_id]": str(SUB["product_id"]),
        "items[0][sub_product_id]": str(SUB["id"]),
        "items[0][description]": "Test line " + SUB["name"],
        "items[0][quantity]": str(qty), "items[0][unit_price]": str(price),
        "items[0][tax_rate]": "18", "items[0][discount_type]": "percent",
        "items[0][discount_value]": "0",
    }
    if token is not None:
        f["form_token"] = token
    return f


def rows_with_token(token):
    with db() as c:
        return c.execute("SELECT id, invoice_number, status FROM invoices WHERE form_token=?",
                         (token,)).fetchall()


def stock():
    with db() as c:
        return c.execute("SELECT stock_qty FROM sub_products WHERE id=?", (SUB["id"],)).fetchone()[0]


def invoice_count():
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]


def target(resp):
    m = re.search(r"/invoices/(\d+)$", resp.headers.get("Location", ""))
    return int(m.group(1)) if m else None


cl = logged_in_client()

# ── Schema ────────────────────────────────────────────────────────────────────
with db() as c:
    cols = {r[1] for r in c.execute("PRAGMA table_info(invoices)")}
    idx = {r[1]: r[2] for r in c.execute("PRAGMA index_list(invoices)")}
check("form_token" in cols, "invoices.form_token column exists")
check(idx.get("idx_invoices_form_token") == 1, "unique index on form_token exists")
try:
    create_app_again()
    check(True, "migrations re-run cleanly on an already-migrated DB")
except Exception as e:                                   # noqa: BLE001
    check(False, f"migrations re-run cleanly ({e!r})")

# ── The incident: save as issued, Back, save again as draft ───────────────────
tok = new_token(cl)
check(bool(tok) and len(tok) >= 16, "new-invoice form carries a one-time token")
check(new_token(cl) != tok, "each new form gets a different token")

before_count, before_stock = invoice_count(), stock()
r1 = cl.post("/invoices/new", data=form(tok, "issued"))
first_id = target(r1)
check(r1.status_code == 302 and first_id, "first save creates the invoice")
check(invoice_count() == before_count + 1, "first save adds exactly one invoice")
check(abs(stock() - (before_stock - 3)) < 1e-9, "first save deducts stock once")
first_no = rows_with_token(tok)[0]["invoice_number"]
check(first_no.startswith("INV-"), f"first save is issued ({first_no})")

r2 = cl.post("/invoices/new", data=form(tok, "draft"))
check(target(r2) == first_id, "re-saving the same form as a draft opens the first invoice")
r3 = cl.post("/invoices/new", data=form(tok, "issued"))
check(target(r3) == first_id, "re-saving the same form as issued opens the first invoice")
check(invoice_count() == before_count + 1, "re-saves create no new invoice")
check(abs(stock() - (before_stock - 3)) < 1e-9, "re-saves deduct no more stock")
page = cl.get(r3.headers["Location"]).get_data(as_text=True)
check(f"already saved as {first_no}" in page, "the page says it was already saved")

# A draft saved first, then re-sent as issued, also stays one (draft) invoice
tok_d = new_token(cl)
rd1 = cl.post("/invoices/new", data=form(tok_d, "draft", qty=1, price=7.0))
rd2 = cl.post("/invoices/new", data=form(tok_d, "issued", qty=1, price=7.0))
rows = rows_with_token(tok_d)
check(len(rows) == 1 and rows[0]["status"] == "draft" and target(rd2) == target(rd1),
      "draft then re-sent as issued stays one draft")

# ── Validation re-render keeps the token ──────────────────────────────────────
tok_v = new_token(cl)
bad = form(tok_v)
for k in [k for k in bad if k.startswith("items[")]:
    del bad[k]
html = cl.post("/invoices/new", data=bad).get_data(as_text=True)
check(f'name="form_token" value="{tok_v}"' in html, "a form sent back for fixes keeps its token")
rv = cl.post("/invoices/new", data=form(tok_v, qty=1, price=5.0))
check(len(rows_with_token(tok_v)) == 1 and target(rv), "the corrected form then saves once")

# ── No token (API-style callers) behaves as before ────────────────────────────
n0 = invoice_count()
cl.post("/invoices/new", data=form(None, qty=1, price=3.0))
cl.post("/invoices/new", data=form(None, qty=1, price=3.0))
check(invoice_count() == n0 + 2, "posts without a token still create invoices")

# ── Two saves of the same form at the same moment ─────────────────────────────
tok_r = new_token(cl)
n0, s0 = invoice_count(), stock()
barrier = threading.Barrier(2)
results = []


def racer():
    c2 = logged_in_client()
    barrier.wait()
    results.append(target(c2.post("/invoices/new", data=form(tok_r, qty=2, price=13.0))))


threads = [threading.Thread(target=racer) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(len(rows_with_token(tok_r)) == 1 and invoice_count() == n0 + 1,
      "two simultaneous saves make one invoice")
check(len(set(results)) == 1 and results[0], "both simultaneous saves open the same invoice")
check(abs(stock() - (s0 - 2)) < 1e-9, "two simultaneous saves deduct stock once")

# The insert-time guard on its own: another request commits between the check
# and the insert, so only the unique index stops the copy.
tok_i = "test-race-" + tok_r
race_items = [{"product_id": SUB["product_id"], "sub_product_id": SUB["id"],
               "description": "race", "quantity": 1, "unit_price": 17.0, "tax_rate": 18}]
with flask_app.app_context():
    first = invoice_service.create_invoice(form(tok_i, qty=1, price=17.0), race_items)
    real_lookup = invoice_service._invoice_id_for_token
    calls = {"n": 0}

    def stale_first_lookup(dbc, token):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_lookup(dbc, token)

    invoice_service._invoice_id_for_token = stale_first_lookup
    n0, s0 = invoice_count(), stock()
    try:
        invoice_service.create_invoice(form(tok_i, qty=1, price=17.0), race_items)
        check(False, "unique index refuses a copy that slipped past the check")
    except invoice_service.AlreadySaved as e:
        check(e.invoice_id == first, "unique index refuses a copy that slipped past the check")
    finally:
        invoice_service._invoice_id_for_token = real_lookup
    check(invoice_count() == n0 and abs(stock() - s0) < 1e-9,
          "the refused copy leaves no invoice and no stock change behind")

# ── Duplicate prompt: new form ────────────────────────────────────────────────
same = form(new_token(cl), "issued")                     # same client + total as first_id
m = cl.post("/invoices/api/possible-duplicate", data=same).get_json()["match"]
check(bool(m) and m["id"] == first_id, "same client and total is flagged before saving")
check(bool(m) and m["invoice_number"] == first_no and m["client_name"],
      "the prompt names the matching invoice and client")
m = cl.post("/invoices/api/possible-duplicate",
            data=form(new_token(cl), "issued", qty=3, price=999.0)).get_json()["match"]
check(m is None or m["id"] != first_id, "a different total is not matched to it")
m = cl.post(f"/invoices/api/possible-duplicate?exclude={first_id}", data=same).get_json()["match"]
check(m is None or m["id"] != first_id, "?exclude skips the invoice being edited")
check(cl.post("/invoices/api/possible-duplicate", data={"client_id": ""}).get_json()["match"] is None,
      "an incomplete form gets no match (and no error)")

# ── Duplicate prompt: issuing a draft ─────────────────────────────────────────
tok_c = new_token(cl)
draft_id = target(cl.post("/invoices/new", data=form(tok_c, "draft")))   # copy of first_id
page = cl.get(f"/invoices/{draft_id}").get_data(as_text=True)
check("This draft may be a copy of" in page and first_no in page, "a copy draft shows the warning banner")
check("Issue this draft anyway?" in page, "issuing the copy draft asks for confirmation")
plain_draft = target(cl.post("/invoices/new", data=form(new_token(cl), "draft", qty=1, price=4321.0)))
check("may be a copy of" not in cl.get(f"/invoices/{plain_draft}").get_data(as_text=True),
      "an ordinary draft shows no warning")
issued_page = cl.get(f"/invoices/{first_id}").get_data(as_text=True)
check("may be a copy of" not in issued_page, "issued invoices show no warning")

# Cancelling the original clears the warning, and the warning never blocks issuing
cl.post(f"/invoices/{first_id}/status", data={"status": "cancelled"})
check("may be a copy of" not in cl.get(f"/invoices/{draft_id}").get_data(as_text=True),
      "a cancelled invoice is not a duplicate")
cl.post(f"/invoices/{draft_id}/status", data={"status": "issued"})
with db() as c:
    st = c.execute("SELECT status FROM invoices WHERE id=?", (draft_id,)).fetchone()["status"]
check(st == "issued", "the draft can still be issued (warning only)")

print(f"{fails} failure(s)")
sys.exit(1 if fails else 0)

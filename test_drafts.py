"""Drafts for invoices, production orders and transit dispatches.

Invoices: a draft holds no stock and keeps its D-### number. Payments can be
recorded against it, but only issuing takes it out of draft: that gives it the
next INV serial, takes the stock once, and then the payments decide issued /
partial / paid. Nothing goes back to draft once issued. Regression for D-001,
D-004, D-005, D-006 (drafts flipped to paid/partial by a payment, never issued, no
stock taken) and INV-0401 / INV-0557 (issued, back to draft, issued again: stock
taken twice).

Production orders and dispatches: a draft's lines can be added, changed and
removed without touching production / transit stock; activating applies them.
Rows deleted on a form leave index gaps, and the rows after a gap still save.

Runs on a snapshot copy of data/ledger.db; the real file is never written.
Run with the repo venv:  .venv/Scripts/python.exe test_drafts.py
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

from app.services import payment_service, client_service, transit_service   # noqa: E402

flask_app = app_pkg.create_app()
API = {"X-MCP-Key": "test-key"}
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


def one(sql, *args):
    with db() as c:
        return c.execute(sql, args).fetchone()


def qty(spid, field="stock_qty"):
    return one(f"SELECT {field} FROM sub_products WHERE id=?", spid)[0]


def inv(iid):
    return one("SELECT * FROM invoices WHERE id=?", iid)


def moves(iid):
    with db() as c:
        return c.execute("SELECT sub_product_id, movement_type, quantity FROM stock_movements "
                         "WHERE invoice_id=? ORDER BY id", (iid,)).fetchall()


def max_inv():
    with db() as c:
        nums = [r[0] for r in c.execute("SELECT invoice_number FROM invoices WHERE invoice_number LIKE 'INV-%'")]
    return max(int(n.split("-")[-1]) for n in nums if n.split("-")[-1].isdigit())


def flashes(cl):
    with cl.session_transaction() as sess:
        return " | ".join(m for _cat, m in sess.get("_flashes", []))


# ── Fixtures: own client, company, product with three sub-products, supplier ──
with db() as c:
    GOD = c.execute("SELECT id FROM users WHERE role='god' ORDER BY id LIMIT 1").fetchone()["id"]
    CLIENT  = c.execute("INSERT INTO clients (name) VALUES ('Draft Test Client')").lastrowid
    CLIENT2 = c.execute("INSERT INTO clients (name) VALUES ('Draft Credit Client')").lastrowid
    CLIENT3 = c.execute("INSERT INTO clients (name) VALUES ('Draft OB Client')").lastrowid
    COMPANY = c.execute("INSERT INTO client_companies (client_id, name, opening_balance) "
                        "VALUES (?, 'Draft Credit Co', -200)", (CLIENT2,)).lastrowid
    c.execute("UPDATE clients SET opening_balance=-200 WHERE id=?", (CLIENT2,))
    PROD = c.execute("INSERT INTO products (name, unit_price) VALUES ('Draft Test Disc', 10)").lastrowid
    A, B, C = (c.execute("INSERT INTO sub_products (product_id, name, stock_qty) VALUES (?, ?, 1000)",
                         (PROD, n)).lastrowid for n in ("A", "B", "C"))
    SUPPLIER = c.execute("INSERT INTO suppliers (name) VALUES ('Draft Test Supplier')").lastrowid

with flask_app.app_context():
    for cid in (CLIENT, CLIENT2, CLIENT3):
        check(not client_service.get_client_lock_status(cid)["locked"], f"fixture client {cid} is unlocked")

cl = flask_app.test_client()
with cl.session_transaction() as sess:
    sess["_user_id"] = str(GOD)
    sess["_fresh"] = True


def new_token():
    html = cl.get("/invoices/new").get_data(as_text=True)
    return re.search(r'name="form_token" value="([^"]+)"', html).group(1)


def inv_form(status, lines, client=CLIENT, company=None, token=None):
    """lines: {row index: (sub_product_id, qty)} at Rs 10, no tax."""
    f = {"client_id": str(client), "issue_date": "2026-10-30", "due_date": "",
         "status": status, "notes": "draft test", "discount_amount": "0", "discount_type": "value",
         "company_id": str(company or "")}
    if token:
        f["form_token"] = token
    for i, (spid, q) in lines.items():
        f.update({
            f"items[{i}][product_id]": str(PROD), f"items[{i}][sub_product_id]": str(spid),
            f"items[{i}][description]": f"Disc {spid}", f"items[{i}][quantity]": str(q),
            f"items[{i}][unit_price]": "10", f"items[{i}][tax_rate]": "0",
            f"items[{i}][discount_type]": "percent", f"items[{i}][discount_value]": "0",
        })
    return f


def new_invoice(status, lines, **kw):
    r = cl.post("/invoices/new", data=inv_form(status, lines, token=new_token(), **kw))
    m = re.search(r"/invoices/(\d+)$", r.headers.get("Location", ""))
    return int(m.group(1)) if m else None


def lines_of(iid):
    with db() as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT sub_product_id, quantity FROM invoice_items WHERE invoice_id=?", (iid,))}


# ── Invoice: draft holds nothing ──────────────────────────────────────────────
a0, b0, c0 = qty(A), qty(B), qty(C)
D = new_invoice("draft", {0: (A, 100), 1: (B, 50)})
check(D is not None, "draft invoice saved")
check(inv(D)["invoice_number"].startswith("D-") and inv(D)["status"] == "draft", "draft gets a D-### number")
check(moves(D) == [] and (qty(A), qty(B)) == (a0, b0), "draft takes no stock")

# ── Payments land on the draft but never move it out of draft ────────────────
with flask_app.app_context():
    payment_service.create_payment({"client_id": CLIENT, "amount": 500, "payment_date": "2026-10-30",
                                    "method": "cash", "invoice_id": D})
check(inv(D)["status"] == "draft" and inv(D)["amount_paid"] == 500,
      "payment recorded against the draft: draft keeps status, 500 counted")
with flask_app.app_context():
    payment_service.create_payment({"client_id": CLIENT, "amount": 300, "payment_date": "2026-10-30",
                                    "method": "cash"})
check(inv(D)["status"] == "draft" and inv(D)["amount_paid"] == 800,
      "auto-allocated payment reaching the draft: still a draft (was the D-006 bug)")
check(inv(D)["invoice_number"].startswith("D-"), "payments do not touch the draft number")
with flask_app.app_context():
    payment_service.recalculate_client_balance(CLIENT)
check(inv(D)["status"] == "draft" and inv(D)["amount_paid"] == 800, "reconcile keeps the draft a draft")
with flask_app.app_context():
    extra = payment_service.create_payment({"client_id": CLIENT, "amount": 200, "payment_date": "2026-10-30",
                                            "method": "cash", "invoice_id": D})
    payment_service.update_payment(extra, {"amount": 250, "payment_date": "2026-10-30", "method": "cash"})
check(inv(D)["status"] == "draft" and inv(D)["amount_paid"] == 1050, "editing a payment on a draft keeps it a draft")
with flask_app.app_context():
    payment_service.delete_payment(extra)
check(inv(D)["status"] == "draft" and inv(D)["amount_paid"] == 800, "deleting a payment on a draft keeps it a draft")

html = cl.get(f"/invoices/{D}").get_data(as_text=True)
check("Payments Received (draft)" in html and "still to collect" in html,
      "draft page shows payments received and what is still to collect")

# ── Edit the draft's lines: change, remove, add (with an index gap) ──────────
html = cl.get(f"/invoices/{D}/edit").get_data(as_text=True)
check("Issue now" in html and 'value="partial"' not in html, "draft edit form offers Draft / Issue now")
r = cl.post(f"/invoices/{D}/edit", data=inv_form("draft", {0: (A, 120), 5: (C, 30)}))
check(lines_of(D) == {A: 120, C: 30}, "draft lines edited: A changed, B removed, C added after a gap")
check(inv(D)["status"] == "draft" and moves(D) == [] and (qty(A), qty(B), qty(C)) == (a0, b0, c0),
      "editing a draft still takes no stock")
check(inv(D)["total"] == 1500, "draft total follows its lines")

# ── Issue: next INV serial, stock taken once, payments counted ───────────────
expected = f"INV-{max_inv() + 1:04d}"
cl.post(f"/invoices/{D}/status", data={"status": "issued"})
row = inv(D)
check(row["invoice_number"] == expected, f"issued draft gets the next serial {expected} (got {row['invoice_number']})")
check(row["issued_at"] is not None, "issued_at set")
check(row["status"] == "partial" and row["amount_paid"] == 800, "payments taken as a draft now count: partial")
check((qty(A), qty(B), qty(C)) == (a0 - 120, b0, c0 - 30), "stock taken for each line once")
check(len([m for m in moves(D) if m["movement_type"] == "sale"]) == 2, "two sale movements logged")
check(f"Issued as {expected}" in flashes(cl), "issue flash names the serial")

# ── Nothing goes back to draft ────────────────────────────────────────────────
cl.post(f"/invoices/{D}/status", data={"status": "draft"})
check(inv(D)["status"] == "partial" and inv(D)["invoice_number"] == expected,
      "issued -> draft refused on the status route (INV-0401 / INV-0557 path)")
r = cl.put(f"/api/invoices/{D}/status", json={"status": "draft"}, headers=API)
check(r.status_code == 400 and inv(D)["status"] == "partial", "issued -> draft refused on the API")
html = cl.get(f"/invoices/{D}/edit").get_data(as_text=True)
check('value="draft"' not in html and '<option value="partial" selected>' in html,
      "live edit form has no Draft option and keeps Partial selected")
cl.post(f"/invoices/{D}/edit", data=inv_form("draft", {0: (A, 120), 1: (C, 30)}))
check(inv(D)["status"] == "partial" and (qty(A), qty(C)) == (a0 - 120, c0 - 30),
      "edit form posting status=draft cannot un-issue or re-take stock")

# ── Live edit: stock follows the lines ────────────────────────────────────────
cl.post(f"/invoices/{D}/edit", data=inv_form("partial", {0: (A, 150)}))
check(lines_of(D) == {A: 150}, "live invoice lines edited")
check((qty(A), qty(C)) == (a0 - 150, c0), "more of A takes 30 more; removed C line puts 30 back")
check(inv(D)["status"] == "partial", "still partial on the same total")
before = (qty(A), lines_of(D))
r = cl.post(f"/invoices/{D}/edit", data=inv_form("partial", {0: (A, 150 + 5000)}))
check(r.status_code == 200 and "Not saved" in r.get_data(as_text=True), "raise beyond stock: form shown again")
check((qty(A), lines_of(D)) == before, "raise beyond stock: nothing saved, no stock moved")

# ── Cancel returns stock; reopening takes it again with the same serial ──────
cl.post(f"/invoices/{D}/status", data={"status": "cancelled"})
check(qty(A) == a0 and inv(D)["status"] == "cancelled", "cancel puts the stock back")
cl.post(f"/invoices/{D}/status", data={"status": "issued"})
check(qty(A) == a0 - 150 and inv(D)["invoice_number"] == expected and inv(D)["status"] == "partial",
      "reopening a cancelled invoice takes the stock again and keeps its serial")

# ── API: draft -> paid issues it properly ─────────────────────────────────────
D2 = new_invoice("draft", {0: (B, 10)})
expected2 = f"INV-{max_inv() + 1:04d}"
r = cl.put(f"/api/invoices/{D2}/status", json={"status": "paid"}, headers=API)
check(r.status_code == 200 and inv(D2)["invoice_number"] == expected2,
      f"API draft -> paid issues with the next serial {expected2}")
check(inv(D2)["status"] == "issued" and qty(B) == b0 - 10,
      "API issue takes stock; status comes from payments (none yet: issued)")

# ── Client credit waiting before the draft existed is applied on issue ───────
with flask_app.app_context():
    payment_service.create_payment({"client_id": CLIENT2, "amount": 1000, "payment_date": "2026-10-29",
                                    "method": "bank_transfer"})
D3 = new_invoice("draft", {0: (B, 60)}, client=CLIENT2)
check(inv(D3)["amount_paid"] == 0, "advance stays as credit while the invoice is a draft")
check("more from the client" in cl.get(f"/invoices/{D3}").get_data(as_text=True),
      "draft page shows the credit that issuing will apply")
cl.post(f"/invoices/{D3}/status", data={"status": "issued"})
check(inv(D3)["status"] == "paid" and inv(D3)["amount_paid"] == 600, "on issue the advance pays it: paid")

# ── Company credit opening balance survives the request (was never committed) ─
D4 = new_invoice("draft", {0: (B, 30)}, client=CLIENT2, company=COMPANY)
cl.post(f"/invoices/{D4}/status", data={"status": "issued"})
bal = one("SELECT amount FROM payments WHERE client_id=? AND method='balance'", CLIENT2)
check(bal is not None and abs(bal["amount"] - 200) < 0.01, "company credit OB applied and committed on web issue")
check(one("SELECT opening_balance FROM client_companies WHERE id=?", COMPANY)[0] == 0, "company OB used up")
check(inv(D4)["status"] == "paid", "company credit + remaining advance pay it in full")

# ── Opening-balance payments are never used as credit ─────────────────────────
with db() as c:
    c.execute("UPDATE clients SET opening_balance=500 WHERE id=?", (CLIENT3,))
with flask_app.app_context():
    payment_service.create_payment({"client_id": CLIENT3, "amount": 500, "payment_date": "2026-10-29",
                                    "method": "cash", "is_opening_balance": 1})
D5 = new_invoice("draft", {0: (B, 5)}, client=CLIENT3)
cl.post(f"/invoices/{D5}/status", data={"status": "issued"})
check(inv(D5)["status"] == "issued" and inv(D5)["amount_paid"] == 0,
      "opening-balance payment is not put on the new invoice")

# ── Production order drafts ───────────────────────────────────────────────────
pa0, pb0, pc0 = qty(A, "production_qty"), qty(B, "production_qty"), qty(C, "production_qty")
r = cl.post("/production/new", data={
    "name": "Draft PO test", "supplier_id": str(SUPPLIER), "status": "draft",
    "product_id_0": str(PROD), "sub_product_id_0": str(A), "quantity_0": "10",
    "product_id_2": str(PROD), "sub_product_id_2": str(B), "quantity_2": "20",
})
PO = int(re.search(r"/production/(\d+)$", r.headers["Location"]).group(1))


def po_lines():
    with db() as c:
        return {r["sub_product_id"]: (r["id"], r["quantity"]) for r in c.execute(
            "SELECT id, sub_product_id, quantity FROM purchase_order_items WHERE po_id=?", (PO,))}


check(set(po_lines()) == {A, B}, "new draft PO keeps the row after a deleted one (index gap)")
check(qty(A, "production_qty") == pa0, "draft PO adds no production")
la, lb = po_lines()[A][0], po_lines()[B][0]
cl.post(f"/production/{PO}/edit", data={
    "name": "Draft PO test", "supplier_id": str(SUPPLIER),
    f"existing_qty_{la}": "10", f"remove_{la}": "1",
    f"existing_qty_{lb}": "25",
    "product_id_0": str(PROD), "sub_product_id_0": str(C), "quantity_0": "5",
})
check({k: v[1] for k, v in po_lines().items()} == {B: 25, C: 5}, "draft PO lines: removed A, B 20->25, added C")
check((qty(A, "production_qty"), qty(B, "production_qty"), qty(C, "production_qty")) == (pa0, pb0, pc0),
      "editing a draft PO touches no production")
cl.post(f"/production/{PO}/activate")
check((qty(B, "production_qty"), qty(C, "production_qty")) == (pb0 + 25, pc0 + 5),
      "activating adds the edited lines to production")
lb, lc = po_lines()[B][0], po_lines()[C][0]
cl.post(f"/production/{PO}/edit", data={"name": "Draft PO test", "supplier_id": str(SUPPLIER),
                                        f"existing_qty_{lb}": "30", f"existing_qty_{lc}": "5",
                                        f"remove_{lc}": "1"})
check(set(po_lines()) == {B} and qty(C, "production_qty") == pc0,
      "live PO: undispatched line removed and its production taken back")
check(qty(B, "production_qty") == pb0 + 30, "live PO: quantity edit still moves production by the difference")
with db() as c:
    c.execute("UPDATE purchase_order_items SET qty_dispatched=5 WHERE id=?", (lb,))
cl.post(f"/production/{PO}/edit", data={"name": "Draft PO test", "supplier_id": str(SUPPLIER),
                                        f"existing_qty_{lb}": "30", f"remove_{lb}": "1",
                                        "product_id_0": str(PROD), "sub_product_id_0": str(A),
                                        "quantity_0": "1"})
check(B in po_lines() and qty(B, "production_qty") == pb0 + 30,
      "a line already dispatched from cannot be removed")
r = cl.post("/production/new", data={"name": "One-line draft", "status": "draft",
                                     "product_id_0": str(PROD), "sub_product_id_0": str(A), "quantity_0": "3"})
PO2 = int(re.search(r"/production/(\d+)$", r.headers["Location"]).group(1))
only = one("SELECT id FROM purchase_order_items WHERE po_id=?", PO2)["id"]
cl.post(f"/production/{PO2}/edit", data={"name": "One-line draft", f"existing_qty_{only}": "3",
                                         f"remove_{only}": "1"})
check(one("SELECT COUNT(*) FROM purchase_order_items WHERE po_id=?", PO2)[0] == 1,
      "removing an order's last line is refused")

# ── Transit drafts ────────────────────────────────────────────────────────────
ta0, tb0, tc0 = qty(A, "in_transit_qty"), qty(B, "in_transit_qty"), qty(C, "in_transit_qty")
r = cl.post("/transit/new", data={
    "name": "Draft dispatch test", "status": "draft", "skip_production": "1",
    "product_id_0": str(PROD), "sub_product_id_0": str(A), "quantity_0": "4",
    "product_id_3": str(PROD), "sub_product_id_3": str(B), "quantity_3": "6",
})
DS = int(re.search(r"/transit/(\d+)$", r.headers["Location"]).group(1))


def ds_lines():
    with db() as c:
        return {r["sub_product_id"]: (r["id"], r["quantity"]) for r in c.execute(
            "SELECT id, sub_product_id, quantity FROM dispatch_items WHERE dispatch_id=?", (DS,))}


check(set(ds_lines()) == {A, B}, "new draft dispatch keeps the row after a deleted one (index gap)")
la, lb = ds_lines()[A][0], ds_lines()[B][0]
html = cl.get(f"/transit/{DS}/edit").get_data(as_text=True)
check(f'name="remove_{la}"' in html and 'onclick="addNewRow()"' in html, "draft dispatch edit offers remove and add")
cl.post(f"/transit/{DS}/edit", data={
    "name": "Draft dispatch test", "skip_production": "1",
    f"qty_{la}": "4", f"pid_{la}": str(PROD), f"sid_{la}": str(A), f"remove_{la}": "1",
    f"qty_{lb}": "8", f"pid_{lb}": str(PROD), f"sid_{lb}": str(B),
    "new_product_id_0": str(PROD), "new_sub_product_id_0": str(C), "new_quantity_0": "7",
})
check({k: v[1] for k, v in ds_lines().items()} == {B: 8, C: 7}, "draft dispatch lines: removed A, B 6->8, added C")
check((qty(A, "in_transit_qty"), qty(B, "in_transit_qty"), qty(C, "in_transit_qty")) == (ta0, tb0, tc0),
      "editing a draft dispatch moves no stock")
cl.post(f"/transit/{DS}/activate")
check((qty(B, "in_transit_qty"), qty(C, "in_transit_qty")) == (tb0 + 8, tc0 + 7),
      "activating moves the edited lines into transit")
lb, lc = ds_lines()[B][0], ds_lines()[C][0]
cl.post(f"/transit/{DS}/edit", data={
    "name": "Draft dispatch test",
    f"qty_{lb}": "10", f"pid_{lb}": str(PROD), f"sid_{lb}": str(B),
    f"qty_{lc}": "7", f"pid_{lc}": str(PROD), f"sid_{lc}": str(C),
    "new_product_id_0": str(PROD), "new_sub_product_id_0": str(A), "new_quantity_0": "3",
    f"remove_{lc}": "1",
})
check({k: v[1] for k, v in ds_lines().items()} == {B: 10, C: 7} and qty(B, "in_transit_qty") == tb0 + 10,
      "live dispatch: quantity edit still moves transit; posted add/remove fields are ignored")
with flask_app.app_context():
    ok, errs, _w = transit_service.update_dispatch(
        DS, {"name": "Draft dispatch test"}, {},
        new_items=[{"product_id": PROD, "sub_product_id": A, "quantity": 1}])
check(not ok and "draft" in errs[0], "live dispatch refuses added lines")
html = cl.get(f"/transit/{DS}/edit").get_data(as_text=True)
check('onclick="addNewRow()"' not in html and 'name="remove_' not in html, "live dispatch edit offers no add/remove")

print()
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)

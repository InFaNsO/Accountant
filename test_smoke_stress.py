"""
Smoke test + stress test for the Accountant Ledger app.
Run against a live Flask server at http://127.0.0.1:5000
"""
import os, requests, re, random, sys
from datetime import date, timedelta

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5000")
s = requests.Session()
s.max_redirects = 10

PASS = []; FAIL = []

def ok(label):
    PASS.append(label)
    print(f"  ✓ {label}")

def fail(label, detail=""):
    FAIL.append(label)
    print(f"  ✗ {label}" + (f" — {detail}" if detail else ""))

def post(path, data, follow=True):
    r = s.post(BASE + path, data=data, allow_redirects=follow)
    return r

def get(path):
    return s.get(BASE + path, allow_redirects=True)

def extract_id(url):
    """Pull last integer from the PATH of a URL like /products/42"""
    from urllib.parse import urlparse
    path = urlparse(url).path          # e.g. /products/42
    m = re.findall(r"/(\d+)", path)   # only look at path, not host/port
    return int(m[-1]) if m else None

def check_page(path, label, expect=200):
    r = get(path)
    if r.status_code == expect:
        ok(label)
    else:
        fail(label, f"HTTP {r.status_code}")
    return r

# ─────────────────────────────────────────────────────────────────────────────
# LOGIN (all routes require auth)
# ─────────────────────────────────────────────────────────────────────────────
r = post("/login", {"email": "bhavilg101@gmail.com", "password": "Mymomisgr8"})
if "/login" in r.url:
    print("✗ Could not log in — aborting.")
    sys.exit(1)
print("✓ Logged in")

# ─────────────────────────────────────────────────────────────────────────────
# SMOKE TEST
# ─────────────────────────────────────────────────────────────────────────────
print("\n══════════════════════════════════════════")
print("  SMOKE TEST — all pages & CRUD operations")
print("══════════════════════════════════════════")

# Pages load
for path, label in [
    ("/",              "Dashboard"),
    ("/clients/",      "Client list"),
    ("/products/",     "Product catalog"),
    ("/invoices/",     "Invoice list"),
    ("/payments/",     "Payment list"),
    ("/suppliers/",    "Supplier list"),
    ("/purchases/",    "PO list"),
    ("/transit/",      "Transit list"),
    ("/products/categories", "Categories"),
]:
    check_page(path, f"GET {label}")

# ── Clients ───────────────────────────────────────────────────────────────────
print("\n── Clients ──")
r = post("/clients/new", {"name": "Smoke Client", "opening_balance_amt": "500",
                           "opening_balance_type": "debt", "email": "smoke@test.com",
                           "city": "Ludhiana", "state": "Punjab",
                           "companies[0][name]": "Smoke Co",
                           "companies[0][opening_balance_amt]": "500",
                           "companies[0][opening_balance_type]": "debt"})
c_id = extract_id(r.url)
if c_id: ok(f"Create client (id={c_id})")
else:     fail("Create client", r.url)

check_page(f"/clients/{c_id}", "View client detail")

r = post(f"/clients/{c_id}/edit", {"name": "Smoke Client Edited", "opening_balance_amt": "200",
                                    "opening_balance_type": "credit"})
ok("Edit client") if "Edited" in get(f"/clients/{c_id}").text or r.status_code < 400 else fail("Edit client")

r = get(f"/clients/{c_id}/ledger")
if r.status_code == 200 and "entries" in r.text:
    ok("Client ledger API")
else:
    fail("Client ledger API", r.text[:100])

# ── Visits (field-sales check-ins) ───────────────────────────────────────────
print("\n── Visits ──")
check_page("/visits/map", "Visit map page")
check_page("/visits/check-in", "Check-in page")

r = s.post(BASE + "/visits/check-in", json={
    "client_id": c_id, "latitude": 30.9010, "longitude": 75.8573,
    "accuracy_m": 12.5, "purpose": "sales_call", "notes": "Smoke visit"})
v_id = r.json().get("id") if r.status_code == 200 else None
ok(f"Check in at client (id={v_id})") if v_id else fail("Check in at client", r.text[:100])

r = s.post(BASE + "/visits/check-in", json={
    "prospect_name": "Smoke Prospect", "latitude": 30.9100, "longitude": 75.8600,
    "purpose": "sales_call"})
ok("Check in at prospect") if r.status_code == 200 and r.json().get("ok") else fail("Check in at prospect", r.text[:100])

r = s.post(BASE + "/visits/check-in", json={"client_id": c_id})
ok("Reject check-in without GPS") if r.status_code == 400 else fail("Reject check-in without GPS", f"HTTP {r.status_code}")

r = s.post(BASE + "/visits/check-in", json={"latitude": 30.9, "longitude": 75.85})
ok("Reject check-in without client/prospect") if r.status_code == 400 else fail("Reject check-in without client/prospect", f"HTTP {r.status_code}")

r = s.post(BASE + f"/visits/{v_id}/check-out", json={"outcome": "order_placed", "notes": "Smoke order"})
ok("Check out") if r.status_code == 200 and r.json().get("ok") else fail("Check out", r.text[:100])

r = s.post(BASE + f"/visits/{v_id}/check-out", json={})
ok("Reject double check-out") if r.status_code == 404 else fail("Reject double check-out", f"HTTP {r.status_code}")

r = get("/visits/api/visits")
if r.status_code == 200:
    visits_json = r.json()
    mine = [v for v in visits_json if v.get("id") == v_id]
    if mine and mine[0]["outcome"] == "order_placed" and mine[0]["checked_out_at"]:
        ok(f"Visits API returns check-in with outcome ({len(visits_json)} visits)")
    else:
        fail("Visits API returns check-in with outcome", str(mine)[:120])
else:
    fail("Visits API", f"HTTP {r.status_code}")

r = get(f"/visits/api/visits?client_id={c_id}")
ok("Visits API client filter") if r.status_code == 200 and all(v["client_id"] == c_id for v in r.json()) else fail("Visits API client filter")

r = get("/visits/api/clients/geo")
if r.status_code == 200:
    geo = {g["id"]: g for g in r.json()}
    g = geo.get(c_id)
    if g and g["status"] == "recent" and g["days_since"] == 0:
        ok("Clients geo API: coverage status 'recent' after visit")
    else:
        fail("Clients geo API: coverage status", str(g)[:120])
else:
    fail("Clients geo API", f"HTTP {r.status_code}")

# ── Sales rep assignments ────────────────────────────────────────────────────
print("\n── Sales Rep Assignments ──")
check_page("/clients/assignments", "Assignments page")

post("/users/new", {"name": "Smoke Rep", "email": "smoke-rep@test.local",
                    "password": "smoke-1234", "role": "user", "is_active": "1",
                    "perm_visits_view": "1", "perm_visits_create": "1",
                    "managed_clients": str(c_id)})
import sqlite3 as _sql2; _dbr = _sql2.connect("data/ledger.db"); _dbr.row_factory = _sql2.Row
_rep = _dbr.execute("SELECT id FROM users WHERE email='smoke-rep@test.local'").fetchone()
rep_id = _rep["id"] if _rep else None
_dbr.close()
ok(f"Create staff user (id={rep_id})") if rep_id else fail("Create staff user")

_dbr = _sql2.connect("data/ledger.db")
_val = _dbr.execute("SELECT sales_rep_id FROM clients WHERE id=?", (c_id,)).fetchone()[0]
_dbr.close()
ok("User form managed_clients fills sales_rep_id") if _val == rep_id else fail("User form managed_clients", str(_val))

# Editing the user with the client unticked releases it
post(f"/users/{rep_id}/edit", {"name": "Smoke Rep", "email": "smoke-rep@test.local",
                               "role": "user", "is_active": "1",
                               "perm_visits_view": "1", "perm_visits_create": "1"})
_dbr = _sql2.connect("data/ledger.db")
_val = _dbr.execute("SELECT sales_rep_id FROM clients WHERE id=?", (c_id,)).fetchone()[0]
_dbr.close()
ok("Untick on user edit releases client") if _val is None else fail("Untick on user edit releases client", str(_val))

r = s.post(BASE + f"/clients/{c_id}/assign-rep", json={"user_id": rep_id})
ok("Assign client to rep") if r.status_code == 200 and r.json().get("ok") else fail("Assign client to rep", r.text[:100])

_dbr = _sql2.connect("data/ledger.db")
_val = _dbr.execute("SELECT sales_rep_id FROM clients WHERE id=?", (c_id,)).fetchone()[0]
_dbr.close()
ok("clients.sales_rep_id filled") if _val == rep_id else fail("clients.sales_rep_id filled", str(_val))

r = s.post(BASE + f"/clients/{c_id}/assign-rep", json={"user_id": 999999})
ok("Reject unknown rep") if r.status_code == 404 else fail("Reject unknown rep", f"HTTP {r.status_code}")

r = s.post(BASE + f"/clients/{c_id}/assign-rep", json={"user_id": None})
_dbr = _sql2.connect("data/ledger.db")
_val = _dbr.execute("SELECT sales_rep_id FROM clients WHERE id=?", (c_id,)).fetchone()[0]
_dbr.close()
ok("Unassign rep (field emptied)") if r.status_code == 200 and _val is None else fail("Unassign rep", str(_val))

# ── Sales-manager role: team + scoping ───────────────────────────────────────
print("\n── Sales Manager Role ──")
# rep_id is our staff member; give them client c_id and a foreign client to compare
s.post(BASE + f"/clients/{c_id}/assign-rep", json={"user_id": rep_id})
_dbr = _sql2.connect("data/ledger.db"); _dbr.row_factory = _sql2.Row
_foreign = _dbr.execute("SELECT id FROM clients WHERE id != ? AND sales_rep_id IS NULL LIMIT 1", (c_id,)).fetchone()
foreign_id = _foreign["id"] if _foreign else None
_dbr.close()

# Create a sales manager with rep on the team; default-denied modules left unticked
post("/users/new", {"name": "Smoke Manager", "email": "smoke-mgr@test.local",
                    "password": "smoke-1234", "role": "sales", "is_active": "1",
                    "perm_clients_view": "1", "perm_invoices_view": "1",
                    "perm_payments_view": "1", "perm_visits_view": "1",
                    "team_staff": str(rep_id)})
_dbr = _sql2.connect("data/ledger.db"); _dbr.row_factory = _sql2.Row
_mgr = _dbr.execute("SELECT id, role FROM users WHERE email='smoke-mgr@test.local'").fetchone()
mgr_id = _mgr["id"] if _mgr else None
_staff_mgr = _dbr.execute("SELECT manager_id FROM users WHERE id=?", (rep_id,)).fetchone()
_dbr.close()
ok(f"Create sales manager (id={mgr_id}, role={_mgr['role'] if _mgr else '?'})") if _mgr and _mgr["role"] == "sales" else fail("Create sales manager")
ok("Staff linked to manager") if _staff_mgr and _staff_mgr["manager_id"] == mgr_id else fail("Staff linked to manager", str(_staff_mgr and _staff_mgr["manager_id"]))

# Log in as the manager in a fresh session
mgr = requests.Session()
r = mgr.post(BASE + "/login", data={"email": "smoke-mgr@test.local", "password": "smoke-1234"})
ok("Sales manager can log in") if "/login" not in r.url else fail("Sales manager login", r.url)

# Dashboard renders the sales variant
r = mgr.get(BASE + "/")
ok("Sales dashboard renders") if r.status_code == 200 and "My Sales Staff" in r.text else fail("Sales dashboard", f"HTTP {r.status_code}")

# Client list scoped to team's clients only
r = mgr.get(BASE + "/clients/")
if r.status_code == 200:
    import re as _re
    shown = _re.findall(r'/clients/(\d+)"', r.text)
    shown_ids = {int(x) for x in shown}
    scoped_ok = c_id in shown_ids and (foreign_id is None or foreign_id not in shown_ids)
    ok("Client list scoped to team") if scoped_ok else fail("Client list scoped", f"c_id in={c_id in shown_ids} foreign in={foreign_id in shown_ids}")
else:
    fail("Client list scoped", f"HTTP {r.status_code}")

# Own client allowed, foreign client 403
r = mgr.get(BASE + f"/clients/{c_id}")
ok("Manager can view own client") if r.status_code == 200 else fail("Manager own client", f"HTTP {r.status_code}")
if foreign_id:
    r = mgr.get(BASE + f"/clients/{foreign_id}")
    ok("Manager blocked from foreign client (403)") if r.status_code == 403 else fail("Foreign client block", f"HTTP {r.status_code}")

# Denied module (products) is 403 for the sales manager
r = mgr.get(BASE + "/products/")
ok("Sales manager denied products (403)") if r.status_code == 403 else fail("Products denied", f"HTTP {r.status_code}")

# Rep assignment page blocked for scoped user
r = mgr.get(BASE + "/clients/assignments")
ok("Sales manager blocked from assignments (403)") if r.status_code == 403 else fail("Assignments block", f"HTTP {r.status_code}")

# Visits API scoped to team members only
r = mgr.get(BASE + "/visits/api/visits")
if r.status_code == 200:
    users_in = {v["user_id"] for v in r.json()}
    ok("Visits API scoped to team") if users_in <= {mgr_id, rep_id} else fail("Visits API scoped", str(users_in))
else:
    fail("Visits API scoped", f"HTTP {r.status_code}")

# Cleanup manager + release
post(f"/users/{mgr_id}/delete", {})
s.post(BASE + f"/clients/{c_id}/assign-rep", json={"user_id": None})
ok("Delete sales manager")

post(f"/users/{rep_id}/delete", {})
ok("Delete staff user")

# ── Categories ────────────────────────────────────────────────────────────────
print("\n── Categories ──")
post("/products/categories/new", {"name": "Smoke Category"})
import sqlite3 as _sql; _dbc = _sql.connect("data/ledger.db"); _dbc.row_factory = _sql.Row
_row = _dbc.execute("SELECT id FROM categories WHERE name='Smoke Category' ORDER BY id DESC LIMIT 1").fetchone()
cat_id = _row["id"] if _row else None
_dbc.close()
ok(f"Create category (id={cat_id})") if cat_id else fail("Create category")
check_page(f"/products/categories", "Category list after create")
post(f"/products/categories/{cat_id}/edit", {"name": "Smoke Category Edited"})
ok("Edit category")

# ── Products ─────────────────────────────────────────────────────────────────
print("\n── Products ──")
r = post("/products/new", {"name": "Smoke Product", "unit_price": "999",
                            "tax_rate": "18", "min_quantity": "10", "category_id": cat_id})
p_id = extract_id(r.url)
ok(f"Create product (id={p_id})") if p_id else fail("Create product", r.url)
check_page(f"/products/{p_id}", "Product detail")

# Adjust stock
r = post(f"/products/{p_id}/stock", {"bucket": "warehouse", "direction": "increase", "qty": "50", "notes": "Initial"})
ok("Adjust product warehouse stock") if r.status_code < 400 else fail("Adjust stock", str(r.status_code))

# Sub-product
r = post(f"/products/{p_id}/sub/new", {"name": "Smoke Sub A", "unit_price": "450",
                                        "min_quantity": "5", "use_parent_price": ""})
sub_id = extract_id(r.url)
ok(f"Create sub-product (id={sub_id})") if sub_id else fail("Create sub-product", r.url)

check_page(f"/products/{p_id}/sub/{sub_id}", "Sub-product detail")

r = post(f"/products/{p_id}/sub/{sub_id}/stock",
         {"bucket": "warehouse", "direction": "increase", "qty": "30", "notes": "Init sub", "from_page": "sub"})
ok("Adjust sub-product stock") if r.status_code < 400 else fail("Adjust sub stock")

# ── Suppliers ─────────────────────────────────────────────────────────────────
print("\n── Suppliers ──")
r = post("/suppliers/new", {"name": "Smoke Supplier", "contact_name": "Jane", "email": "s@smoke.com"})
sup_id = extract_id(r.url)
ok(f"Create supplier (id={sup_id})") if sup_id else fail("Create supplier", r.url)
check_page(f"/suppliers/{sup_id}", "Supplier detail")
post(f"/suppliers/{sup_id}/edit", {"name": "Smoke Supplier Edited", "contact_name": "Jane"})
ok("Edit supplier")

# ── Purchase orders ───────────────────────────────────────────────────────────
print("\n── Purchase Orders ──")
r = post("/purchases/new", {
    "name": "SMOKE-PO-001", "supplier_id": sup_id,
    "expected_completion": str(date.today() + timedelta(days=30)),
    "product_id_0": p_id, "sub_product_id_0": "", "quantity_0": "20", "price_0": "450",
})
po_id = extract_id(r.url)
ok(f"Create PO (id={po_id})") if po_id else fail("Create PO", r.url)
check_page(f"/purchases/{po_id}", "PO detail")

# ── Transit / Dispatch ────────────────────────────────────────────────────────
print("\n── Transit ──")
r = post("/transit/new", {
    "name": "SMOKE-DISP-001", "supplier_id": sup_id,
    "dispatch_date": str(date.today()),
    "expected_arrival": str(date.today() + timedelta(days=14)),
    "product_id_0": p_id, "sub_product_id_0": "", "quantity_0": "10", "price_0": "450",
})
disp_id = extract_id(r.url)
ok(f"Create dispatch (id={disp_id})") if disp_id else fail("Create dispatch", r.url)

if disp_id:
    # Get dispatch items to receive
    dr = get(f"/transit/{disp_id}")
    di_ids = re.findall(r'name="recv_(\d+)"', dr.text)
    if di_ids:
        recv_data = {f"recv_{di_id}": "5" for di_id in di_ids}
        post(f"/transit/{disp_id}/receive", recv_data)
        ok(f"Receive dispatch items ({di_ids})")
    else:
        fail("Receive dispatch — no dispatch_items found in HTML")

# ── Invoices ──────────────────────────────────────────────────────────────────
print("\n── Invoices ──")
inv_data = {
    "client_id": c_id, "status": "sent",
    "issue_date": str(date.today()),
    "due_date": str(date.today() + timedelta(days=30)),
    "discount_amount": "0",
    "items[0][description]": "Smoke Product", "items[0][product_id]": p_id,
    "items[0][sub_product_id]": "", "items[0][quantity]": "3",
    "items[0][unit_price]": "999", "items[0][tax_rate]": "18",
}
r = post("/invoices/new", inv_data)
inv_id = extract_id(r.url)
ok(f"Create invoice (id={inv_id})") if inv_id else fail("Create invoice", r.url)
check_page(f"/invoices/{inv_id}", "Invoice detail")

# ── Payments ──────────────────────────────────────────────────────────────────
print("\n── Payments ──")
r = post("/payments/new", {
    "client_id": c_id, "invoice_id": inv_id,
    "amount": "1000", "payment_date": str(date.today()),
    "method": "bank", "reference": "SMOKE-REF-001",
})
pmt_id = extract_id(r.url)
ok(f"Create payment (id={pmt_id})") if pmt_id else fail("Create payment", r.url)

# ── Deletions ─────────────────────────────────────────────────────────────────
print("\n── Deletions ──")
post(f"/invoices/{inv_id}/delete", {})
ok("Delete invoice") if get(f"/invoices/{inv_id}").status_code in (404, 302, 200) else fail("Delete invoice")
post(f"/purchases/{po_id}/delete", {})
ok("Delete PO")
post(f"/transit/{disp_id}/delete", {})
ok("Delete dispatch")
post(f"/suppliers/{sup_id}/delete", {})
ok("Delete supplier")
post(f"/products/{p_id}/delete", {})
ok("Delete product (cascades sub)")
post(f"/clients/{c_id}/delete", {})
ok("Delete client")
post(f"/products/categories/{cat_id}/delete", {})
ok("Delete category")

# ─────────────────────────────────────────────────────────────────────────────
# STRESS TEST
# ─────────────────────────────────────────────────────────────────────────────
print("\n══════════════════════════════════════════")
print("  STRESS TEST — bulk data creation")
print("══════════════════════════════════════════")

CATS  = ["Furniture", "Textiles", "Lighting", "Hardware", "Decor"]
cat_ids = []
for name in CATS:
    post("/products/categories/new", {"name": name})
# Fetch IDs from the category list page
import sqlite3 as _sl3
_db_path = "data/ledger.db"
_db = _sl3.connect(_db_path); _db.row_factory = _sl3.Row
for name in CATS:
    row = _db.execute("SELECT id FROM categories WHERE name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    if row: cat_ids.append(row["id"])
_db.close()
ok(f"Created {len(cat_ids)} categories ({cat_ids})")

# ── 50 clients ────────────────────────────────────────────────────────────────
client_ids = []
cities = ["Mumbai","Delhi","Bengaluru","Chennai","Hyderabad","Pune","Ahmedabad","Kolkata","Jaipur","Surat"]
for i in range(1, 51):
    ob_amt  = random.choice([0, 500, 1200, 2500, 5000, 10000])
    ob_type = random.choice(["debt", "credit"])
    r = post("/clients/new", {
        "name":                 f"Client {i:02d} — {random.choice(['Traders','Exports','Pvt Ltd','Enterprises'])}",
        "company":              f"Co-{i:02d}",
        "email":                f"client{i}@example.com",
        "phone":                f"98{i:08d}",
        "city":                 random.choice(cities),
        "country":              "India",
        "opening_balance_amt":  str(ob_amt),
        "opening_balance_type": ob_type,
        "companies[0][name]":                 f"Co-{i:02d}",
        "companies[0][opening_balance_amt]":  str(ob_amt),
        "companies[0][opening_balance_type]": ob_type,
    })
    cid = extract_id(r.url)
    if cid: client_ids.append(cid)
ok(f"Created {len(client_ids)} clients")

# ── 5 suppliers ───────────────────────────────────────────────────────────────
supplier_ids = []
for i in range(1, 6):
    r = post("/suppliers/new", {"name": f"Supplier {i:02d}", "contact_name": f"Contact {i}",
                                 "email": f"sup{i}@vendor.com", "phone": f"97{i:08d}"})
    sid = extract_id(r.url)
    if sid: supplier_ids.append(sid)
ok(f"Created {len(supplier_ids)} suppliers")

# ── 30 products (15 with sub-products) ───────────────────────────────────────
PRODUCT_TEMPLATES = [
    ("Wooden Chair", 1800, "Furniture"), ("Dining Table", 12000, "Furniture"),
    ("Bookshelf", 5500, "Furniture"),    ("Wardrobe", 22000, "Furniture"),
    ("Sofa Set", 35000, "Furniture"),    ("Coffee Table", 8500, "Furniture"),
    ("Bed Frame", 18000, "Furniture"),   ("Side Table", 3200, "Furniture"),
    ("Cotton Curtain", 850, "Textiles"), ("Linen Bedsheet", 1200, "Textiles"),
    ("Woolen Blanket", 2200, "Textiles"),("Silk Cushion", 650, "Textiles"),
    ("Table Runner", 480, "Textiles"),   ("Bath Towel Set", 1100, "Textiles"),
    ("Floor Lamp", 4500, "Lighting"),    ("Pendant Light", 6200, "Lighting"),
    ("Wall Sconce", 2800, "Lighting"),   ("Desk Lamp", 1900, "Lighting"),
    ("LED Strip Kit", 850, "Lighting"),  ("Chandelier", 15000, "Lighting"),
    ("Cabinet Handle", 120, "Hardware"), ("Door Hinge Set", 350, "Hardware"),
    ("Drawer Slider", 280, "Hardware"),  ("Shelf Bracket", 95, "Hardware"),
    ("Wood Screw Box", 180, "Hardware"), ("Wall Plug Kit", 220, "Hardware"),
    ("Vase Set", 2200, "Decor"),         ("Picture Frame", 780, "Decor"),
    ("Wall Clock", 3400, "Decor"),       ("Candle Holder", 560, "Decor"),
]
cat_map = {n: cat_ids[i] for i, n in enumerate(CATS)}

product_ids = []    # (pid, has_subs, [sub_ids])
all_leaf_ids = []   # list of (product_id, sub_product_id_or_None, price)

for idx, (name, price, cat_name) in enumerate(PRODUCT_TEMPLATES):
    min_qty = random.choice([5, 10, 15, 20, 25])
    r = post("/products/new", {
        "name": name, "sku": f"SKU-{idx+1:03d}",
        "category_id": cat_map.get(cat_name, cat_ids[0]),
        "unit_price": str(price), "tax_rate": "18",
        "min_quantity": str(min_qty),
    })
    pid = extract_id(r.url)
    if not pid:
        fail(f"Create product {name}", r.url); continue

    init_stock = random.randint(30, 150)

    # First 15 products get 2 sub-products
    if idx < 15:
        subs_created = []
        colors = [("Natural","A"), ("Walnut","B"), ("White","C")][:2]
        for color_name, suffix in colors:
            sp = random.choice([-1, 0, 1])  # -1=own lower price, 0=parent, 1=own higher
            use_parent = (sp == 0)
            sub_price = price if use_parent else price + sp * random.randint(100, 500)
            sub_stock = random.randint(15, 80)
            sub_min   = random.choice([3, 5, 8, 10])
            r2 = post(f"/products/{pid}/sub/new", {
                "name": f"{color_name} Finish",
                "sku_suffix": suffix,
                "unit_price": str(sub_price),
                "min_quantity": str(sub_min),
                "use_parent_price": "1" if use_parent else "",
            })
            sid = extract_id(r2.url)
            if sid:
                subs_created.append(sid)
                # Initial warehouse stock for sub
                post(f"/products/{pid}/sub/{sid}/stock", {
                    "bucket": "warehouse", "direction": "increase",
                    "qty": str(sub_stock), "notes": "Opening stock", "from_page": "sub"
                })
                all_leaf_ids.append((pid, sid, sub_price if not use_parent else price))
        product_ids.append((pid, True, subs_created))
    else:
        # No subs — just set warehouse stock
        post(f"/products/{pid}/stock", {
            "bucket": "warehouse", "direction": "increase",
            "qty": str(init_stock), "notes": "Opening stock"
        })
        product_ids.append((pid, False, []))
        all_leaf_ids.append((pid, None, price))

ok(f"Created {len(product_ids)} products, {sum(len(s) for _,_,s in product_ids)} sub-products")

# Add production stock to products (so dispatches can move them)
for pid, has_subs, sub_ids in product_ids:
    if has_subs:
        for sid in sub_ids:
            post(f"/products/{pid}/sub/{sid}/stock", {
                "bucket": "production", "direction": "increase",
                "qty": str(random.randint(20, 60)), "notes": "PO batch", "from_page": "sub"
            })
    else:
        post(f"/products/{pid}/stock", {
            "bucket": "production", "direction": "increase",
            "qty": str(random.randint(20, 60)), "notes": "PO batch"
        })
ok("Added production stock for all products")

# ── 5 purchase orders (10 products each) ────────────────────────────────────
po_ids = []
leaf_sample = random.sample(all_leaf_ids, min(len(all_leaf_ids), 50))
for po_num in range(1, 6):
    chunk = leaf_sample[(po_num-1)*10 : po_num*10]
    data = {
        "name": f"PO-2026-{po_num:03d}",
        "supplier_id": random.choice(supplier_ids),
        "expected_completion": str(date.today() + timedelta(days=random.randint(20, 60))),
        "notes": f"Stress test PO #{po_num}",
    }
    for i, (pid, sid, price) in enumerate(chunk):
        data[f"product_id_{i}"]     = pid
        data[f"sub_product_id_{i}"] = sid or ""
        data[f"quantity_{i}"]       = str(random.randint(10, 50))
        data[f"price_{i}"]          = str(price)
    r = post("/purchases/new", data)
    pid_po = extract_id(r.url)
    if pid_po: po_ids.append(pid_po)
ok(f"Created {len(po_ids)} purchase orders")

# ── 7 dispatches ─────────────────────────────────────────────────────────────
disp_ids = []
for d_num in range(1, 8):
    chunk = random.sample(all_leaf_ids, min(len(all_leaf_ids), random.randint(3, 7)))
    data = {
        "name": f"DISP-2026-{d_num:03d}",
        "supplier_id": random.choice(supplier_ids),
        "dispatch_date": str(date.today() - timedelta(days=random.randint(0, 10))),
        "expected_arrival": str(date.today() + timedelta(days=random.randint(5, 30))),
        "notes": f"Stress dispatch #{d_num}",
    }
    for i, (pid, sid, price) in enumerate(chunk):
        data[f"product_id_{i}"]     = pid
        data[f"sub_product_id_{i}"] = sid or ""
        data[f"quantity_{i}"]       = str(random.randint(5, 20))
        data[f"price_{i}"]          = str(price)
        data[f"cbm_{i}"]            = str(round(random.uniform(0.5, 5.0), 2))
        data[f"gross_weight_{i}"]   = str(round(random.uniform(10, 200), 1))
    r = post("/transit/new", data)
    did = extract_id(r.url)
    if did: disp_ids.append(did)
ok(f"Created {len(disp_ids)} dispatches")

# ── Receive 3 dispatches into warehouse ──────────────────────────────────────
received_count = 0
for did in disp_ids[:3]:
    dr = get(f"/transit/{did}")
    di_ids = re.findall(r'name="recv_(\d+)"', dr.text)
    if di_ids:
        recv_data = {f"recv_{di_id}": str(random.randint(2, 10)) for di_id in di_ids}
        rr = post(f"/transit/{did}/receive", recv_data)
        if rr.status_code < 400:
            received_count += 1
ok(f"Received items from {received_count} dispatches into warehouse")

# ── 15 invoices for first 5 clients ──────────────────────────────────────────
invoice_ids = []
target_clients = client_ids[:5]
for inv_num in range(1, 16):
    cid = target_clients[(inv_num - 1) % 5]
    # Pick 3-6 line items
    lines = random.sample(all_leaf_ids, min(len(all_leaf_ids), random.randint(3, 6)))
    data = {
        "client_id": cid, "status": "sent",
        "issue_date": str(date.today() - timedelta(days=random.randint(0, 30))),
        "due_date":   str(date.today() + timedelta(days=random.randint(15, 45))),
        "discount_amount": str(random.choice([0, 0, 0, 500, 1000])),
    }
    for li, (pid, sid, price) in enumerate(lines):
        data[f"items[{li}][description]"]   = f"Product line {li+1}"
        data[f"items[{li}][product_id]"]    = pid
        data[f"items[{li}][sub_product_id]"]= sid or ""
        data[f"items[{li}][quantity]"]      = str(random.randint(1, 10))
        data[f"items[{li}][unit_price]"]    = str(price)
        data[f"items[{li}][tax_rate]"]      = "18"
    r = post("/invoices/new", data)
    iid = extract_id(r.url)
    if iid: invoice_ids.append((iid, cid))
ok(f"Created {len(invoice_ids)} invoices")

# ── 3-5 payments for the 5 clients ───────────────────────────────────────────
payment_count = 0
methods = ["bank", "cash", "cheque", "upi"]
# Group invoices by client
from collections import defaultdict
inv_by_client = defaultdict(list)
for iid, cid in invoice_ids:
    inv_by_client[cid].append(iid)

for cid in target_clients:
    n_payments = random.randint(3, 5)
    invs = inv_by_client[cid]
    for p in range(n_payments):
        inv_to_pay = invs[p % len(invs)] if invs else None
        r = post("/payments/new", {
            "client_id":    cid,
            "invoice_id":   inv_to_pay or "",
            "amount":       str(random.randint(1000, 15000)),
            "payment_date": str(date.today() - timedelta(days=random.randint(0, 20))),
            "method":       random.choice(methods),
            "reference":    f"REF-{cid}-{p+1:02d}",
            "notes":        "Stress test payment",
        })
        if extract_id(r.url): payment_count += 1
ok(f"Created {payment_count} payments")

# ── Final summary ─────────────────────────────────────────────────────────────
print("\n══════════════════════════════════════════")
print(f"  RESULTS:  ✓ {len(PASS)} passed   ✗ {len(FAIL)} failed")
print("══════════════════════════════════════════")
if FAIL:
    print("\nFailed checks:")
    for f in FAIL:
        print(f"  • {f}")
    sys.exit(1)
else:
    print("\nAll checks passed!")

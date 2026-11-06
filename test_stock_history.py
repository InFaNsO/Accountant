"""Stock history shows every movement, a page at a time.

It used to stop at the newest 100 movements per tab, which on a busy product is
only about 90 days. Walking all the pages must give back every movement exactly
once, newest first, even when many share the same second.

Runs on a snapshot copy of data/ledger.db; the real file is never written.
Run with the repo venv:  .venv/Scripts/python.exe test_stock_history.py
"""
import os
import re
import sqlite3
import sys
import tempfile

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

from app.services import product_service        # noqa: E402

flask_app = app_pkg.create_app()
fails = 0


def check(cond, label):
    global fails
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails += 1


db = sqlite3.connect(tmp)
god = db.execute("SELECT id FROM users WHERE role='god' ORDER BY id LIMIT 1").fetchone()[0]


def add_product(name):
    return db.execute("INSERT INTO products (name, sku) VALUES (?, ?)", (name, name)).lastrowid


def add_moves(prefix, count, movement_type, product_id=None, sub_id=None, same_second=0):
    """`count` movements, one a day from Jan 2025, the newest `same_second` of them at
    one instant. Returns their notes, newest first."""
    notes = []
    for n in range(count):
        day = min(n, count - same_second) if same_second else n
        note = f"{prefix}-{n:04d}"
        db.execute("INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, "
                   "notes, created_at) VALUES (?, ?, ?, 1, ?, datetime('2025-01-01', ?))",
                   (product_id, sub_id, movement_type, note, f"+{day} days"))
        notes.append(note)
    return notes[::-1]


busy = add_product("HIST-busy")
wh_notes = add_moves("wh", 230, "warehouse_add", product_id=busy, same_second=40)
pr_notes = add_moves("pr", 120, "production_add", product_id=busy)

parent = add_product("HIST-parent")
sub = db.execute("INSERT INTO sub_products (product_id, name) VALUES (?, ?)", (parent, "HIST-sub")).lastrowid
sub_notes = add_moves("sb", 60, "warehouse_add", product_id=parent, sub_id=sub)
db.commit()

client = flask_app.test_client()
with client.session_transaction() as sess:
    sess["_user_id"] = str(god)
    sess["_fresh"] = True


def page(path, **params):
    r = client.get(path, query_string=params)
    return r.status_code, r.get_data(as_text=True)


def notes_on(html, prefix):
    return re.findall(rf">({prefix}-\d{{4}})<", html)


def pane(html, name):
    """The markup of one tab, from its opening div up to the next tab or the end."""
    start = html.index(f'id="tab-{name}"')
    following = [html.find(f'id="tab-{other}"', start + 1) for other in ("production", "transit")]
    following = [x for x in following if x > 0]
    return html[start:min(following) if following else len(html)]


def walk(path, prefix, per_page, **params):
    seen, n = [], 1
    while True:
        status, html = page(path, page=n, per_page=per_page, **params)
        rows = notes_on(pane(html, params.get("tab", "warehouse")), prefix)
        if status != 200 or not rows or rows[0] in seen:
            return seen
        seen += rows
        n += 1


BUSY = f"/products/{busy}"
SUB = f"/products/{parent}/sub/{sub}"

print("-- default view")
status, html = page(BUSY)
check(status == 200, "product page opens")
check(len(notes_on(pane(html, "warehouse"), "wh")) == 50, "opens on 50 entries")
check("1–50 of 230" in html, "says how far through the history it is")
check('aria-current="page">1<' in html, "marks the current page")
check('rel="next"' in html and 'rel="prev"' not in html, "page 1 offers Older, not Newer")

print("-- nothing is lost")
check(walk(BUSY, "wh", 25) == wh_notes, "25 a page: all 230 once, newest first")
check(walk(BUSY, "wh", 50) == wh_notes, "50 a page: all 230 once, newest first")
check(walk(BUSY, "wh", 100) == wh_notes, "100 a page: all 230 once, newest first")
check(walk(BUSY, "wh", 200) == wh_notes, "200 a page: all 230 once, newest first")
check(walk(BUSY, "pr", 50, tab="production") == pr_notes, "production tab pages too")
check(walk(SUB, "sb", 25) == sub_notes, "sub-product page pages too")

print("-- the old cap is gone")
status, html = page(BUSY, page=5, per_page=50)
check(wh_notes[-1] in notes_on(html, "wh"), "the oldest entry can be reached")
check("201–230 of 230" in html, "last page shows what is left")
last = pane(html, "warehouse")
check('rel="prev"' in last and 'rel="next"' not in last, "last page offers Newer, not Older")

print("-- tabs")
status, html = page(BUSY, tab="production", page=2)
check(notes_on(pane(html, "production"), "pr")[0] == pr_notes[50], "opens on the chosen tab and page")
check('id="tab-warehouse" class="stock-tab-pane" style="display:none"' in html, "other tabs stay hidden")
check('id="tab-production" class="stock-tab-pane">' in html, "chosen tab is showing")
check(notes_on(pane(html, "warehouse"), "wh")[0] == wh_notes[0], "other tabs stay on their newest entries")
check("No transit history yet." in html, "an empty tab still says so")

print("-- links keep your place")
status, html = page(BUSY, tab="production", page=2, per_page=25)
links = re.findall(r'rel="next"[^>]*?href="([^"]*)"', pane(html, "production"))
link = links[0].replace("&amp;", "&") if links else ""
for part in ("tab=production", "page=3", "per_page=25", "#stock-history"):
    check(part in link, f"Older link has {part}")
check(f'action="{BUSY}#stock-history"' in html, "per-page box stays on this product")

print("-- bad input never breaks the page")
for params, want, label in (
        ({"page": 9999}, "201–230 of 230", "page past the end shows the last page"),
        ({"page": 0}, "1–50 of 230", "page 0 shows the first page"),
        ({"page": -3}, "1–50 of 230", "negative page shows the first page"),
        ({"page": "abc"}, "1–50 of 230", "page that is not a number shows the first page"),
        ({"per_page": 7}, "1–50 of 230", "unknown per_page uses the default"),
        ({"per_page": "abc"}, "1–50 of 230", "per_page that is not a number uses the default"),
        ({"per_page": 100000}, "1–50 of 230", "a huge per_page is refused"),
        ({"tab": "bogus"}, "1–50 of 230", "unknown tab opens Warehouse")):
    status, html = page(BUSY, **params)
    check(status == 200 and want in html, label)

print("-- other pages")
status, html = page(f"/products/{parent}")
check(status == 200 and 'id="stock-history"' not in html, "a product with sub-products still has no history card")
empty = add_product("HIST-empty")
db.commit()
status, html = page(f"/products/{empty}")
check(status == 200 and "No warehouse history yet." in html and "hist-pager" not in html.split("<style>")[0],
      "a product with no history shows no pager")

print("-- count and rows agree on real data")
row = db.execute("""SELECT sm.product_id, COUNT(*) n FROM stock_movements sm
                    WHERE sm.sub_product_id IS NULL AND sm.notes NOT LIKE 'wh-%' AND sm.notes NOT LIKE 'pr-%'
                    GROUP BY sm.product_id ORDER BY n DESC LIMIT 1""").fetchone()
if row:
    with flask_app.app_context():
        total = product_service.count_stock_history(product_id=row[0])
        rows = product_service.get_stock_history(product_id=row[0], limit=total + 10)
    check(total == row[1] == len(rows), f"product {row[0]}: {total} movements, all returned")
    check(len({r["id"] for r in rows}) == total, "no repeats")

db.close()
print("\nALL PASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

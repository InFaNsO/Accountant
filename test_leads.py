"""Leads: create/edit with interests and areas, the status timeline, duplicate
warnings, overlap detection against real client areas, conversion to a client,
permissions and sales-team scoping — and the client page's region editor still
working after it moved into the shared _region_editor.html partial.

Overlap cases use boundaries already cached in the database (no geocoding
calls): a lead placed on a client's exact area must overlap that client; a lead
on a district that only borders another client's district must not.

Runs on a snapshot copy of data/ledger.db; the real file is never written.
Run:  python test_leads.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid

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

flask_app = app_pkg.create_app()
flask_app.config["TESTING"] = True
http = flask_app.test_client()
db = sqlite3.connect(tmp)
db.row_factory = sqlite3.Row
q1 = lambda sql, *a: db.execute(sql, a).fetchone()
qa = lambda sql, *a: db.execute(sql, a).fetchall()

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def login(user_id):
    with http.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def new_lead(**fields):
    form = {"company_name": "Test Co", "status": "need_to_talk", **fields}
    r = http.post("/leads/new", data=form)
    assert r.status_code == 302, (r.status_code, r.data[:300])
    return q1("SELECT MAX(id) AS id FROM leads")["id"]


def overlaps(lead_id):
    r = http.get(f"/leads/{lead_id}/overlaps")
    assert r.status_code == 200, r.status_code
    return r.get_json()


god = q1("SELECT id FROM users WHERE role='god' ORDER BY id")["id"]
login(god)

# ── Pick real areas from the client data ─────────────────────────────────────
shared = q1("""SELECT boundary_id FROM client_regions WHERE geometry_override IS NULL
               GROUP BY boundary_id ORDER BY COUNT(*) DESC, boundary_id LIMIT 1""")["boundary_id"]
shared_clients = {r["client_id"] for r in qa("SELECT client_id FROM client_regions WHERE boundary_id=?", shared)}
print(f"shared boundary {shared} used by clients {sorted(shared_clients)}")

# ── Segments ─────────────────────────────────────────────────────────────────
tag = uuid.uuid4().hex[:6]   # the snapshot may already hold segments; use a fresh name
r = http.post("/leads/segments", json={"name": f"  Paint   dealer {tag} "})
seg = r.get_json()
check(r.status_code == 200 and seg["created"] and seg["name"] == f"Paint dealer {tag}", "segment created, name tidied")
r = http.post("/leads/segments", json={"name": f"paint DEALER {tag}"})
check(r.get_json()["id"] == seg["id"] and not r.get_json()["created"], "same segment name in another case reuses it")

# ── Create with interests + areas ────────────────────────────────────────────
cat = q1("SELECT id FROM categories ORDER BY id")["id"]
prod = q1("SELECT id FROM products WHERE is_active=1 ORDER BY id")["id"]
a = new_lead(company_name="Alpha Traders", contact_person="Ravi", phone="+91 98765-43210",
             city="Ludhiana", state="Punjab", source="Expo 2026",
             category_ids=[cat], product_ids=[prod], segment_ids=[seg["id"]],
             add_areas=[json.dumps({"boundary_id": shared})])
lead = q1("SELECT * FROM leads WHERE id=?", a)
check(lead["company_name"] == "Alpha Traders" and lead["created_by"] == god, "lead saved with creator")
check(q1("SELECT COUNT(*) n FROM lead_categories WHERE lead_id=?", a)["n"] == 1
      and q1("SELECT COUNT(*) n FROM lead_products WHERE lead_id=?", a)["n"] == 1
      and q1("SELECT COUNT(*) n FROM lead_segment_links WHERE lead_id=?", a)["n"] == 1, "interests saved")
check(q1("SELECT COUNT(*) n FROM lead_regions WHERE lead_id=? AND boundary_id=?", a, shared)["n"] == 1,
      "area from the form linked")
check(q1("SELECT kind FROM lead_activities WHERE lead_id=?", a)["kind"] == "created", "timeline starts with 'created'")

r = http.post("/leads/new", data={"company_name": "  "})
check(r.status_code == 200 and b"Company name is required" in r.data, "blank company name refused")

r = http.post("/leads/new", data={"company_name": "Beta", "source": "Expo 2026", "state": "Punjab",
                                  "submit": "add_another"})
check(r.status_code == 302 and "source=Expo+2026" in r.headers["Location"], "save & add another keeps the batch source")
b_quick = q1("SELECT MAX(id) id FROM leads")["id"]

# ── Overlaps ─────────────────────────────────────────────────────────────────
found = overlaps(a)
clients_hit = {o["id"] for o in found if o["kind"] == "client"}
check(shared_clients <= clients_hit, "lead on a client's exact area overlaps every client using it")
check(all(o["share_here"] > 0.99 for o in found if o["id"] in shared_clients), "…covering 100% of the lead's area")
check(all("geometry" in o for o in found), "overlap list carries shapes for the map")

b = new_lead(company_name="Gamma Supplies", add_areas=[json.dumps({"boundary_id": shared})])
check(a in {o["id"] for o in overlaps(b) if o["kind"] == "lead"}, "second lead in the same area overlaps the first")
check(b in {o["id"] for o in overlaps(a) if o["kind"] == "lead"}, "…and the first overlaps the second")

sea = {"type": "Polygon", "coordinates": [[[65.0, 15.0], [65.2, 15.0], [65.2, 15.2], [65.0, 15.2], [65.0, 15.0]]]}
c = new_lead(company_name="Offshore Ltd")
r = http.post(f"/leads/{c}/regions/custom", json={"geometry": sea, "name": "Sea"})
check(r.status_code == 200 and r.get_json()["ok"], "custom area drawn on a lead")
check(overlaps(c) == [], "area nobody else works in has no overlaps")

# Neighbours: a district that only shares a border must not count.
from shapely.geometry import shape  # noqa: E402
pair = None
geoms = {r["id"]: shape(json.loads(r["geometry"])).buffer(0) for r in qa(
    "SELECT id, geometry FROM area_boundaries WHERE id IN (SELECT boundary_id FROM client_regions) AND source != 'custom'")}
for i in geoms:
    for j in geoms:
        if i < j and geoms[i].intersects(geoms[j]):
            share = geoms[i].intersection(geoms[j]).area / min(geoms[i].area, geoms[j].area)
            if 0 < share < 0.01:
                pair = (i, j)
                break
    if pair:
        break
if pair:
    n = new_lead(company_name="Neighbour Co", add_areas=[json.dumps({"boundary_id": pair[0]})])
    neighbours = {r["client_id"] for r in qa("SELECT client_id FROM client_regions WHERE boundary_id=?", pair[1])}
    own = {r["client_id"] for r in qa("SELECT client_id FROM client_regions WHERE boundary_id=?", pair[0])}
    hit = {o["id"] for o in overlaps(n) if o["kind"] == "client"}
    check(not ((neighbours - own) & hit), f"border-only neighbour (boundary {pair[1]}) is not an overlap")
else:
    print("skip neighbour case — no touching pair in this data")

# ── Duplicates ───────────────────────────────────────────────────────────────
dup = http.get("/leads/duplicates?phone=09876543210").get_json()
check(any(x["kind"] == "lead" and x["id"] == a and x["match"] == "phone" for x in dup),
      "same phone in another format is flagged")
dup = http.get(f"/leads/duplicates?phone=9876543210&exclude={a}").get_json()
check(not any(x["kind"] == "lead" and x["id"] == a for x in dup), "a lead isn't its own duplicate")
cl = q1("SELECT id, phone FROM clients WHERE phone IS NOT NULL AND length(phone) >= 10 LIMIT 1")
if cl:
    dup = http.get(f"/leads/duplicates?phone={cl['phone']}").get_json()
    check(any(x["kind"] == "client" and x["id"] == cl["id"] for x in dup), "client with that phone is flagged")
dup = http.get("/leads/duplicates?company=ALPHA traders").get_json()
check(any(x["id"] == a and x["match"] == "company" for x in dup), "same company name (any case) is flagged")

# ── Status, notes, follow-up ─────────────────────────────────────────────────
r = http.post(f"/leads/{a}/status", json={"status": "rates_given"})
check(r.status_code == 200 and r.get_json()["label"] == "Rates given", "status change via JSON")
act = q1("SELECT * FROM lead_activities WHERE lead_id=? AND kind='status' ORDER BY id DESC", a)
check(act and act["old_status"] == "need_to_talk" and act["new_status"] == "rates_given", "status change on the timeline")
check(http.post(f"/leads/{a}/status", json={"status": "bogus"}).status_code == 400, "unknown status refused")
http.post(f"/leads/{a}/status", data={"status": "samples_sent", "note": "Sent 3 discs"})
act = q1("SELECT * FROM lead_activities WHERE lead_id=? ORDER BY id DESC", a)
check(act["new_status"] == "samples_sent" and act["body"] == "Sent 3 discs", "status change keeps its note")
http.post(f"/leads/{a}/notes", data={"note": "Call back after Diwali"})
note = q1("SELECT * FROM lead_activities WHERE lead_id=? AND kind='note' ORDER BY id DESC", a)
check(note and note["body"] == "Call back after Diwali", "note added")
http.post(f"/leads/{a}/notes/{note['id']}/delete")
check(q1("SELECT COUNT(*) n FROM lead_activities WHERE id=?", note["id"])["n"] == 0, "note deleted")
http.post(f"/leads/{a}/notes/{act['id']}/delete")
check(q1("SELECT COUNT(*) n FROM lead_activities WHERE id=?", act["id"])["n"] == 1, "status history can't be deleted")
http.post(f"/leads/{a}/follow-up", data={"next_follow_up": "2026-11-02"})
check(q1("SELECT next_follow_up f FROM leads WHERE id=?", a)["f"] == "2026-11-02", "follow-up set")

# ── Pages ────────────────────────────────────────────────────────────────────
r = http.get("/leads/")
html = r.data.decode()
check(r.status_code == 200 and "Alpha Traders" in html and "Gamma Supplies" in html, "list page shows leads")
check('class="ov-badge ov-client"' in html, "list page flags overlapping clients")
r = http.get(f"/leads/{a}")
html = r.data.decode()
check(r.status_code == 200 and "Alpha Traders" in html and f'"/leads/{a}"' in html, "detail page renders with its map")
check("Samples sent" in html and f"Paint dealer {tag}" in html, "detail shows status and interests")
r = http.get(f"/leads/{a}/edit")
check(r.status_code == 200 and b'value="Alpha Traders"' in r.data, "edit form prefilled")
check(f'"boundary_id": {shared}' in r.data.decode(), "edit form hands its saved areas to the map")
check(http.get("/leads/new").status_code == 200 and b'id="areaMap"' in http.get("/leads/new").data,
      "new form renders with the area map")

# The form's map previews a search result's real boundary before saving.
ref = q1("SELECT id, source_ref FROM area_boundaries WHERE source='nominatim' ORDER BY id LIMIT 1")
_, ot, oi = ref["source_ref"].split(":")
r = http.get(f"/leads/regions/resolve?osm_type={ot}&osm_id={oi}&name=x")
check(r.status_code == 200 and r.get_json()["boundary_id"] == ref["id"]
      and r.get_json()["geometry"]["type"] in ("Polygon", "MultiPolygon"),
      "resolve returns the cached boundary with its shape")
check(http.get("/leads/regions/resolve?osm_type=bogus&osm_id=1").status_code == 400, "resolve rejects a bad place type")
check(http.get("/leads/segments").status_code == 200, "segments page renders")

# ── Edit ─────────────────────────────────────────────────────────────────────
link = q1("SELECT id FROM lead_regions WHERE lead_id=?", b)["id"]
r = http.post(f"/leads/{b}/edit", data={"company_name": "Gamma Supplies", "contact_person": "Neha",
                                        "status": "other", "remove_regions": [str(link)]})
check(r.status_code == 302, "edit saved")
row = q1("SELECT * FROM leads WHERE id=?", b)
check(row["contact_person"] == "Neha" and row["status"] == "other", "edit changed fields and status")
check(q1("SELECT COUNT(*) n FROM lead_regions WHERE lead_id=?", b)["n"] == 0, "edit removed the area")
check(b not in {o["id"] for o in overlaps(a)}, "removed area no longer overlaps")

# ── Convert to client ────────────────────────────────────────────────────────
r = http.get(f"/clients/new?from_lead={a}")
check(r.status_code == 200 and b"Alpha Traders - Ludhiana" in r.data and b'name="from_lead"' in r.data,
      "client form prefilled from the lead")
r = http.post("/clients/new", data={"name": "Alpha Traders - Ludhiana", "phone": "9876543210",
                                    "payment_terms": "30", "companies[0][name]": "Alpha Traders",
                                    "companies[0][opening_balance_amt]": "0",
                                    "companies[0][opening_balance_type]": "debt", "from_lead": str(a)})
check(r.status_code == 302, "client created from lead")
new_client = q1("SELECT client_id FROM leads WHERE id=?", a)["client_id"]
check(new_client is not None, "lead linked to its client")
check(q1("SELECT COUNT(*) n FROM client_regions WHERE client_id=? AND boundary_id=?", new_client, shared)["n"] == 1,
      "client got the lead's areas")
check(new_client not in {o["id"] for o in overlaps(a) if o["kind"] == "client"}, "lead doesn't overlap its own client")
check(q1("SELECT kind FROM lead_activities WHERE lead_id=? ORDER BY id DESC", a)["kind"] == "converted",
      "conversion on the timeline")
r = http.get(f"/clients/new?from_lead={a}")
check(b'name="from_lead"' not in r.data, "an already-converted lead can't be converted again")

# ── Client page still runs the (now shared) region editor ────────────────────
cid = sorted(shared_clients)[0]
r = http.get(f"/clients/{cid}")
html = r.data.decode()
check(r.status_code == 200 and f'const REGION_BASE = "/clients/{cid}"' in html
      and 'const REGION_LOOKUP = "/clients/regions"' in html, "client page embeds the shared editor")
regions = http.get(f"/clients/{cid}/regions").get_json()
check(len(regions) == q1("SELECT COUNT(*) n FROM client_regions WHERE client_id=?", cid)["n"],
      "client region list endpoint unchanged")
r = http.post(f"/clients/{cid}/regions/custom", json={"geometry": sea, "name": "Test sea"})
bid = r.get_json().get("boundary_id")
link = q1("SELECT id FROM client_regions WHERE client_id=? AND boundary_id=?", cid, bid)
check(link is not None, "client custom area still saves")
r = http.post(f"/clients/{cid}/regions/{link['id']}/rename", json={"name": "Renamed"})
check(r.get_json().get("ok") and q1("SELECT name FROM area_boundaries WHERE id=?", bid)["name"] == "Renamed",
      "client custom area still renames")
http.post(f"/clients/{cid}/regions/{link['id']}/delete")
check(q1("SELECT COUNT(*) n FROM client_regions WHERE id=?", link["id"])["n"] == 0, "client area still removes")
check(http.get("/clients/regions/all").status_code == 200, "dashboard coverage endpoint unchanged")

# ── Permissions & scoping ────────────────────────────────────────────────────
office = q1("SELECT id FROM users WHERE role='user' AND is_active=1 AND id NOT IN "
            "(SELECT user_id FROM user_permissions WHERE module='leads') ORDER BY id")
if office:
    login(office["id"])
    check(http.get("/leads/").status_code == 403, "user without leads permission is refused")
    check(b"/leads/" not in http.get("/").data, "…and has no Leads menu item")
sales = q1("SELECT id FROM users WHERE role='sales' AND is_active=1 ORDER BY id")
if sales:
    db.execute("INSERT OR REPLACE INTO user_permissions (user_id, module, can_view) VALUES (?, 'leads', 1)", (sales["id"],))
    db.execute("UPDATE leads SET assigned_to=? WHERE id=?", (sales["id"], c))
    db.execute("UPDATE leads SET assigned_to=NULL WHERE id=?", (b_quick,))
    db.commit()
    login(sales["id"])
    html = http.get("/leads/").data.decode()
    check("Offshore Ltd" in html and "Alpha Traders" not in html, "sales manager sees only their team's leads")
    check(http.get(f"/leads/{a}").status_code == 403, "…and can't open another team's lead")
    check(http.post(f"/leads/{c}/status", json={"status": "closed"}).status_code == 403, "…view-only can't change status")
login(god)

# ── Users form lists the new module ──────────────────────────────────────────
html = http.get("/users/new").data.decode()
check(all(f'name="perm_leads_{a}"' in html for a in ("view", "create", "edit", "delete")),
      "user form has a Leads row with view / create / edit / delete")
target = q1("SELECT * FROM users WHERE role='user' AND is_active=1 ORDER BY id")
if target:
    form = {"name": target["name"], "email": target["email"], "role": "user", "is_active": "1",
            "chat_level": target["chat_level"] or "helper",
            **{f"perm_leads_{a}": "1" for a in ("view", "create", "edit", "delete")}}
    r = http.post(f"/users/{target['id']}/edit", data=form)
    p = q1("SELECT * FROM user_permissions WHERE user_id=? AND module='leads'", target["id"])
    check(r.status_code == 302 and p and all(p[f"can_{a}"] for a in ("view", "create", "edit", "delete")),
          "granting all four Leads permissions saves")
    html = http.get(f"/users/{target['id']}/edit").data.decode()
    check(all(re.search(rf'name="perm_leads_{a}"[^>]*\bchecked', html) for a in ("view", "create", "edit", "delete")),
          "…and shows ticked when the user is reopened")

# ── Deletes ──────────────────────────────────────────────────────────────────
http.post(f"/leads/segments/{seg['id']}/delete")
check(q1("SELECT COUNT(*) n FROM lead_segment_links WHERE segment_id=?", seg["id"])["n"] == 0,
      "deleting a segment untags its leads")
http.post(f"/leads/{c}/delete")
check(q1("SELECT COUNT(*) n FROM leads WHERE id=?", c)["n"] == 0
      and q1("SELECT COUNT(*) n FROM lead_regions WHERE lead_id=?", c)["n"] == 0
      and q1("SELECT COUNT(*) n FROM lead_activities WHERE lead_id=?", c)["n"] == 0,
      "deleting a lead removes its areas and timeline")

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)

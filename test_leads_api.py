"""Lead MCP tools end to end: each tool in mcp_server.py is called through the
same in-process transport the assistant uses, so this covers the MCP wrapper,
the /api/leads routes and the permission map together.

Place search is stubbed (no network): it answers "Delhi" with a US town first
and the Indian city second, so the India-first matching is exercised too.

Runs on a snapshot copy of data/ledger.db; the real file is never written.
Run:  python test_leads_api.py
"""
import os
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

flask_app = app_pkg.create_app()
flask_app.config["TESTING"] = True

import mcp_server                                        # noqa: E402
from app.chat import tools as chat_tools                 # noqa: E402
from app.chat.policy import TOOL_POLICY                  # noqa: E402
from app.services import geocoding_service               # noqa: E402

db = sqlite3.connect(tmp)
db.row_factory = sqlite3.Row
q1 = lambda sql, *a: db.execute(sql, a).fetchone()

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except chat_tools.ToolError as e:
        return str(e)
    return None


# A cached Nominatim boundary, so resolving it needs no network.
delhi = q1("SELECT id, source_ref, name FROM area_boundaries WHERE source='nominatim' AND name LIKE 'Delhi%India' "
           "ORDER BY id LIMIT 1") or q1("SELECT id, source_ref, name FROM area_boundaries "
                                         "WHERE source='nominatim' ORDER BY id LIMIT 1")
_, osm_type, osm_id = delhi["source_ref"].split(":")
geocoding_service.search_places = lambda query, limit=8: [
    {"osm_type": "relation", "osm_id": 999999999, "name": "Delhi, Delaware County, Iowa, United States",
     "type": "administrative", "lat": 42.4, "lon": -91.3},
    {"osm_type": osm_type, "osm_id": int(osm_id), "name": delhi["name"], "type": "administrative",
     "lat": 28.6, "lon": 77.2},
]
clients_there = {r["client_id"] for r in db.execute(
    "SELECT client_id FROM client_regions WHERE boundary_id=?", (delhi["id"],))}

lead_tools = ["list_leads", "get_lead", "create_lead", "update_lead", "set_lead_status",
              "add_lead_note", "delete_lead", "list_lead_segments", "create_lead_segment"]

with flask_app.app_context():
    mcp_server.set_transport(chat_tools.InProcessTransport())

    # ── Catalogue & permissions ──────────────────────────────────────────────
    cat = chat_tools.catalogue()
    check(all(t in cat for t in lead_tools), "every lead tool is in the MCP catalogue")
    check(chat_tools.disabled_reason() is None, "assistant startup check passes (all tools mapped)")
    check(all(TOOL_POLICY[t][0] == "leads" for t in lead_tools), "lead tools are gated on the leads module")
    check(TOOL_POLICY["delete_lead"] == ("leads", "delete") and TOOL_POLICY["create_lead"] == ("leads", "create"),
          "delete/create need the matching action")

    rep = q1("SELECT id, name FROM users WHERE is_active=1 AND role != 'god' ORDER BY id")
    category = q1("SELECT id, name FROM categories ORDER BY id")

    # ── Create ───────────────────────────────────────────────────────────────
    r = mcp_server.create_lead(
        company_name="API Hardware", contact_person="Vikas", phone="+91 99887 66554", city="Delhi",
        state="Delhi", source="Expo API", assigned_to=rep["name"].strip().lower(),
        next_follow_up="2026-11-05", categories=[category["name"].upper(), "No Such Category"],
        segments=["API dealers"], areas=["Delhi"])
    lead_id = r["lead_id"]
    row = q1("SELECT * FROM leads WHERE id=?", lead_id)
    check(row["company_name"] == "API Hardware" and row["assigned_to"] == rep["id"], "create_lead saves, assignee by name")
    check(row["created_by"] is None, "API-created lead has no signed-in author")
    check(r["areas_added"] and r["areas_added"][0]["matched"] == delhi["name"], "area by name prefers the Indian match")
    check(any("No category named" in w for w in r["warnings"]), "unknown category is reported, not created")
    check(any("New segment created" in w for w in r["warnings"]), "new segment is created and reported")
    check(clients_there <= {o["id"] for o in r["overlaps"] if o["kind"] == "client"},
          "create result lists overlapping clients")
    check(q1("SELECT COUNT(*) n FROM lead_categories WHERE lead_id=? AND category_id=?", lead_id, category["id"])["n"] == 1,
          "category matched by name (any case)")
    check(raises(mcp_server.create_lead, company_name="  ") is not None, "blank company refused")
    check("Unknown status" in (raises(mcp_server.create_lead, company_name="X", status="won") or ""),
          "unknown status refused with the valid list")
    check("No active user" in (raises(mcp_server.create_lead, company_name="X", assigned_to="Nobody Here") or ""),
          "unknown assignee refused")
    check(raises(mcp_server.create_lead, company_name="X", next_follow_up="05/11/2026") is not None,
          "badly formatted date refused")

    # ── Read ─────────────────────────────────────────────────────────────────
    g = mcp_server.get_lead(lead_id)
    check(g["status"] == "need_to_talk" and g["status_label"] == "Need to talk", "get_lead returns status")
    check([c["name"] for c in g["interests"]["categories"]] == [category["name"]]
          and [s_["name"] for s_ in g["interests"]["segments"]] == ["API dealers"], "get_lead returns interests")
    check(len(g["areas"]) == 1 and g["areas"][0]["link_id"], "get_lead returns areas with link ids")
    check(g["timeline"][0]["kind"] == "created" and g["timeline"][0]["by"] is None, "timeline has the creation")
    check(raises(mcp_server.get_lead, 987654) is not None, "missing lead is an error")

    L = mcp_server.list_leads(search="99887 66554")
    check([i["id"] for i in L["items"]] == [lead_id], "list_leads finds by phone (spaces ignored)")
    L = mcp_server.list_leads(status="need_to_talk,rates_given", assigned_to=rep["name"], include_overlaps=True)
    check(lead_id in [i["id"] for i in L["items"]] and L["status_counts"].get("need_to_talk"), "status + assignee filters")
    item = next(i for i in L["items"] if i["id"] == lead_id)
    check(set(item["overlaps"]["clients"]) >= {q1("SELECT name FROM clients WHERE id=?", c)["name"] for c in clients_there},
          "include_overlaps adds overlap names")
    check(lead_id not in [i["id"] for i in mcp_server.list_leads(follow_up="none")["items"]], "follow_up=none excludes it")
    check(lead_id in [i["id"] for i in mcp_server.list_leads(category_id=category["id"])["items"]], "category filter")

    # ── Update (partial) ─────────────────────────────────────────────────────
    r = mcp_server.update_lead(lead_id, contact_person="Vikas Jain", categories=[])
    row = q1("SELECT * FROM leads WHERE id=?", lead_id)
    check(row["contact_person"] == "Vikas Jain" and row["phone"] == "+91 99887 66554"
          and row["assigned_to"] == rep["id"] and row["next_follow_up"] == "2026-11-05",
          "update_lead changes only what was given")
    check(q1("SELECT COUNT(*) n FROM lead_categories WHERE lead_id=?", lead_id)["n"] == 0
          and q1("SELECT COUNT(*) n FROM lead_segment_links WHERE lead_id=?", lead_id)["n"] == 1,
          "giving categories replaces them; segments left alone")
    mcp_server.update_lead(lead_id, email="")
    check(q1("SELECT email FROM leads WHERE id=?", lead_id)["email"] is None, "empty string clears a field")
    mcp_server.update_lead(lead_id, status="rates_given")
    check(q1("SELECT kind, new_status FROM lead_activities WHERE lead_id=? ORDER BY id DESC", lead_id)["new_status"]
          == "rates_given", "status change via update is on the timeline")
    check(raises(mcp_server.update_lead, lead_id, status="bogus") is not None, "update refuses an unknown status")

    # ── Status / notes ───────────────────────────────────────────────────────
    r = mcp_server.set_lead_status(lead_id, "samples_sent", note="3 sample discs couriered")
    a = q1("SELECT * FROM lead_activities WHERE lead_id=? ORDER BY id DESC", lead_id)
    check(a["new_status"] == "samples_sent" and a["body"] == "3 sample discs couriered", "set_lead_status with note")
    mcp_server.add_lead_note(lead_id, "Asked for 5% more discount")
    check(q1("SELECT body FROM lead_activities WHERE lead_id=? ORDER BY id DESC", lead_id)["body"]
          == "Asked for 5% more discount", "add_lead_note")
    check(raises(mcp_server.add_lead_note, lead_id, "   ") is not None, "empty note refused")

    # ── Areas through update ─────────────────────────────────────────────────
    link = g["areas"][0]["link_id"]
    r = mcp_server.update_lead(lead_id, remove_area_ids=[link])
    check(r["areas_removed"] == [link] and r["overlaps"] == [], "remove_area_ids drops the area and its overlaps")
    r = mcp_server.update_lead(lead_id, add_areas=["Delhi"])
    check(len(r["areas_added"]) == 1 and r["overlaps"], "add_areas puts it back")

    # ── Segments ─────────────────────────────────────────────────────────────
    seg = mcp_server.create_lead_segment("api DEALERS")
    check(not seg["created"], "create_lead_segment reuses an existing name")
    names = [x["name"] for x in mcp_server.list_lead_segments()["items"]]
    check("API dealers" in names, "list_lead_segments")

    # ── Delete ───────────────────────────────────────────────────────────────
    r = mcp_server.delete_lead(lead_id)
    check("permanently deleted" in r["message"] and q1("SELECT COUNT(*) n FROM leads WHERE id=?", lead_id)["n"] == 0,
          "delete_lead removes it")
    check(q1("SELECT COUNT(*) n FROM lead_activities WHERE lead_id=?", lead_id)["n"] == 0, "…with its timeline")

    # ── Auth ─────────────────────────────────────────────────────────────────
    http = flask_app.test_client()
    check(http.get("/api/leads").status_code == 401, "API refuses calls without the key")

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)

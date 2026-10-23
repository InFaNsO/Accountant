"""Sales leads: prospects (exhibition contacts and the like) tracked from first
conversation to first order, with what they're interested in, where they work
and a timeline of what happened.
"""
import re
from datetime import date

from ..database import get_db
from . import region_service
from .auth_service import get_team_user_ids

# (key, label, text colour, badge background, text colour in dark mode). The
# order is the order shown everywhere — the pipeline as the team works it.
STATUSES = [
    ("need_to_talk",     "Need to talk",     "#B45309", "rgba(245,158,11,.16)", "#FCD34D"),
    ("rates_given",      "Rates given",      "#1D4ED8", "rgba(59,130,246,.15)", "#93C5FD"),
    ("samples_sent",     "Samples sent",     "#6D28D9", "rgba(139,92,246,.15)", "#C4B5FD"),
    ("other",            "Other",            "#475569", "rgba(100,116,139,.16)", "#CBD5E1"),
    ("first_order_sent", "First order sent", "#0F766E", "rgba(20,184,166,.16)", "#5EEAD4"),
    ("closed",           "Closed",           "#047857", "rgba(16,185,129,.16)", "#6EE7B7"),
    ("abandoned",        "Abandoned",        "#B91C1C", "rgba(239,68,68,.13)", "#FCA5A5"),
]
STATUS_MAP = {k: {"key": k, "label": l, "color": c, "bg": b, "dark": d} for k, l, c, b, d in STATUSES}
DEFAULT_STATUS = "need_to_talk"
# Statuses that still need someone to act — follow-ups only count for these.
OPEN_STATUSES = {"need_to_talk", "rates_given", "samples_sent", "other", "first_order_sent"}

_FIELDS = ("company_name", "contact_person", "phone", "alt_phone", "email",
           "city", "state", "address", "source", "notes")


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_phone(phone):
    """Digits only, without the +91 / leading-0 trunk prefix, so
    '+91 98765-43210', '098765 43210' and '9876543210' all compare equal."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def _clean(data):
    """Form/JSON dict -> column values. Empty strings become NULL."""
    out = {f: ((data.get(f) or "").strip() or None) for f in _FIELDS}
    status = data.get("status") or DEFAULT_STATUS
    out["status"] = status if status in STATUS_MAP else DEFAULT_STATUS
    follow = (data.get("next_follow_up") or "").strip()
    try:
        out["next_follow_up"] = date.fromisoformat(follow).isoformat() if follow else None
    except ValueError:
        out["next_follow_up"] = None
    try:
        out["assigned_to"] = int(data.get("assigned_to")) if data.get("assigned_to") else None
    except (TypeError, ValueError):
        out["assigned_to"] = None
    return out


def parse_ids(values):
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return sorted(set(out))


def _log(db, lead_id, user_id, kind, body=None, old_status=None, new_status=None):
    db.execute(
        """INSERT INTO lead_activities (lead_id, user_id, kind, body, old_status, new_status)
           VALUES (?,?,?,?,?,?)""",
        (lead_id, user_id, kind, body, old_status, new_status),
    )


def _set_interests(db, lead_id, interests):
    """interests: {"category_ids": [...], "product_ids": [...], "segment_ids": [...]};
    replaces whatever was there."""
    for table, col, key in (("lead_categories", "category_id", "category_ids"),
                            ("lead_products", "product_id", "product_ids"),
                            ("lead_segment_links", "segment_id", "segment_ids")):
        db.execute(f"DELETE FROM {table} WHERE lead_id=?", (lead_id,))
        for ref in parse_ids(interests.get(key)):
            db.execute(f"INSERT OR IGNORE INTO {table} (lead_id, {col}) VALUES (?,?)", (lead_id, ref))


# ── Scope (sales managers see their team's leads) ─────────────────────────────

def visible_lead_ids(user):
    """Lead ids a sales-role user may see — assigned to, or added by, them or
    their staff — or None for unrestricted (same rule as clients)."""
    if getattr(user, "role", None) != "sales":
        return None
    team = get_team_user_ids(user.id)
    ph = ",".join("?" * len(team))
    rows = get_db().execute(
        f"SELECT id FROM leads WHERE assigned_to IN ({ph}) OR created_by IN ({ph})",
        team + team,
    ).fetchall()
    return {r["id"] for r in rows}


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_leads(lead_ids=None):
    """Every lead (or just lead_ids) with what the list page shows: who it's
    assigned to, its areas and its interests, newest first."""
    if lead_ids is not None and not lead_ids:
        return []
    where, params = "", []
    if lead_ids is not None:
        where = f"WHERE l.id IN ({','.join('?' * len(lead_ids))})"
        params = list(lead_ids)
    db = get_db()
    rows = db.execute(
        f"""SELECT l.*, u.name AS assigned_name, c.name AS client_name,
                   (SELECT COUNT(*) FROM lead_regions lr WHERE lr.lead_id = l.id) AS region_count,
                   (SELECT GROUP_CONCAT(ab.name, '|') FROM lead_regions lr
                      JOIN area_boundaries ab ON ab.id = lr.boundary_id
                     WHERE lr.lead_id = l.id) AS region_names
            FROM leads l
            LEFT JOIN users u   ON u.id = l.assigned_to
            LEFT JOIN clients c ON c.id = l.client_id
            {where}
            ORDER BY l.created_at DESC, l.id DESC""",
        params,
    ).fetchall()
    interests = _interests_by_lead(db)
    out = []
    for r in rows:
        d = dict(r)
        d["region_names"] = [n for n in (r["region_names"] or "").split("|") if n]
        d["interests"] = interests.get(r["id"], [])
        out.append(d)
    return out


def _interests_by_lead(db):
    """{lead_id: [{"kind", "id", "name"}, ...]} — categories, then segments,
    then products, each alphabetical."""
    out = {}
    queries = (
        ("category", """SELECT lc.lead_id, c.id, c.name FROM lead_categories lc
                         JOIN categories c ON c.id = lc.category_id ORDER BY c.name"""),
        ("segment",  """SELECT ls.lead_id, s.id, s.name FROM lead_segment_links ls
                         JOIN lead_segments s ON s.id = ls.segment_id ORDER BY s.name"""),
        ("product",  """SELECT lp.lead_id, p.id, p.name FROM lead_products lp
                         JOIN products p ON p.id = lp.product_id ORDER BY p.name"""),
    )
    for kind, sql in queries:
        for lead_id, ref_id, name in db.execute(sql):
            out.setdefault(lead_id, []).append({"kind": kind, "id": ref_id, "name": name})
    return out


# Timestamps are stored as SQLite CURRENT_TIMESTAMP (UTC); the team reads them in IST.
_IST = "'+330 minutes'"


def get_lead(lead_id):
    return get_db().execute(
        f"""SELECT l.*, u.name AS assigned_name, cb.name AS created_by_name, c.name AS client_name,
                   datetime(l.created_at, {_IST}) AS created_local,
                   datetime(l.status_changed_at, {_IST}) AS status_changed_local,
                   (SELECT COUNT(*) FROM lead_regions lr WHERE lr.lead_id = l.id) AS region_count
           FROM leads l
           LEFT JOIN users u   ON u.id  = l.assigned_to
           LEFT JOIN users cb  ON cb.id = l.created_by
           LEFT JOIN clients c ON c.id  = l.client_id
           WHERE l.id=?""",
        (lead_id,),
    ).fetchone()


def get_interests(lead_id):
    """{"categories": [...], "segments": [...], "products": [...]} rows for one lead."""
    db = get_db()
    return {
        "categories": db.execute(
            """SELECT c.id, c.name FROM lead_categories lc JOIN categories c ON c.id = lc.category_id
               WHERE lc.lead_id=? ORDER BY c.name""", (lead_id,)).fetchall(),
        "segments": db.execute(
            """SELECT s.id, s.name FROM lead_segment_links ls JOIN lead_segments s ON s.id = ls.segment_id
               WHERE ls.lead_id=? ORDER BY s.name""", (lead_id,)).fetchall(),
        "products": db.execute(
            """SELECT p.id, p.name, c.name AS category_name FROM lead_products lp
               JOIN products p ON p.id = lp.product_id
               LEFT JOIN categories c ON c.id = p.category_id
               WHERE lp.lead_id=? ORDER BY c.name, p.name""", (lead_id,)).fetchall(),
    }


def get_interest_ids(lead_id):
    i = get_interests(lead_id)
    return {"category_ids": [r["id"] for r in i["categories"]],
            "segment_ids":  [r["id"] for r in i["segments"]],
            "product_ids":  [r["id"] for r in i["products"]]}


def get_activities(lead_id):
    return get_db().execute(
        f"""SELECT a.*, u.name AS user_name, datetime(a.created_at, {_IST}) AS created_local
           FROM lead_activities a
           LEFT JOIN users u ON u.id = a.user_id
           WHERE a.lead_id=? ORDER BY a.created_at DESC, a.id DESC""",
        (lead_id,),
    ).fetchall()


def get_sources():
    """Sources already used, most recent first — offered as suggestions so one
    exhibition isn't typed five different ways."""
    rows = get_db().execute(
        """SELECT source FROM leads WHERE source IS NOT NULL AND source != ''
           GROUP BY source ORDER BY MAX(created_at) DESC LIMIT 30"""
    ).fetchall()
    return [r["source"] for r in rows]


def get_assignable_users():
    return get_db().execute(
        "SELECT id, name FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()


# ── Duplicates ────────────────────────────────────────────────────────────────

def find_duplicates(phone=None, company=None, exclude_lead_id=None, alt_phone=None):
    """Existing clients and leads that look like the same business: a phone
    number in common (any format), or the same company name. A warning, not a
    block — two branches can share a number."""
    phones = {p for p in (normalize_phone(phone), normalize_phone(alt_phone)) if len(p) >= 6}
    name = (company or "").strip().lower()
    if not phones and not name:
        return []
    db = get_db()
    out, seen = [], set()

    def add(kind, eid, label, match):
        if (kind, eid, match) in seen:
            return
        seen.add((kind, eid, match))
        out.append({"kind": kind, "id": eid, "name": label, "match": match})

    for r in db.execute("SELECT id, company_name, phone, alt_phone FROM leads"):
        if r["id"] == exclude_lead_id:
            continue
        if phones & {normalize_phone(r["phone"]), normalize_phone(r["alt_phone"])}:
            add("lead", r["id"], r["company_name"], "phone")
        if name and (r["company_name"] or "").strip().lower() == name:
            add("lead", r["id"], r["company_name"], "company")
    for r in db.execute("SELECT id, name, phone FROM clients"):
        if phones and normalize_phone(r["phone"]) in phones:
            add("client", r["id"], r["name"], "phone")
        if name and (r["name"] or "").strip().lower() == name:
            add("client", r["id"], r["name"], "company")
    for r in db.execute(
            """SELECT cc.client_id, cc.name AS company, cc.phone, c.name AS client_name
               FROM client_companies cc JOIN clients c ON c.id = cc.client_id"""):
        if phones and normalize_phone(r["phone"]) in phones:
            add("client", r["client_id"], r["client_name"], "phone")
        if name and (r["company"] or "").strip().lower() == name:
            add("client", r["client_id"], r["client_name"], "company")
    return out


# ── Writes ────────────────────────────────────────────────────────────────────

def create_lead(data, interests, user_id):
    db = get_db()
    v = _clean(data)
    cur = db.execute(
        """INSERT INTO leads (company_name, contact_person, phone, alt_phone, email, city, state,
                              address, source, notes, status, next_follow_up, assigned_to, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (v["company_name"], v["contact_person"], v["phone"], v["alt_phone"], v["email"],
         v["city"], v["state"], v["address"], v["source"], v["notes"], v["status"],
         v["next_follow_up"], v["assigned_to"], user_id),
    )
    lead_id = cur.lastrowid
    _set_interests(db, lead_id, interests)
    _log(db, lead_id, user_id, "created",
         body=f"From {v['source']}" if v["source"] else None, new_status=v["status"])
    db.commit()
    return lead_id


def update_lead(lead_id, data, interests, user_id):
    db = get_db()
    old = db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not old:
        return False
    v = _clean(data)
    db.execute(
        """UPDATE leads SET company_name=?, contact_person=?, phone=?, alt_phone=?, email=?,
                            city=?, state=?, address=?, source=?, notes=?, next_follow_up=?,
                            assigned_to=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (v["company_name"], v["contact_person"], v["phone"], v["alt_phone"], v["email"],
         v["city"], v["state"], v["address"], v["source"], v["notes"], v["next_follow_up"],
         v["assigned_to"], lead_id),
    )
    _set_interests(db, lead_id, interests)
    db.commit()
    if v["status"] != old["status"]:
        set_status(lead_id, v["status"], user_id)
    return True


def patch_lead(lead_id, changes, interests=None, user_id=None):
    """Change only the fields given (the API and assistant send what the user
    asked to change, not the whole form). interests: any of category_ids /
    product_ids / segment_ids replaces that kind; kinds left out are kept."""
    row = get_lead(lead_id)
    if not row:
        return False
    merged = {f: row[f] for f in _FIELDS + ("status", "next_follow_up", "assigned_to")}
    merged.update({k: v for k, v in changes.items() if k in merged})
    current = get_interest_ids(lead_id)
    current.update({k: v for k, v in (interests or {}).items() if v is not None})
    return update_lead(lead_id, merged, current, user_id)


def set_status(lead_id, status, user_id, note=None):
    """Move a lead to another status, logging it on the timeline. Returns False
    for an unknown lead or status; a no-op (still True) if nothing changed."""
    if status not in STATUS_MAP:
        return False
    db = get_db()
    row = db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        return False
    note = (note or "").strip() or None
    if row["status"] == status and not note:
        return True
    if row["status"] != status:
        db.execute(
            """UPDATE leads SET status=?, status_changed_at=CURRENT_TIMESTAMP,
                                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, lead_id),
        )
        _log(db, lead_id, user_id, "status", body=note, old_status=row["status"], new_status=status)
    else:
        _log(db, lead_id, user_id, "note", body=note)
    db.commit()
    return True


def set_follow_up(lead_id, follow_up):
    """follow_up: 'YYYY-MM-DD' or empty to clear."""
    value = None
    if follow_up:
        try:
            value = date.fromisoformat(follow_up).isoformat()
        except ValueError:
            return False
    db = get_db()
    cur = db.execute(
        "UPDATE leads SET next_follow_up=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (value, lead_id))
    db.commit()
    return cur.rowcount > 0


def add_note(lead_id, user_id, body):
    body = (body or "").strip()
    if not body:
        return False
    db = get_db()
    _log(db, lead_id, user_id, "note", body=body[:4000])
    db.execute("UPDATE leads SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (lead_id,))
    db.commit()
    return True


def delete_note(lead_id, activity_id):
    """Only notes can be removed — status changes and conversion are history."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM lead_activities WHERE id=? AND lead_id=? AND kind='note'", (activity_id, lead_id))
    db.commit()
    return cur.rowcount > 0


def delete_lead(lead_id):
    db = get_db()
    db.execute("DELETE FROM leads WHERE id=?", (lead_id,))
    db.commit()


def mark_converted(lead_id, client_id, user_id):
    """Link a lead to the client made from it and give that client the lead's
    areas of work."""
    db = get_db()
    lead = db.execute("SELECT client_id FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return False
    db.execute("UPDATE leads SET client_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (client_id, lead_id))
    client = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    _log(db, lead_id, user_id, "converted",
         body=f"Converted to client {client['name']}" if client else "Converted to client")
    db.commit()
    region_service.copy_regions("lead", lead_id, "client", client_id)
    return True


# ── Segments ──────────────────────────────────────────────────────────────────

def get_segments():
    return get_db().execute(
        """SELECT s.id, s.name,
                  (SELECT COUNT(*) FROM lead_segment_links l WHERE l.segment_id = s.id) AS lead_count
           FROM lead_segments s ORDER BY s.name COLLATE NOCASE"""
    ).fetchall()


def create_segment(name):
    """Returns (id, created). A name that already exists (any case) returns
    the existing segment rather than a near-duplicate."""
    name = " ".join((name or "").split())[:80]
    if not name:
        return None, False
    db = get_db()
    row = db.execute("SELECT id FROM lead_segments WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"], False
    cur = db.execute("INSERT INTO lead_segments (name) VALUES (?)", (name,))
    db.commit()
    return cur.lastrowid, True


def rename_segment(segment_id, name):
    name = " ".join((name or "").split())[:80]
    if not name:
        return False
    db = get_db()
    clash = db.execute("SELECT id FROM lead_segments WHERE name=? COLLATE NOCASE AND id != ?",
                       (name, segment_id)).fetchone()
    if clash:
        return False
    cur = db.execute("UPDATE lead_segments SET name=? WHERE id=?", (name, segment_id))
    db.commit()
    return cur.rowcount > 0


def delete_segment(segment_id):
    db = get_db()
    db.execute("DELETE FROM lead_segments WHERE id=?", (segment_id,))
    db.commit()

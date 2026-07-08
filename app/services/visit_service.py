"""Field-visit tracking: GPS check-ins by sales staff at client locations.

Timestamps are stored in UTC (SQLite CURRENT_TIMESTAMP). The business runs on
IST, so queries expose an `ist` column shifted by +5:30 for display and all
date filtering happens on the IST date.
"""
from ..database import get_db

# SQLite datetime modifiers for UTC → IST
_IST = "'+5 hours', '+30 minutes'"

PURPOSES = ["sales_call", "delivery", "collection", "follow_up", "other"]
OUTCOMES = ["order_placed", "follow_up", "no_order", "payment_collected", "other"]

# Coverage thresholds for the map pins (days since last visit)
COVERAGE_RECENT_DAYS = 30


def create_visit(user_id, data):
    """Create a check-in. data: client_id OR prospect_name, latitude, longitude,
    accuracy_m, purpose, outcome, notes. Returns new visit id."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO client_visits
               (user_id, client_id, prospect_name, latitude, longitude,
                accuracy_m, purpose, outcome, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            user_id,
            data.get("client_id"),
            (data.get("prospect_name") or "").strip() or None,
            float(data["latitude"]),
            float(data["longitude"]),
            float(data["accuracy_m"]) if data.get("accuracy_m") not in (None, "") else None,
            data.get("purpose"),
            data.get("outcome"),
            (data.get("notes") or "").strip() or None,
        ),
    )
    db.commit()
    return cur.lastrowid


def check_out(visit_id, user_id, is_god=False):
    """Stamp checked_out_at on the user's own open visit. Returns True if updated."""
    db = get_db()
    if is_god:
        cur = db.execute(
            "UPDATE client_visits SET checked_out_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND checked_out_at IS NULL",
            (visit_id,),
        )
    else:
        cur = db.execute(
            "UPDATE client_visits SET checked_out_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND user_id=? AND checked_out_at IS NULL",
            (visit_id, user_id),
        )
    db.commit()
    return cur.rowcount > 0


def update_outcome(visit_id, user_id, outcome, notes=None, is_god=False):
    """Let staff set the outcome when leaving (check-out screen)."""
    db = get_db()
    params = [outcome]
    sql = "UPDATE client_visits SET outcome=?"
    if notes is not None:
        sql += ", notes=?"
        params.append(notes.strip() or None)
    sql += " WHERE id=?"
    params.append(visit_id)
    if not is_god:
        sql += " AND user_id=?"
        params.append(user_id)
    cur = db.execute(sql, params)
    db.commit()
    return cur.rowcount > 0


def get_visits(user_id=None, client_id=None, date_from=None, date_to=None,
               state=None, limit=1000, user_ids=None):
    """Visits joined with staff + client info, newest first. Date filters are
    inclusive and applied on the IST calendar date (YYYY-MM-DD strings).
    user_ids (list) restricts to a set of staff — used to scope a sales
    manager to their own team."""
    sql = f"""
        SELECT v.*,
               datetime(v.checked_in_at,  {_IST}) AS checked_in_ist,
               datetime(v.checked_out_at, {_IST}) AS checked_out_ist,
               date(v.checked_in_at, {_IST})      AS visit_date_ist,
               u.name AS user_name,
               c.name AS client_name, c.city AS client_city, c.state AS client_state
        FROM client_visits v
        JOIN users  u ON u.id = v.user_id
        LEFT JOIN clients c ON c.id = v.client_id
        WHERE 1=1
    """
    params = []
    if user_ids is not None:
        if not user_ids:
            return []
        ph = ",".join("?" * len(user_ids))
        sql += f" AND v.user_id IN ({ph})"
        params.extend(user_ids)
    if user_id:
        sql += " AND v.user_id=?"
        params.append(user_id)
    if client_id:
        sql += " AND v.client_id=?"
        params.append(client_id)
    if date_from:
        sql += f" AND date(v.checked_in_at, {_IST}) >= ?"
        params.append(date_from)
    if date_to:
        sql += f" AND date(v.checked_in_at, {_IST}) <= ?"
        params.append(date_to)
    if state:
        sql += " AND c.state=?"
        params.append(state)
    sql += " ORDER BY v.checked_in_at DESC LIMIT ?"
    params.append(limit)
    return get_db().execute(sql, params).fetchall()


def get_client_visits(client_id, limit=50):
    """Visit history for one client's detail page."""
    return get_db().execute(
        f"""SELECT v.*,
                   datetime(v.checked_in_at,  {_IST}) AS checked_in_ist,
                   datetime(v.checked_out_at, {_IST}) AS checked_out_ist,
                   u.name AS user_name
            FROM client_visits v
            JOIN users u ON u.id = v.user_id
            WHERE v.client_id=?
            ORDER BY v.checked_in_at DESC LIMIT ?""",
        (client_id, limit),
    ).fetchall()


def get_open_visit(user_id):
    """The user's most recent visit today (IST) without a check-out, if any."""
    return get_db().execute(
        f"""SELECT v.*, c.name AS client_name,
                   datetime(v.checked_in_at, {_IST}) AS checked_in_ist
            FROM client_visits v
            LEFT JOIN clients c ON c.id = v.client_id
            WHERE v.user_id=? AND v.checked_out_at IS NULL
              AND date(v.checked_in_at, {_IST}) = date('now', {_IST})
            ORDER BY v.checked_in_at DESC LIMIT 1""",
        (user_id,),
    ).fetchone()


def get_my_visits_today(user_id):
    """Today's (IST) check-ins for the staff member's own list."""
    return get_db().execute(
        f"""SELECT v.*, c.name AS client_name,
                   datetime(v.checked_in_at,  {_IST}) AS checked_in_ist,
                   datetime(v.checked_out_at, {_IST}) AS checked_out_ist
            FROM client_visits v
            LEFT JOIN clients c ON c.id = v.client_id
            WHERE v.user_id=? AND date(v.checked_in_at, {_IST}) = date('now', {_IST})
            ORDER BY v.checked_in_at DESC""",
        (user_id,),
    ).fetchall()


def get_clients_geo(client_ids=None):
    """Client pins for the map with coverage status.
    status: 'recent' (visited within COVERAGE_RECENT_DAYS), 'stale', 'never'.
    client_ids (set/list) restricts to a scope — used for sales managers."""
    where = ""
    params = []
    if client_ids is not None:
        if not client_ids:
            return []
        ph = ",".join("?" * len(client_ids))
        where = f"WHERE c.id IN ({ph})"
        params = list(client_ids)
    rows = get_db().execute(
        f"""SELECT c.id, c.name, c.city, c.state, c.latitude, c.longitude,
                   (SELECT MAX(date(v.checked_in_at, {_IST}))
                      FROM client_visits v WHERE v.client_id = c.id) AS last_visit,
                   (SELECT CAST(julianday(date('now', {_IST})) -
                                julianday(MAX(date(v.checked_in_at, {_IST}))) AS INTEGER)
                      FROM client_visits v WHERE v.client_id = c.id) AS days_since
            FROM clients c
            {where}
            ORDER BY c.name""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["days_since"] is None:
            d["status"] = "never"
        elif d["days_since"] <= COVERAGE_RECENT_DAYS:
            d["status"] = "recent"
        else:
            d["status"] = "stale"
        out.append(d)
    return out


def get_checkin_clients():
    """Minimal client list for the staff check-in screen (no financials)."""
    return get_db().execute(
        "SELECT id, name, city, state, latitude, longitude, sales_rep_id "
        "FROM clients ORDER BY name"
    ).fetchall()

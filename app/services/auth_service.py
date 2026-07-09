from functools import wraps
from flask import abort, redirect, url_for, flash
from flask_login import UserMixin, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from ..database import get_db

# All controllable modules
MODULES = [
    "clients",
    "invoices",
    "payments",
    "products",
    "suppliers",
    "production",
    "transit",
    "palm_purchase",
    "visits",
]

# Clients-only extra permissions, shown as sub-lines under the Clients row in the
# user form. "actions" maps a grid column (view/create/edit/delete) to the
# permission flag stored as user_permissions.can_<flag>.
CLIENT_EXTRAS = [
    {"key": "financials", "label": "Financials",
     "actions": {"view": "financials"},
     "desc": "Balance, ledger & payment status visibility"},
    {"key": "locks", "label": "Invoice Locks",
     "actions": {"view": "locks_view", "edit": "locks_edit"},
     "desc": "View lock status / toggle the tally lock and edit balance-lock settings"},
]
# Flat list of the extra permission flags: ["financials", "locks_view", "locks_edit"]
CLIENT_EXTRA_FLAGS = [flag for e in CLIENT_EXTRAS for flag in e["actions"].values()]

# Dashboard sections (key, display label)
DASHBOARD_SECTIONS = [
    ("stat_revenue",     "Total Revenue"),
    ("stat_outstanding", "Outstanding"),
    ("stat_clients",     "Active Clients"),
    ("stat_overdue",     "Overdue Count"),
    ("revenue_chart",    "Revenue Chart"),
    ("recent_invoices",  "Recent Invoices"),
    ("production_due",   "Production Due Soon"),
    ("low_stock",        "Low Stock Alerts"),
    ("in_production",    "In Production"),
    ("in_transit",       "In Transit"),
    ("coverage_map",     "Client Coverage Map"),
]
_ALL_DASH_KEYS = {k for k, _ in DASHBOARD_SECTIONS}


class User(UserMixin):
    def __init__(self, row):
        self.id            = row["id"]
        self.name          = row["name"]
        self.email         = row["email"]
        self.password_hash = row["password_hash"]
        self.role          = row["role"]
        self.manager_id    = row["manager_id"]
        self._is_active    = bool(row["is_active"])

    @property
    def is_active(self):
        return self._is_active

    def is_god(self):
        return self.role == "god"

    def has_permission(self, module, action):
        """action: view | create | edit | delete | financials | locks_view | locks_edit
        (financials and locks_* apply to the clients module only)"""
        if self.is_god():
            return True
        db = get_db()
        row = db.execute(
            "SELECT * FROM user_permissions WHERE user_id=? AND module=?",
            (self.id, module),
        ).fetchone()
        if not row:
            return False
        try:
            return bool(row[f"can_{action}"])
        except IndexError:
            return False

    def get_all_permissions(self):
        """Return dict of {module: {view,create,edit,delete[,financials,locks_view,locks_edit]}}"""
        db = get_db()
        rows = db.execute(
            "SELECT * FROM user_permissions WHERE user_id=?", (self.id,)
        ).fetchall()
        perms = {m: {"view": False, "create": False, "edit": False, "delete": False}
                 for m in MODULES}
        for flag in CLIENT_EXTRA_FLAGS:
            perms["clients"][flag] = False
        for r in rows:
            if r["module"] in perms:
                perms[r["module"]] = {
                    "view":   bool(r["can_view"]),
                    "create": bool(r["can_create"]),
                    "edit":   bool(r["can_edit"]),
                    "delete": bool(r["can_delete"]),
                }
                if r["module"] == "clients":
                    for flag in CLIENT_EXTRA_FLAGS:
                        try:
                            perms["clients"][flag] = bool(r[f"can_{flag}"])
                        except IndexError:
                            perms["clients"][flag] = False
        return perms

    def has_dashboard_section(self, section):
        """Return True if user can see this dashboard section."""
        if self.is_god():
            return True
        db = get_db()
        row = db.execute(
            "SELECT 1 FROM user_dashboard_sections WHERE user_id=? AND section=?",
            (self.id, section),
        ).fetchone()
        return row is not None

    def get_dashboard_sections(self):
        """Return set of section keys this user can see."""
        if self.is_god():
            return _ALL_DASH_KEYS.copy()
        db = get_db()
        rows = db.execute(
            "SELECT section FROM user_dashboard_sections WHERE user_id=?", (self.id,)
        ).fetchall()
        return {r["section"] for r in rows}


# ── Loader ────────────────────────────────────────────────────────────────────

def load_user(user_id):
    row = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return User(row) if row else None


# ── Decorators ────────────────────────────────────────────────────────────────

def permission_required(module, action):
    """Decorator: require current_user has module+action permission."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login"))
            if not current_user.has_permission(module, action):
                flash("You don't have permission to do that.", "error")
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def god_required(f):
    """Decorator: only the god account may access this route."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if not current_user.is_god():
            flash("God account required.", "error")
            abort(403)
        return f(*args, **kwargs)
    return wrapped


# ── CRUD ──────────────────────────────────────────────────────────────────────

def get_all_users():
    return get_db().execute(
        "SELECT * FROM users ORDER BY role DESC, name"
    ).fetchall()


def get_user(user_id):
    return get_db().execute(
        "SELECT * FROM users WHERE id=?", (user_id,)
    ).fetchone()


def get_user_by_email(email):
    return get_db().execute(
        "SELECT * FROM users WHERE email=?", (email,)
    ).fetchone()


def create_user(data, permissions, dash_sections=None):
    """Create user and set per-module permissions. permissions = {module: {view,create,edit,delete}}"""
    db = get_db()
    pw_hash = generate_password_hash(data["password"])
    cur = db.execute(
        "INSERT INTO users (name, email, password_hash, role, is_active) VALUES (?,?,?,?,1)",
        (data["name"], data["email"], pw_hash, data.get("role", "user")),
    )
    user_id = cur.lastrowid
    _save_permissions(db, user_id, permissions)
    _save_dashboard_sections(db, user_id, dash_sections or [])
    db.commit()
    return user_id


def update_user(user_id, data, permissions, dash_sections=None):
    db = get_db()
    if data.get("password"):
        db.execute(
            "UPDATE users SET name=?, email=?, password_hash=?, role=?, is_active=? WHERE id=?",
            (data["name"], data["email"], generate_password_hash(data["password"]),
             data.get("role", "user"), 1 if data.get("is_active") else 0, user_id),
        )
    else:
        db.execute(
            "UPDATE users SET name=?, email=?, role=?, is_active=? WHERE id=?",
            (data["name"], data["email"], data.get("role", "user"),
             1 if data.get("is_active") else 0, user_id),
        )
    _save_permissions(db, user_id, permissions)
    _save_dashboard_sections(db, user_id, dash_sections or [])
    db.commit()


def delete_user(user_id):
    db = get_db()
    # Detach any staff who reported to this user, and release the clients they
    # managed (visits keep their user_id via ON DELETE SET NULL on the FK).
    db.execute("UPDATE users SET manager_id=NULL WHERE manager_id=?", (user_id,))
    db.execute("UPDATE clients SET sales_rep_id=NULL WHERE sales_rep_id=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()


def verify_password(email, password):
    row = get_user_by_email(email)
    if not row:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    if not row["is_active"]:
        return None
    return User(row)


def ensure_god_account(email, password, name="God"):
    """Create the god account if it doesn't exist yet."""
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        return
    pw_hash = generate_password_hash(password)
    db.execute(
        "INSERT INTO users (name, email, password_hash, role, is_active) VALUES (?,?,?,'god',1)",
        (name, email, pw_hash),
    )
    db.commit()


def _save_permissions(db, user_id, permissions):
    """Upsert permissions for every module."""
    for module, perms in permissions.items():
        db.execute(
            """INSERT INTO user_permissions
                   (user_id, module, can_view, can_create, can_edit, can_delete,
                    can_financials, can_locks_view, can_locks_edit)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, module) DO UPDATE SET
                   can_view=excluded.can_view,
                   can_create=excluded.can_create,
                   can_edit=excluded.can_edit,
                   can_delete=excluded.can_delete,
                   can_financials=excluded.can_financials,
                   can_locks_view=excluded.can_locks_view,
                   can_locks_edit=excluded.can_locks_edit""",
            (user_id, module,
             1 if perms.get("view")       else 0,
             1 if perms.get("create")     else 0,
             1 if perms.get("edit")       else 0,
             1 if perms.get("delete")     else 0,
             1 if perms.get("financials") else 0,
             1 if perms.get("locks_view") else 0,
             1 if perms.get("locks_edit") else 0),
        )


def _save_dashboard_sections(db, user_id, sections):
    """Replace dashboard section grants for a user."""
    db.execute("DELETE FROM user_dashboard_sections WHERE user_id=?", (user_id,))
    for section in sections:
        if section in _ALL_DASH_KEYS:
            db.execute(
                "INSERT OR IGNORE INTO user_dashboard_sections (user_id, section) VALUES (?,?)",
                (user_id, section),
            )


# ── Sales-manager role scoping ────────────────────────────────────────────────
# Users with role='sales' manage a team of staff (users.manager_id) and may only
# see data for clients assigned to themselves or their team.

# Modules a sales manager does not get by default (the god account can still
# grant them explicitly on the user form).
SALES_DEFAULT_DENIED_MODULES = ["products", "suppliers", "production", "transit", "palm_purchase"]


def get_team_user_ids(manager_id):
    """The manager themself + their active staff."""
    rows = get_db().execute(
        "SELECT id FROM users WHERE manager_id=? AND is_active=1", (manager_id,)
    ).fetchall()
    return [manager_id] + [r["id"] for r in rows]


def get_scoped_client_ids(user):
    """Set of client ids a sales-role user may see, or None for unrestricted
    (god and regular users are not row-scoped — modules gate them instead)."""
    if getattr(user, "role", None) != "sales":
        return None
    team = get_team_user_ids(user.id)
    ph = ",".join("?" * len(team))
    rows = get_db().execute(
        f"SELECT id FROM clients WHERE sales_rep_id IN ({ph})", team
    ).fetchall()
    return {r["id"] for r in rows}


def get_own_scoped_client_ids(user):
    """Set of client ids assigned to this user (clients.sales_rep_id), or None
    for unrestricted. Any user with at least one client assigned to them gets
    a dashboard scoped to just those clients; a user with none assigned sees
    company-wide data. God is never scoped, even if somehow assigned a client.
    Sales managers (role='sales') get the wider team view via
    get_scoped_client_ids() instead, on a separate dashboard branch."""
    if user.is_god():
        return None
    rows = get_db().execute(
        "SELECT id FROM clients WHERE sales_rep_id=?", (user.id,)
    ).fetchall()
    ids = {r["id"] for r in rows}
    return ids or None


def set_manager_staff(manager_id, staff_ids):
    """Replace a manager's team in one save: listed users get manager_id set,
    their previous staff not in the list are released."""
    db = get_db()
    ids = sorted({int(i) for i in staff_ids} - {int(manager_id)})
    if ids:
        ph = ",".join("?" * len(ids))
        db.execute(
            f"UPDATE users SET manager_id=NULL WHERE manager_id=? AND id NOT IN ({ph})",
            (manager_id, *ids),
        )
        db.execute(
            f"UPDATE users SET manager_id=? WHERE id IN ({ph}) AND role != 'god'",
            (manager_id, *ids),
        )
    else:
        db.execute("UPDATE users SET manager_id=NULL WHERE manager_id=?", (manager_id,))
    db.commit()


def get_manager_staff(manager_id):
    """Users currently reporting to this manager (active and inactive)."""
    return get_db().execute(
        "SELECT id, name, is_active FROM users WHERE manager_id=? ORDER BY name",
        (manager_id,),
    ).fetchall()

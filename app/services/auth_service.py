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
]

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
]
_ALL_DASH_KEYS = {k for k, _ in DASHBOARD_SECTIONS}


class User(UserMixin):
    def __init__(self, row):
        self.id            = row["id"]
        self.name          = row["name"]
        self.email         = row["email"]
        self.password_hash = row["password_hash"]
        self.role          = row["role"]
        self._is_active    = bool(row["is_active"])

    @property
    def is_active(self):
        return self._is_active

    def is_god(self):
        return self.role == "god"

    def has_permission(self, module, action):
        """action: view | create | edit | delete"""
        if self.is_god():
            return True
        db = get_db()
        row = db.execute(
            "SELECT * FROM user_permissions WHERE user_id=? AND module=?",
            (self.id, module),
        ).fetchone()
        if not row:
            return False
        return bool(row[f"can_{action}"])

    def get_all_permissions(self):
        """Return dict of {module: {view,create,edit,delete}}"""
        db = get_db()
        rows = db.execute(
            "SELECT * FROM user_permissions WHERE user_id=?", (self.id,)
        ).fetchall()
        perms = {m: {"view": False, "create": False, "edit": False, "delete": False}
                 for m in MODULES}
        for r in rows:
            if r["module"] in perms:
                perms[r["module"]] = {
                    "view":   bool(r["can_view"]),
                    "create": bool(r["can_create"]),
                    "edit":   bool(r["can_edit"]),
                    "delete": bool(r["can_delete"]),
                }
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
            """INSERT INTO user_permissions (user_id, module, can_view, can_create, can_edit, can_delete)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(user_id, module) DO UPDATE SET
                   can_view=excluded.can_view,
                   can_create=excluded.can_create,
                   can_edit=excluded.can_edit,
                   can_delete=excluded.can_delete""",
            (user_id, module,
             1 if perms.get("view")   else 0,
             1 if perms.get("create") else 0,
             1 if perms.get("edit")   else 0,
             1 if perms.get("delete") else 0),
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

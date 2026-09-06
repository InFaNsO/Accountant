from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import current_user
from ..services import client_service
from ..services.auth_service import (
    god_required, get_all_users, get_user, create_user,
    update_user, delete_user, MODULES, DASHBOARD_SECTIONS,
    CLIENT_EXTRAS, CLIENT_EXTRA_FLAGS,
    set_manager_staff, SALES_DEFAULT_DENIED_MODULES,
)

bp = Blueprint("users", __name__, url_prefix="/users")


def _parse_managed_clients(form):
    """Client ids ticked in the 'Managed Clients' section of the user form."""
    ids = []
    for raw in form.getlist("managed_clients"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            pass
    return ids


def _parse_team(form):
    """User ids ticked in the 'Sales Team' section of the user form."""
    ids = []
    for raw in form.getlist("team_staff"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            pass
    return ids


def _team_candidates(exclude_id=None):
    """Users who can be put on a sales manager's team."""
    return [dict(u) for u in get_all_users()
            if u["role"] != "god" and u["id"] != exclude_id]


def _parse_permissions(form):
    """Extract permission checkboxes from the submitted form."""
    perms = {}
    for module in MODULES:
        perms[module] = {
            "view":   bool(form.get(f"perm_{module}_view")),
            "create": bool(form.get(f"perm_{module}_create")),
            "edit":   bool(form.get(f"perm_{module}_edit")),
            "delete": bool(form.get(f"perm_{module}_delete")),
        }
    # Clients-only extra permissions (financials, locks_view, locks_edit, …)
    for flag in CLIENT_EXTRA_FLAGS:
        perms["clients"][flag] = bool(form.get(f"perm_clients_{flag}"))
    return perms


def _parse_dashboard_sections(form):
    """Extract dashboard section checkboxes from the submitted form."""
    return [key for key, _ in DASHBOARD_SECTIONS if form.get(f"dash_{key}")]


@bp.route("/")
@god_required
def list_users():
    users = get_all_users()
    return render_template("users/list.html", users=users)


@bp.route("/new", methods=["GET", "POST"])
@god_required
def new_user():
    all_clients = [dict(c) for c in client_service.get_clients_with_reps()]
    candidates  = _team_candidates()
    if request.method == "POST":
        data    = request.form.to_dict()
        perms   = _parse_permissions(request.form)
        dash    = _parse_dashboard_sections(request.form)
        managed = _parse_managed_clients(request.form)
        team    = _parse_team(request.form)
        if not data.get("name") or not data.get("email") or not data.get("password"):
            flash("Name, email and password are required.", "error")
            return render_template("users/form.html", user=data, perms=perms,
                                   modules=MODULES, client_extras=CLIENT_EXTRAS, action="new",
                                   dash_sections=DASHBOARD_SECTIONS,
                                   user_dash_sections=set(dash),
                                   all_clients=all_clients, managed_ids=set(managed),
                                   team_candidates=candidates, team_ids=set(team),
                                   sales_denied=SALES_DEFAULT_DENIED_MODULES)
        try:
            user_id = create_user(data, perms, dash)
            if managed:
                client_service.set_rep_clients(user_id, managed)
            if data.get("role") == "sales" and team:
                set_manager_staff(user_id, team)
            flash(f"User {data['name']} created.", "success")
            return redirect(url_for("users.list_users"))
        except Exception as e:
            flash(f"Error: {e}", "error")
            return render_template("users/form.html", user=data, perms=perms,
                                   modules=MODULES, client_extras=CLIENT_EXTRAS, action="new",
                                   dash_sections=DASHBOARD_SECTIONS,
                                   user_dash_sections=set(dash),
                                   all_clients=all_clients, managed_ids=set(managed),
                                   team_candidates=candidates, team_ids=set(team),
                                   sales_denied=SALES_DEFAULT_DENIED_MODULES)
    empty_perms = {m: {"view": False, "create": False, "edit": False, "delete": False}
                   for m in MODULES}
    return render_template("users/form.html", user={}, perms=empty_perms,
                           modules=MODULES, client_extras=CLIENT_EXTRAS, action="new",
                           dash_sections=DASHBOARD_SECTIONS,
                           user_dash_sections=set(),
                           all_clients=all_clients, managed_ids=set(),
                           team_candidates=candidates, team_ids=set(),
                           sales_denied=SALES_DEFAULT_DENIED_MODULES)


@bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@god_required
def edit_user(user_id):
    row = get_user(user_id)
    if not row:
        flash("User not found.", "error")
        return redirect(url_for("users.list_users"))
    from ..services.auth_service import User
    user_obj = User(row)
    all_clients = [dict(c) for c in client_service.get_clients_with_reps()]
    candidates  = _team_candidates(exclude_id=user_id)
    if request.method == "POST":
        data    = request.form.to_dict()
        perms   = _parse_permissions(request.form)
        dash    = _parse_dashboard_sections(request.form)
        managed = _parse_managed_clients(request.form)
        team    = _parse_team(request.form)
        if not data.get("name") or not data.get("email"):
            flash("Name and email are required.", "error")
            return render_template("users/form.html", user=data, perms=perms,
                                   modules=MODULES, client_extras=CLIENT_EXTRAS, action="edit", user_id=user_id,
                                   dash_sections=DASHBOARD_SECTIONS,
                                   user_dash_sections=set(dash),
                                   all_clients=all_clients, managed_ids=set(managed),
                                   team_candidates=candidates, team_ids=set(team),
                                   sales_denied=SALES_DEFAULT_DENIED_MODULES)
        # Prevent demoting or deactivating the god account
        if row["role"] == "god":
            data["role"] = "god"
            data["is_active"] = "1"
        update_user(user_id, data, perms, dash)
        # Saving the user's client list is what assigns/releases the reps'
        # clients (the god account never holds assignments).
        if row["role"] != "god":
            client_service.set_rep_clients(user_id, managed)
            # Team only applies to sales managers; demoting the role releases it.
            set_manager_staff(user_id, team if data.get("role") == "sales" else [])
        flash("User updated.", "success")
        return redirect(url_for("users.list_users"))
    perms = user_obj.get_all_permissions()
    user_dash = user_obj.get_dashboard_sections()
    managed_ids = {c["id"] for c in all_clients if c["sales_rep_id"] == user_id}
    team_ids = {u["id"] for u in candidates if u.get("manager_id") == user_id}
    return render_template("users/form.html", user=dict(row), perms=perms,
                           modules=MODULES, client_extras=CLIENT_EXTRAS, action="edit", user_id=user_id,
                           dash_sections=DASHBOARD_SECTIONS,
                           user_dash_sections=user_dash,
                           all_clients=all_clients, managed_ids=managed_ids,
                           team_candidates=candidates, team_ids=team_ids,
                           sales_denied=SALES_DEFAULT_DENIED_MODULES)


@bp.route("/<int:user_id>/delete", methods=["POST"])
@god_required
def delete_user_route(user_id):
    row = get_user(user_id)
    if row and row["role"] == "god":
        flash("Cannot delete the god account.", "error")
        return redirect(url_for("users.list_users"))
    if row and row["id"] == current_user.id:
        flash("Cannot delete your own account.", "error")
        return redirect(url_for("users.list_users"))
    delete_user(user_id)
    flash("User deleted.", "success")
    return redirect(url_for("users.list_users"))

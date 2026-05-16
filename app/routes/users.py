from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import current_user
from ..services.auth_service import (
    god_required, get_all_users, get_user, create_user,
    update_user, delete_user, MODULES,
)

bp = Blueprint("users", __name__, url_prefix="/users")


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
    return perms


@bp.route("/")
@god_required
def list_users():
    users = get_all_users()
    return render_template("users/list.html", users=users)


@bp.route("/new", methods=["GET", "POST"])
@god_required
def new_user():
    if request.method == "POST":
        data  = request.form.to_dict()
        perms = _parse_permissions(request.form)
        if not data.get("name") or not data.get("email") or not data.get("password"):
            flash("Name, email and password are required.", "error")
            return render_template("users/form.html", user=data, perms=perms,
                                   modules=MODULES, action="new")
        try:
            create_user(data, perms)
            flash(f"User {data['name']} created.", "success")
            return redirect(url_for("users.list_users"))
        except Exception as e:
            flash(f"Error: {e}", "error")
            return render_template("users/form.html", user=data, perms=perms,
                                   modules=MODULES, action="new")
    empty_perms = {m: {"view": False, "create": False, "edit": False, "delete": False}
                   for m in MODULES}
    return render_template("users/form.html", user={}, perms=empty_perms,
                           modules=MODULES, action="new")


@bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@god_required
def edit_user(user_id):
    row = get_user(user_id)
    if not row:
        flash("User not found.", "error")
        return redirect(url_for("users.list_users"))
    from ..services.auth_service import User
    user_obj = User(row)
    if request.method == "POST":
        data  = request.form.to_dict()
        perms = _parse_permissions(request.form)
        if not data.get("name") or not data.get("email"):
            flash("Name and email are required.", "error")
            return render_template("users/form.html", user=data, perms=perms,
                                   modules=MODULES, action="edit", user_id=user_id)
        # Prevent demoting or deactivating the god account
        if row["role"] == "god":
            data["role"] = "god"
            data["is_active"] = "1"
        update_user(user_id, data, perms)
        flash("User updated.", "success")
        return redirect(url_for("users.list_users"))
    perms = user_obj.get_all_permissions()
    return render_template("users/form.html", user=dict(row), perms=perms,
                           modules=MODULES, action="edit", user_id=user_id)


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

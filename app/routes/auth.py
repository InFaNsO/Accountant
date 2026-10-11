from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from ..services.auth_service import verify_password

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        user = verify_password(email, password)
        if not user:
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html", email=email)
        login_user(user, remember=remember)
        next_page = request.args.get("next")
        return redirect(next_page or url_for("dashboard.index"))
    return render_template("auth/login.html", email="")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))

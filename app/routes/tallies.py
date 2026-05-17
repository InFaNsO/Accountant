from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from ..services import tally_service
from ..services.auth_service import permission_required

bp = Blueprint("tallies", __name__, url_prefix="/tallies")


@bp.route("/")
@login_required
@permission_required("products", "view")
def list_tallies():
    tallies = tally_service.get_all_tallies()
    return render_template("tallies/list.html", tallies=tallies)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("products", "create")
def new_tally():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        notes = request.form.get("notes", "").strip()
        if not name:
            flash("Tally name is required.", "error")
            return render_template("tallies/new.html")
        tally_id = tally_service.create_tally(name, notes)
        flash("Stock tally created.", "success")
        return redirect(url_for("tallies.tally_detail", tally_id=tally_id))
    return render_template("tallies/new.html")


@bp.route("/<int:tally_id>")
@login_required
@permission_required("products", "view")
def tally_detail(tally_id):
    tally, categories = tally_service.get_tally(tally_id)
    if not tally:
        flash("Tally not found.", "error")
        return redirect(url_for("tallies.list_tallies"))
    return render_template("tallies/detail.html", tally=tally, categories=categories)


@bp.route("/<int:tally_id>/save", methods=["POST"])
@login_required
@permission_required("products", "edit")
def save_tally(tally_id):
    tally_service.save_tally_items(tally_id, request.form.to_dict())
    flash("Physical counts saved.", "success")
    return redirect(url_for("tallies.tally_detail", tally_id=tally_id))


@bp.route("/<int:tally_id>/refresh/<int:product_id>", methods=["POST"])
@login_required
@permission_required("products", "edit")
def refresh_digital(tally_id, product_id):
    # Save any current inputs before refreshing
    tally_service.save_tally_items(tally_id, request.form.to_dict())
    ok = tally_service.refresh_product_digital(tally_id, product_id)
    if ok:
        flash("Digital stock refreshed to current warehouse levels.", "success")
    else:
        flash("Could not refresh — tally may already be applied.", "error")
    return redirect(url_for("tallies.tally_detail", tally_id=tally_id))


@bp.route("/<int:tally_id>/apply", methods=["POST"])
@login_required
@permission_required("products", "edit")
def apply_tally(tally_id):
    # Save form data first so nothing is lost
    tally_service.save_tally_items(tally_id, request.form.to_dict())
    ok, err = tally_service.apply_tally(tally_id)
    if ok:
        flash("Corrections applied — digital stock now matches physical count.", "success")
    else:
        flash(f"Cannot apply: {err}", "error")
    return redirect(url_for("tallies.tally_detail", tally_id=tally_id))

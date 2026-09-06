from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required
from ..services import tally_service
from ..services.auth_service import permission_required
from ..database import get_db

bp = Blueprint("tallies", __name__, url_prefix="/tallies")


def _wants_json():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _item_payload(tally_id, item_id):
    r = get_db().execute(
        "SELECT id, digital_qty, physical_qty FROM stock_tally_items WHERE id=? AND tally_id=?",
        (item_id, tally_id),
    ).fetchone()
    return None if not r else {"id": r["id"], "digital_qty": r["digital_qty"], "physical_qty": r["physical_qty"]}


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
    if _wants_json():
        if not ok:
            return jsonify({"ok": False, "error": "Could not refresh — tally may already be applied."})
        rows = get_db().execute(
            "SELECT id, digital_qty, physical_qty FROM stock_tally_items WHERE tally_id=? AND product_id=?",
            (tally_id, product_id),
        ).fetchall()
        return jsonify({"ok": True, "message": "Digital stock refreshed to current warehouse levels.",
                        "items": [{"id": r["id"], "digital_qty": r["digital_qty"], "physical_qty": r["physical_qty"]} for r in rows]})
    if ok:
        flash("Digital stock refreshed to current warehouse levels.", "success")
    else:
        flash("Could not refresh — tally may already be applied.", "error")
    return redirect(url_for("tallies.tally_detail", tally_id=tally_id))


@bp.route("/<int:tally_id>/refresh-item/<int:item_id>", methods=["POST"])
@login_required
@permission_required("products", "edit")
def refresh_item(tally_id, item_id):
    tally_service.save_tally_items(tally_id, request.form.to_dict())
    ok = tally_service.refresh_item_digital(tally_id, item_id)
    if _wants_json():
        if not ok:
            return jsonify({"ok": False, "error": "Could not refresh — tally may already be applied."})
        return jsonify({"ok": True, "message": "Digital stock refreshed.",
                        "item": _item_payload(tally_id, item_id)})
    flash("Digital stock refreshed." if ok else "Could not refresh — tally may already be applied.",
          "success" if ok else "error")
    return redirect(url_for("tallies.tally_detail", tally_id=tally_id))


@bp.route("/<int:tally_id>/correct-item/<int:item_id>", methods=["POST"])
@login_required
@permission_required("products", "edit")
def correct_item(tally_id, item_id):
    tally_service.save_tally_items(tally_id, request.form.to_dict())
    ok, err = tally_service.apply_item_correction(tally_id, item_id)
    if _wants_json():
        if not ok:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, "message": "Correction inserted — stock adjusted.",
                        "item": _item_payload(tally_id, item_id)})
    flash("Correction inserted — warehouse stock adjusted to the counted quantity."
          if ok else f"Cannot insert correction: {err}", "success" if ok else "error")
    return redirect(url_for("tallies.tally_detail", tally_id=tally_id))


@bp.route("/<int:tally_id>/correct-product/<int:product_id>", methods=["POST"])
@login_required
@permission_required("products", "edit")
def correct_product(tally_id, product_id):
    tally_service.save_tally_items(tally_id, request.form.to_dict())
    ok, err, n = tally_service.apply_product_corrections(tally_id, product_id)
    if _wants_json():
        if not ok:
            return jsonify({"ok": False, "error": err})
        rows = get_db().execute(
            "SELECT id, digital_qty, physical_qty FROM stock_tally_items WHERE tally_id=? AND product_id=?",
            (tally_id, product_id),
        ).fetchall()
        return jsonify({"ok": True, "adjusted": n,
                        "message": f"Corrections inserted for {n} sub-product(s) — stock adjusted.",
                        "items": [{"id": r["id"], "digital_qty": r["digital_qty"], "physical_qty": r["physical_qty"]} for r in rows]})
    flash(f"Corrections inserted for {n} sub-product(s) — warehouse stock adjusted."
          if ok else f"Cannot insert corrections: {err}", "success" if ok else "error")
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

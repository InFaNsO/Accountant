from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from datetime import date

from ..services import palm_purchase_service, supplier_service, product_service
from ..services.auth_service import permission_required

bp = Blueprint("palm_purchase", __name__, url_prefix="/palm-purchase")


def _build_product_choices():
    choices = []
    for p in product_service.get_all_products(active_only=True):
        subs = product_service.get_sub_products(p["id"])
        if subs:
            for s in subs:
                if not s["is_active"]:
                    continue
                choices.append({
                    "product_id":     p["id"],
                    "sub_product_id": s["id"],
                    "name":           f"{p['name']} — {s['name']}",
                    "sku":            s["sku"] or p["sku"] or "",
                    "unit_price":     p["unit_price"] if s["use_parent_price"] else (s["unit_price"] or 0),
                    "stock_qty":      s["stock_qty"] or 0,
                })
        else:
            choices.append({
                "product_id":     p["id"],
                "sub_product_id": None,
                "name":           p["name"],
                "sku":            p["sku"] or "",
                "unit_price":     p["unit_price"] or 0,
                "stock_qty":      p["stock_qty"] or 0,
            })
    return choices


def _parse_items(form):
    items = []
    i = 0
    while True:
        if (f"product_id_{i}" not in form and
            f"sub_product_id_{i}" not in form and
            f"quantity_{i}" not in form):
            break
        pid  = form.get(f"product_id_{i}") or None
        sid  = form.get(f"sub_product_id_{i}") or None
        qty  = form.get(f"quantity_{i}", "")
        cost = form.get(f"unit_cost_{i}", "")
        notes = form.get(f"notes_{i}", "").strip() or None
        if not pid and not sid:
            i += 1
            continue
        try:
            qty = float(qty)
            if qty <= 0:
                i += 1
                continue
        except ValueError:
            i += 1
            continue
        try:
            cost = float(cost) if cost not in ("", None) else 0.0
        except ValueError:
            cost = 0.0
        items.append({
            "product_id":     pid,
            "sub_product_id": sid,
            "quantity":       qty,
            "unit_cost":      cost,
            "notes":          notes,
        })
        i += 1
    return items


@bp.route("/")
@login_required
@permission_required("palm_purchase", "view")
def list_purchases():
    purchases = palm_purchase_service.get_all_palm_purchases()
    return render_template("palm_purchase/list.html", purchases=purchases)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("palm_purchase", "create")
def new_purchase():
    suppliers = supplier_service.get_all_suppliers()
    products  = _build_product_choices()
    if request.method == "POST":
        data  = request.form.to_dict()
        items = _parse_items(request.form)
        if not items:
            flash("Add at least one product with a positive quantity.", "error")
            return render_template("palm_purchase/form.html", pp=data, action="new",
                                   suppliers=suppliers, products=products,
                                   today=str(date.today()))
        try:
            pp_id = palm_purchase_service.create_palm_purchase(data, items)
        except ValueError as e:
            flash(str(e), "error")
            return render_template("palm_purchase/form.html", pp=data, action="new",
                                   suppliers=suppliers, products=products,
                                   today=str(date.today()))
        flash("Palm purchase recorded — warehouse stock updated.", "success")
        return redirect(url_for("palm_purchase.detail", pp_id=pp_id))
    return render_template("palm_purchase/form.html", pp={}, action="new",
                           suppliers=suppliers, products=products,
                           today=str(date.today()))


@bp.route("/<int:pp_id>")
@login_required
@permission_required("palm_purchase", "view")
def detail(pp_id):
    pp = palm_purchase_service.get_palm_purchase(pp_id)
    if not pp:
        flash("Palm purchase not found.", "error")
        return redirect(url_for("palm_purchase.list_purchases"))
    items = palm_purchase_service.get_palm_purchase_items(pp_id)
    return render_template("palm_purchase/detail.html", pp=pp, items=items)


@bp.route("/<int:pp_id>/delete", methods=["POST"])
@login_required
@permission_required("palm_purchase", "delete")
def delete(pp_id):
    palm_purchase_service.delete_palm_purchase(pp_id)
    flash("Palm purchase deleted — warehouse stock reversed.", "success")
    return redirect(url_for("palm_purchase.list_purchases"))

from flask import Blueprint, render_template, request, redirect, url_for, flash
from ..services import production_service, supplier_service, product_service

bp = Blueprint("production", __name__, url_prefix="/production")


def _parse_items(form):
    items = []
    i = 0
    while True:
        if f"product_id_{i}" not in form and f"sub_product_id_{i}" not in form:
            break
        pid  = form.get(f"product_id_{i}") or None
        sid  = form.get(f"sub_product_id_{i}") or None
        qty  = form.get(f"quantity_{i}", "")
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
        items.append({
            "product_id":     pid,
            "sub_product_id": sid,
            "quantity":       qty,
            "price":          form.get(f"price_{i}") or None,
        })
        i += 1
    return items


@bp.route("/")
def list_production():
    orders = production_service.get_all_production_orders()
    return render_template("production/list.html", orders=orders)


@bp.route("/new", methods=["GET", "POST"])
def new_production():
    suppliers = supplier_service.get_all_suppliers()
    products  = _build_product_choices()
    if request.method == "POST":
        data  = request.form.to_dict()
        items = _parse_items(request.form)
        if not data.get("name"):
            flash("Order name is required.", "error")
            return render_template("production/form.html", po={}, action="new",
                                   suppliers=suppliers, products=products)
        if not items:
            flash("Add at least one product line.", "error")
            return render_template("production/form.html", po=data, action="new",
                                   suppliers=suppliers, products=products)
        po_id = production_service.create_production_order(data, items)
        flash("Production order created.", "success")
        return redirect(url_for("production.detail", po_id=po_id))
    return render_template("production/form.html", po={}, action="new",
                           suppliers=suppliers, products=products)


@bp.route("/<int:po_id>")
def detail(po_id):
    po = production_service.get_production_order(po_id)
    if not po:
        flash("Production order not found.", "error")
        return redirect(url_for("production.list_production"))
    items = production_service.get_po_items(po_id)
    return render_template("production/detail.html", po=po, items=items)


@bp.route("/<int:po_id>/edit", methods=["GET", "POST"])
def edit_production(po_id):
    po = production_service.get_production_order(po_id)
    if not po:
        flash("Production order not found.", "error")
        return redirect(url_for("production.list_production"))
    suppliers = supplier_service.get_all_suppliers()
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Order name is required.", "error")
            return render_template("production/edit.html", po=data,
                                   suppliers=suppliers, po_id=po_id)
        production_service.update_production_order(po_id, data)
        flash("Production order updated.", "success")
        return redirect(url_for("production.detail", po_id=po_id))
    return render_template("production/edit.html", po=dict(po),
                           suppliers=suppliers, po_id=po_id)


@bp.route("/<int:po_id>/close", methods=["POST"])
def close_production(po_id):
    production_service.close_production_order(po_id)
    flash("Production order closed.", "success")
    return redirect(url_for("production.detail", po_id=po_id))


@bp.route("/<int:po_id>/delete", methods=["POST"])
def delete_production(po_id):
    production_service.delete_production_order(po_id)
    flash("Production order deleted.", "success")
    return redirect(url_for("production.list_production"))


def _build_product_choices():
    choices = []
    for p in product_service.get_all_products(active_only=False):
        subs = product_service.get_sub_products(p["id"])
        if subs:
            for s in subs:
                choices.append({
                    "product_id":     p["id"],
                    "sub_product_id": s["id"],
                    "label": f"{p['name']} — {s['name']}" + (f" [{s['sku']}]" if s["sku"] else ""),
                    "price": p["unit_price"] if s["use_parent_price"] else s["unit_price"],
                })
        else:
            choices.append({"product_id": p["id"], "sub_product_id": None,
                            "label": p["name"] + (f" [{p['sku']}]" if p["sku"] else ""),
                            "price": p["unit_price"]})
    return choices

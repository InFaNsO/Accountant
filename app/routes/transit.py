from flask import Blueprint, render_template, request, redirect, url_for, flash
from ..services import transit_service, supplier_service, product_service

bp = Blueprint("transit", __name__, url_prefix="/transit")


def _parse_items(form):
    items = []
    i = 0
    while True:
        if f"product_id_{i}" not in form and f"sub_product_id_{i}" not in form:
            break
        pid = form.get(f"product_id_{i}") or None
        sid = form.get(f"sub_product_id_{i}") or None
        qty = form.get(f"quantity_{i}", "")
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
            "cbm":            form.get(f"cbm_{i}") or None,
            "gross_weight":   form.get(f"gross_weight_{i}") or None,
        })
        i += 1
    return items


@bp.route("/")
def list_transit():
    dispatches = transit_service.get_all_dispatches()
    return render_template("transit/list.html", dispatches=dispatches)


@bp.route("/new", methods=["GET", "POST"])
def new_dispatch():
    suppliers = supplier_service.get_all_suppliers()
    products  = _build_product_choices()
    if request.method == "POST":
        data  = request.form.to_dict()
        items = _parse_items(request.form)
        if not data.get("name"):
            flash("Dispatch name is required.", "error")
            return render_template("transit/form.html", dispatch={}, action="new",
                                   suppliers=suppliers, products=products)
        if not items:
            flash("Add at least one product line.", "error")
            return render_template("transit/form.html", dispatch=data, action="new",
                                   suppliers=suppliers, products=products)
        did, warnings = transit_service.create_dispatch(data, items)
        for w in warnings:
            flash(w, "warning")
        flash("Dispatch created.", "success")
        return redirect(url_for("transit.detail", dispatch_id=did))
    return render_template("transit/form.html", dispatch={}, action="new",
                           suppliers=suppliers, products=products)


@bp.route("/<int:dispatch_id>")
def detail(dispatch_id):
    dispatch = transit_service.get_dispatch(dispatch_id)
    if not dispatch:
        flash("Dispatch not found.", "error")
        return redirect(url_for("transit.list_transit"))
    items = transit_service.get_dispatch_items(dispatch_id)
    return render_template("transit/detail.html", dispatch=dispatch, items=items)


@bp.route("/<int:dispatch_id>/receive", methods=["POST"])
def receive(dispatch_id):
    received = {}
    for key, val in request.form.items():
        if key.startswith("recv_"):
            di_id = key.replace("recv_", "")
            try:
                qty = float(val)
                if qty > 0:
                    received[di_id] = qty
            except ValueError:
                pass
    if received:
        transit_service.receive_items(dispatch_id, received)
        flash("Stock updated from received items.", "success")
    else:
        flash("No quantities entered.", "error")
    return redirect(url_for("transit.detail", dispatch_id=dispatch_id))


@bp.route("/<int:dispatch_id>/delete", methods=["POST"])
def delete_dispatch(dispatch_id):
    transit_service.delete_dispatch(dispatch_id)
    flash("Dispatch deleted and quantities reversed.", "success")
    return redirect(url_for("transit.list_transit"))


def _build_product_choices():
    choices = []
    for p in product_service.get_all_products(active_only=False):
        choices.append({
            "product_id": p["id"], "sub_product_id": None,
            "label": p["name"] + (f" [{p['sku']}]" if p["sku"] else ""),
            "production_qty": p["production_qty"],
        })
        for s in product_service.get_sub_products(p["id"]):
            choices.append({
                "product_id":     p["id"],
                "sub_product_id": s["id"],
                "label": f"{p['name']} — {s['name']}" + (f" [{s['sku']}]" if s["sku"] else ""),
                "production_qty": s["production_qty"],
            })
    return choices

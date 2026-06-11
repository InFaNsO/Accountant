from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from ..services import transit_service, supplier_service, product_service
from ..services.auth_service import permission_required

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
@login_required
@permission_required("transit", "view")
def list_transit():
    # All dispatches (ordered by nearest arrival); status filtering is done client-side.
    dispatches = transit_service.get_all_dispatches()
    return render_template("transit/list.html", dispatches=dispatches)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("transit", "create")
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
        is_draft = data.get("status") == "draft"
        # Validate dispatch qty does not exceed available production qty.
        # Drafts skip this check — availability is re-validated on activation instead.
        if not is_draft:
            errors = []
            for it in items:
                pid = int(it["product_id"]) if it.get("product_id") else None
                sid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
                qty = float(it["quantity"])
                if sid:
                    row = product_service.get_sub_product(sid)
                    available = float(row["production_qty"] or 0) if row else 0
                    name = f"{row['parent_name']} — {row['name']}" if row else f"Sub-product #{sid}"
                else:
                    row = product_service.get_product(pid)
                    available = float(row["production_qty"] or 0) if row else 0
                    name = row["name"] if row else f"Product #{pid}"
                if qty > available:
                    errors.append(f"{name}: dispatch qty {qty} exceeds production qty {available}.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("transit/form.html", dispatch=data, action="new",
                                       suppliers=suppliers, products=products)
        did, warnings = transit_service.create_dispatch(data, items)
        for w in warnings:
            flash(w, "warning")
        if is_draft:
            flash("Draft dispatch saved — no stock moved yet.", "success")
        else:
            flash("Dispatch created.", "success")
        return redirect(url_for("transit.detail", dispatch_id=did))
    return render_template("transit/form.html", dispatch={}, action="new",
                           suppliers=suppliers, products=products)


@bp.route("/<int:dispatch_id>")
@login_required
@permission_required("transit", "view")
def detail(dispatch_id):
    dispatch = transit_service.get_dispatch(dispatch_id)
    if not dispatch:
        flash("Dispatch not found.", "error")
        return redirect(url_for("transit.list_transit"))
    items = transit_service.get_dispatch_items(dispatch_id)
    return render_template("transit/detail.html", dispatch=dispatch, items=items)


@bp.route("/<int:dispatch_id>/activate", methods=["POST"])
@login_required
@permission_required("transit", "edit")
def activate(dispatch_id):
    ok, errors, warnings = transit_service.activate_dispatch(dispatch_id)
    if not ok:
        for e in errors:
            flash(e, "error")
    else:
        for w in warnings:
            flash(w, "warning")
        flash("Dispatch activated — stock moved to in-transit.", "success")
    return redirect(url_for("transit.detail", dispatch_id=dispatch_id))


@bp.route("/<int:dispatch_id>/receive", methods=["POST"])
@login_required
@permission_required("transit", "edit")
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
@login_required
@permission_required("transit", "delete")
def delete_dispatch(dispatch_id):
    transit_service.delete_dispatch(dispatch_id)
    flash("Dispatch deleted and quantities reversed.", "success")
    return redirect(url_for("transit.list_transit"))


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
                    "production_qty": s["production_qty"] or 0,
                })
        else:
            choices.append({
                "product_id": p["id"], "sub_product_id": None,
                "label": p["name"] + (f" [{p['sku']}]" if p["sku"] else ""),
                "production_qty": p["production_qty"] or 0,
            })
    return choices

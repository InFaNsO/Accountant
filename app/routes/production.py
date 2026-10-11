from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from ..services import production_service, supplier_service, product_service
from ..services.auth_service import permission_required

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
@login_required
@permission_required("production", "view")
def list_production():
    # All production orders (ordered by nearest completion); status filtering is done client-side.
    orders = production_service.get_all_production_orders()
    return render_template("production/list.html", orders=orders)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("production", "create")
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
        if data.get("status") == "draft":
            flash("Draft production order saved — no production stock added yet.", "success")
        else:
            flash("Production order created.", "success")
        return redirect(url_for("production.detail", po_id=po_id))
    return render_template("production/form.html", po={}, action="new",
                           suppliers=suppliers, products=products)


@bp.route("/<int:po_id>")
@login_required
@permission_required("production", "view")
def detail(po_id):
    po = production_service.get_production_order(po_id)
    if not po:
        flash("Production order not found.", "error")
        return redirect(url_for("production.list_production"))
    items = production_service.get_po_items(po_id)
    return render_template("production/detail.html", po=po, items=items)


@bp.route("/<int:po_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("production", "edit")
def edit_production(po_id):
    po = production_service.get_production_order(po_id)
    if not po:
        flash("Production order not found.", "error")
        return redirect(url_for("production.list_production"))
    suppliers = supplier_service.get_all_suppliers()
    items     = production_service.get_po_items(po_id)
    products  = _build_product_choices()
    eco_map   = _eco_map(products)
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Order name is required.", "error")
            return render_template("production/edit.html", po=data, po_id=po_id,
                                   suppliers=suppliers, items=items, products=products,
                                   eco_map=eco_map)
        # Edits to existing lines (qty/price). Quantity floor is enforced in the service.
        item_updates = []
        for it in items:
            qty_key = f"existing_qty_{it['id']}"
            if qty_key in request.form:
                item_updates.append({
                    "id":       it["id"],
                    "quantity": request.form.get(qty_key),
                    "price":    request.form.get(f"existing_price_{it['id']}"),
                })
        new_items = _parse_items(request.form)
        production_service.update_production_order(po_id, data, item_updates, new_items)
        flash("Production order updated.", "success")
        return redirect(url_for("production.detail", po_id=po_id))
    return render_template("production/edit.html", po=dict(po), po_id=po_id,
                           suppliers=suppliers, items=items, products=products,
                           eco_map=eco_map)


@bp.route("/<int:po_id>/activate", methods=["POST"])
@login_required
@permission_required("production", "edit")
def activate_production(po_id):
    if production_service.activate_production_order(po_id):
        flash("Production order activated — quantities added to production stock.", "success")
    else:
        flash("This order is not a draft and could not be activated.", "error")
    return redirect(url_for("production.detail", po_id=po_id))


@bp.route("/<int:po_id>/close", methods=["POST"])
@login_required
@permission_required("production", "edit")
def close_production(po_id):
    production_service.close_production_order(po_id)
    flash("Production order closed.", "success")
    return redirect(url_for("production.detail", po_id=po_id))


@bp.route("/<int:po_id>/delete", methods=["POST"])
@login_required
@permission_required("production", "delete")
def delete_production(po_id):
    production_service.delete_production_order(po_id)
    flash("Production order deleted.", "success")
    return redirect(url_for("production.list_production"))


def _build_product_choices():
    """Catalog choices for the line-item picker, each annotated with its eco
    counterpart (`eco`) when one exists — used by the 'Switch to Eco' control.

    Eco pairing (1:1):
      • product (no subs): main has has_eco_range=1; eco has eco_parent_id=main.
      • sub-product:        main sub has an eco sub whose eco_parent_sub_id=main_sub.
    The counterpart is bidirectional, so a main line can switch to its eco line and
    vice-versa.
    """
    choices = []
    sub_eco_parent = {}   # sub_id -> eco_parent_sub_id  (set => this sub IS an eco sub)
    sub_to_product = {}   # sub_id -> owning product_id
    prod_eco_parent = {}  # product_id -> eco_parent_id  (set => this product IS an eco product)
    for p in product_service.get_all_products(active_only=False):
        prod_eco_parent[p["id"]] = p["eco_parent_id"]
        subs = product_service.get_sub_products(p["id"])
        if subs:
            for s in subs:
                sub_eco_parent[s["id"]] = s["eco_parent_sub_id"]
                sub_to_product[s["id"]] = p["id"]
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

    by_key       = {(c["product_id"], c["sub_product_id"]): c for c in choices}
    eco_sub_of   = {parent: sid for sid, parent in sub_eco_parent.items() if parent}   # main_sub -> eco_sub
    eco_prod_of  = {parent: pid for pid, parent in prod_eco_parent.items() if parent}  # main_prod -> eco_prod

    for c in choices:
        cp_key, direction = None, None
        if c["sub_product_id"]:
            sid = c["sub_product_id"]
            if sub_eco_parent.get(sid):                 # eco sub -> its main sub
                cp_sid, direction = sub_eco_parent[sid], "to_main"
            elif sid in eco_sub_of:                     # main sub -> its eco sub
                cp_sid, direction = eco_sub_of[sid], "to_eco"
            else:
                cp_sid = None
            if cp_sid and cp_sid in sub_to_product:
                cp_key = (sub_to_product[cp_sid], cp_sid)
        else:
            pid = c["product_id"]
            if prod_eco_parent.get(pid):                # eco product -> its main product
                cp_key, direction = (prod_eco_parent[pid], None), "to_main"
            elif pid in eco_prod_of:                    # main product -> its eco product
                cp_key, direction = (eco_prod_of[pid], None), "to_eco"
        cp = by_key.get(cp_key) if cp_key else None
        c["eco"] = {
            "product_id":     cp["product_id"],
            "sub_product_id": cp["sub_product_id"],
            "label":          cp["label"],
            "price":          cp["price"],
            "direction":      direction,
        } if cp else None
    return choices


def _eco_map(choices):
    """{'pid:sid': eco-counterpart-dict} for every choice that has an eco pair."""
    return {
        f"{c['product_id']}:{c['sub_product_id'] or ''}": c["eco"]
        for c in choices if c.get("eco")
    }

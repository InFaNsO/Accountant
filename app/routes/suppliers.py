from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required
from ..services import supplier_service, product_service
from ..services.auth_service import permission_required

bp = Blueprint("suppliers", __name__, url_prefix="/suppliers")


@bp.route("/")
@login_required
@permission_required("suppliers", "view")
def list_suppliers():
    suppliers = supplier_service.get_all_suppliers()
    return render_template("suppliers/list.html", suppliers=suppliers)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("suppliers", "create")
def new_supplier():
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Supplier name is required.", "error")
            return render_template("suppliers/form.html", supplier=data, action="new")
        sid = supplier_service.create_supplier(data)
        flash("Supplier created.", "success")
        return redirect(url_for("suppliers.detail", supplier_id=sid))
    return render_template("suppliers/form.html", supplier={}, action="new")


@bp.route("/<int:supplier_id>")
@login_required
@permission_required("suppliers", "view")
def detail(supplier_id):
    supplier = supplier_service.get_supplier(supplier_id)
    if not supplier:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers.list_suppliers"))
    sp_list  = supplier_service.get_supplier_products(supplier_id)
    products = product_service.get_all_products(active_only=False)

    # Build sets of already-added IDs
    added_sub_ids        = {sp["sub_product_id"] for sp in sp_list if sp["sub_product_id"]}
    added_product_nosub  = {sp["product_id"]     for sp in sp_list if not sp["sub_product_id"]}

    # Filter choices: hide parents whose all subs are present, hide simple products already added
    choices = []
    for p in products:
        subs = product_service.get_sub_products(p["id"])
        if subs:
            all_sub_ids = {s["id"] for s in subs}
            if not all_sub_ids.issubset(added_sub_ids):
                choices.append({"id": p["id"], "label": p["name"] + (f" [{p['sku']}]" if p["sku"] else "")})
        else:
            if p["id"] not in added_product_nosub:
                choices.append({"id": p["id"], "label": p["name"] + (f" [{p['sku']}]" if p["sku"] else "")})

    return render_template("suppliers/detail.html",
                           supplier=supplier, sp_list=sp_list, choices=choices)


@bp.route("/<int:supplier_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("suppliers", "edit")
def edit_supplier(supplier_id):
    supplier = supplier_service.get_supplier(supplier_id)
    if not supplier:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers.list_suppliers"))
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Supplier name is required.", "error")
            return render_template("suppliers/form.html", supplier=data,
                                   action="edit", supplier_id=supplier_id)
        supplier_service.update_supplier(supplier_id, data)
        flash("Supplier updated.", "success")
        return redirect(url_for("suppliers.detail", supplier_id=supplier_id))
    return render_template("suppliers/form.html", supplier=dict(supplier),
                           action="edit", supplier_id=supplier_id)


@bp.route("/<int:supplier_id>/delete", methods=["POST"])
@login_required
@permission_required("suppliers", "delete")
def delete_supplier(supplier_id):
    supplier_service.delete_supplier(supplier_id)
    flash("Supplier deleted.", "success")
    return redirect(url_for("suppliers.list_suppliers"))


@bp.route("/<int:supplier_id>/products/add", methods=["POST"])
@login_required
@permission_required("suppliers", "edit")
def add_product(supplier_id):
    data = request.form.to_dict()
    pid  = data.get("product_id")
    if not pid:
        flash("Select a product.", "error")
        return redirect(url_for("suppliers.detail", supplier_id=supplier_id))

    existing = supplier_service.get_supplier_products(supplier_id)
    existing_sub_ids     = {sp["sub_product_id"] for sp in existing if sp["sub_product_id"]}
    existing_product_ids = {sp["product_id"]     for sp in existing if not sp["sub_product_id"]}

    subs = product_service.get_sub_products(int(pid))
    if subs:
        added = skipped = 0
        for s in subs:
            if s["id"] in existing_sub_ids:
                skipped += 1
                continue
            supplier_service.add_supplier_product(supplier_id, {
                "product_id":     pid,
                "sub_product_id": str(s["id"]),
                "price":          data.get("price") or None,
                "notes":          data.get("notes"),
            })
            added += 1
        if added == 0:
            flash("All sub-products are already in the price list.", "error")
        elif skipped:
            flash(f"Added {added} new sub-products ({skipped} already existed).", "success")
        else:
            flash(f"Added {added} sub-products to supplier.", "success")
    else:
        if int(pid) in existing_product_ids:
            flash("This product is already in the price list.", "error")
        else:
            supplier_service.add_supplier_product(supplier_id, data)
            flash("Product added to supplier.", "success")

    return redirect(url_for("suppliers.detail", supplier_id=supplier_id))


@bp.route("/<int:supplier_id>/products/<int:sp_id>/update", methods=["POST"])
@login_required
@permission_required("suppliers", "edit")
def update_product(supplier_id, sp_id):
    data  = request.get_json(silent=True) or {}
    price_raw = data.get("price", "")
    try:
        price = float(price_raw) if str(price_raw).strip() != "" else None
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid price value"}), 400
    supplier_service.update_supplier_product(sp_id, price, data.get("notes", ""))
    return jsonify({"ok": True})


@bp.route("/<int:supplier_id>/products/<int:sp_id>/delete", methods=["POST"])
@login_required
@permission_required("suppliers", "delete")
def remove_product(supplier_id, sp_id):
    supplier_service.remove_supplier_product(sp_id)
    flash("Removed from supplier.", "success")
    return redirect(url_for("suppliers.detail", supplier_id=supplier_id))


@bp.route("/api/list")
@login_required
@permission_required("suppliers", "view")
def api_list():
    return jsonify([dict(s) for s in supplier_service.get_all_suppliers()])

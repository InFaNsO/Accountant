from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from ..services import supplier_service, product_service

bp = Blueprint("suppliers", __name__, url_prefix="/suppliers")


@bp.route("/")
def list_suppliers():
    suppliers = supplier_service.get_all_suppliers()
    return render_template("suppliers/list.html", suppliers=suppliers)


@bp.route("/new", methods=["GET", "POST"])
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
def detail(supplier_id):
    supplier = supplier_service.get_supplier(supplier_id)
    if not supplier:
        flash("Supplier not found.", "error")
        return redirect(url_for("suppliers.list_suppliers"))
    sp_list  = supplier_service.get_supplier_products(supplier_id)
    products = product_service.get_all_products(active_only=False)
    # Build flat list: products + sub-products for the add-product dropdown
    choices = []
    for p in products:
        choices.append({"id": p["id"], "sub_id": None,
                        "label": p["name"] + (f" [{p['sku']}]" if p["sku"] else "")})
        for s in product_service.get_sub_products(p["id"]):
            choices.append({"id": p["id"], "sub_id": s["id"],
                            "label": f"{p['name']} — {s['name']}" +
                                     (f" [{s['sku']}]" if s["sku"] else "")})
    return render_template("suppliers/detail.html",
                           supplier=supplier, sp_list=sp_list, choices=choices)


@bp.route("/<int:supplier_id>/edit", methods=["GET", "POST"])
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
def delete_supplier(supplier_id):
    supplier_service.delete_supplier(supplier_id)
    flash("Supplier deleted.", "success")
    return redirect(url_for("suppliers.list_suppliers"))


@bp.route("/<int:supplier_id>/products/add", methods=["POST"])
def add_product(supplier_id):
    data = request.form.to_dict()
    supplier_service.add_supplier_product(supplier_id, data)
    flash("Product added to supplier.", "success")
    return redirect(url_for("suppliers.detail", supplier_id=supplier_id))


@bp.route("/<int:supplier_id>/products/<int:sp_id>/delete", methods=["POST"])
def remove_product(supplier_id, sp_id):
    supplier_service.remove_supplier_product(sp_id)
    flash("Removed from supplier.", "success")
    return redirect(url_for("suppliers.detail", supplier_id=supplier_id))


@bp.route("/api/list")
def api_list():
    return jsonify([dict(s) for s in supplier_service.get_all_suppliers()])

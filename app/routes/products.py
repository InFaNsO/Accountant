from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required
from ..services import product_service
from ..services.auth_service import permission_required

bp = Blueprint("products", __name__, url_prefix="/products")


# ── Categories ────────────────────────────────────────────────────────────────

@bp.route("/categories")
@login_required
@permission_required("products", "view")
def list_categories():
    categories = product_service.get_all_categories()
    return render_template("products/categories.html", categories=categories)


@bp.route("/categories/new", methods=["GET", "POST"])
@login_required
@permission_required("products", "create")
def new_category():
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Category name is required.", "error")
            return render_template("products/category_form.html", category=data, action="new")
        product_service.create_category(data)
        flash("Category created.", "success")
        return redirect(url_for("products.list_categories"))
    return render_template("products/category_form.html", category={}, action="new")


@bp.route("/categories/<int:cat_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("products", "edit")
def edit_category(cat_id):
    cat = product_service.get_category(cat_id)
    if not cat:
        flash("Category not found.", "error")
        return redirect(url_for("products.list_categories"))
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Category name is required.", "error")
            return render_template("products/category_form.html", category=data, action="edit", cat_id=cat_id)
        product_service.update_category(cat_id, data)
        flash("Category updated.", "success")
        return redirect(url_for("products.list_categories"))
    return render_template("products/category_form.html", category=dict(cat), action="edit", cat_id=cat_id)


@bp.route("/categories/<int:cat_id>/delete", methods=["POST"])
@login_required
@permission_required("products", "delete")
def delete_category(cat_id):
    product_service.delete_category(cat_id)
    flash("Category deleted.", "success")
    return redirect(url_for("products.list_categories"))


# ── Products ──────────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
@permission_required("products", "view")
def list_products():
    groups = product_service.get_products_with_subs()
    categories = product_service.get_all_categories()
    return render_template("products/list.html", groups=groups, categories=categories)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("products", "create")
def new_product():
    categories = product_service.get_all_categories()
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Product name is required.", "error")
            return render_template("products/form.html", product=data, action="new", categories=categories)
        pid = product_service.create_product(data)
        flash("Product created.", "success")
        return redirect(url_for("products.product_detail", product_id=pid))
    return render_template("products/form.html", product={}, action="new", categories=categories)


@bp.route("/<int:product_id>")
@login_required
@permission_required("products", "view")
def product_detail(product_id):
    product = product_service.get_product(product_id)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("products.list_products"))
    subs = product_service.get_sub_products(product_id)
    product = dict(product)

    if subs:
        # When a product has sub-products, the stock card shows aggregated totals
        product["stock_qty"]      = sum(s["stock_qty"]      or 0 for s in subs)
        product["min_quantity"]   = sum(s["min_quantity"]   or 0 for s in subs)
        product["production_qty"] = sum(s["production_qty"] or 0 for s in subs)
        product["in_transit_qty"] = sum(s["in_transit_qty"] or 0 for s in subs)

    # Only load stock history for leaf products (no sub-products)
    warehouse_history = production_history = transit_history = []
    if not subs:
        warehouse_history  = product_service.get_stock_history(
            product_id=product_id,
            movement_types=["opening", "add", "arrival", "transit_arrival", "correction",
                            "warehouse_add", "warehouse_deduct", "sale", "sale_cancelled"])
        production_history = product_service.get_stock_history(
            product_id=product_id,
            movement_types=["production", "production_add", "production_deduct"])
        transit_history    = product_service.get_stock_history(
            product_id=product_id,
            movement_types=["dispatch", "transit_dispatch", "dispatch_add", "dispatch_deduct"])

    # Eco range: load linked eco product (if main) or main product (if eco)
    eco_product = None
    main_product = None
    from ..database import get_db as _get_db
    db = _get_db()
    if product.get("has_eco_range"):
        eco_row = db.execute("SELECT id, name, sku, unit_price FROM products WHERE eco_parent_id=? AND is_active=1", (product_id,)).fetchone()
        if eco_row:
            eco_product = dict(eco_row)
    elif product.get("eco_parent_id"):
        main_row = db.execute("SELECT id, name FROM products WHERE id=?", (product["eco_parent_id"],)).fetchone()
        if main_row:
            main_product = dict(main_row)

    return render_template("products/detail.html", product=product, subs=subs,
                           warehouse_history=warehouse_history,
                           production_history=production_history,
                           transit_history=transit_history,
                           eco_product=eco_product,
                           main_product=main_product)


@bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("products", "edit")
def edit_product(product_id):
    product = product_service.get_product(product_id)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("products.list_products"))
    categories = product_service.get_all_categories()
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Product name is required.", "error")
            return render_template("products/form.html", product=data, action="edit",
                                   product_id=product_id, categories=categories)
        product_service.update_product(product_id, data)
        flash("Product updated.", "success")
        return redirect(url_for("products.product_detail", product_id=product_id))
    return render_template("products/form.html", product=dict(product), action="edit",
                           product_id=product_id, categories=categories)


@bp.route("/<int:product_id>/delete", methods=["POST"])
@login_required
@permission_required("products", "delete")
def delete_product(product_id):
    product_service.delete_product(product_id)
    flash("Product deleted.", "success")
    return redirect(url_for("products.list_products"))


# ── Eco range ────────────────────────────────────────────────────────────────

@bp.route("/<int:product_id>/eco-range", methods=["POST"])
@login_required
@permission_required("products", "create")
def create_eco_range(product_id):
    from ..database import get_db as _get_db
    unit_price_raw = request.form.get("unit_price", "0")
    sku = request.form.get("sku", "").strip()
    try:
        unit_price = float(unit_price_raw)
    except ValueError:
        flash("Enter a valid eco product price.", "error")
        return redirect(url_for("products.product_detail", product_id=product_id))

    db = _get_db()
    p = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        flash("Product not found.", "error")
        return redirect(url_for("products.list_products"))
    if p["has_eco_range"]:
        flash("This product already has an eco range.", "error")
        return redirect(url_for("products.product_detail", product_id=product_id))
    if p["eco_parent_id"]:
        flash("Eco products cannot have their own eco range.", "error")
        return redirect(url_for("products.product_detail", product_id=product_id))

    eco_sku = sku or (("E-" + p["sku"]) if p["sku"] else None)
    eco_cur = db.execute(
        """INSERT INTO products (category_id, name, sku, description, unit_price, tax_rate,
           track_inventory, stock_qty, min_quantity, pcs_per_carton, is_active, eco_parent_id)
           VALUES (?,?,?,?,?,?,1,0,?,?,1,?)""",
        (p["category_id"], "Eco " + p["name"], eco_sku, p["description"],
         unit_price, p["tax_rate"], p["min_quantity"], p["pcs_per_carton"] or 0, product_id),
    )
    eco_id = eco_cur.lastrowid
    db.execute("UPDATE products SET has_eco_range=1 WHERE id=?", (product_id,))

    main_subs = db.execute(
        "SELECT * FROM sub_products WHERE product_id=? AND is_active=1 ORDER BY id", (product_id,)
    ).fetchall()
    for ms in main_subs:
        eco_sub_sku = (("E-" + ms["sku"]) if ms["sku"] else None)
        db.execute(
            """INSERT INTO sub_products (product_id, name, sku, description, unit_price,
               use_parent_price, tax_rate, track_inventory, stock_qty, min_quantity,
               production_qty, in_transit_qty, is_active, eco_parent_sub_id)
               VALUES (?,?,?,?,0,0,?,1,0,?,0,0,1,?)""",
            (eco_id, "Eco " + ms["name"], eco_sub_sku, ms["description"],
             ms["tax_rate"], ms["min_quantity"] or 0, ms["id"]),
        )
    db.commit()

    sub_count = len(main_subs)
    flash(
        f"Eco range created: 'Eco {p['name']}'" +
        (f" with {sub_count} eco sub-product(s) — set their pricing from the eco product page." if sub_count else " — add stock from the eco product page."),
        "success"
    )
    return redirect(url_for("products.product_detail", product_id=eco_id))


# ── Stock movement (product level) ────────────────────────────────────────────

@bp.route("/<int:product_id>/stock", methods=["POST"])
@login_required
@permission_required("products", "edit")
def stock_action(product_id):
    bucket    = request.form.get("bucket", "warehouse")
    direction = request.form.get("direction", "increase")
    qty_raw   = request.form.get("qty", "0")
    notes     = request.form.get("notes", "")
    try:
        qty = float(qty_raw)
        if qty <= 0:
            raise ValueError
    except ValueError:
        flash("Enter a valid positive quantity.", "error")
        return redirect(url_for("products.product_detail", product_id=product_id))
    try:
        product_service.adjust_stock(product_id, None, bucket, direction, qty, notes)
        bucket_label = {"warehouse": "Warehouse", "production": "Production", "dispatch": "Dispatch"}.get(bucket, bucket)
        flash(f"{bucket_label} stock {'increased' if direction == 'increase' else 'decreased'} by {qty}.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("products.product_detail", product_id=product_id))


# ── Sub-products ──────────────────────────────────────────────────────────────

@bp.route("/<int:product_id>/sub/new", methods=["GET", "POST"])
@login_required
@permission_required("products", "create")
def new_sub_product(product_id):
    product = product_service.get_product(product_id)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("products.list_products"))
    if request.method == "POST":
        data = request.form.to_dict()
        data["product_id"] = product_id
        if not data.get("name"):
            flash("Sub-product name is required.", "error")
            return render_template("products/sub_form.html", sub=data, action="new", product=product)
        if data.get("sku_has_prefix") and product["sku"] and data.get("sku_suffix"):
            data["sku"] = f"{product['sku']}-{data['sku_suffix'].strip()}"
        elif data.get("sku_has_prefix") and product["sku"]:
            data["sku"] = None
        product_service.create_sub_product(data)
        flash("Sub-product created.", "success")
        return redirect(url_for("products.product_detail", product_id=product_id))
    return render_template("products/sub_form.html", sub={}, action="new", product=product)


@bp.route("/<int:product_id>/sub/<int:sub_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("products", "edit")
def edit_sub_product(product_id, sub_id):
    product = product_service.get_product(product_id)
    sub     = product_service.get_sub_product(sub_id)
    if not product or not sub:
        flash("Not found.", "error")
        return redirect(url_for("products.list_products"))
    if request.method == "POST":
        data = request.form.to_dict()
        data["product_id"] = product_id
        if not data.get("name"):
            flash("Sub-product name is required.", "error")
            return render_template("products/sub_form.html", sub=data, action="edit",
                                   product=product, sub_id=sub_id)
        product_service.update_sub_product(sub_id, data)
        flash("Sub-product updated.", "success")
        return redirect(url_for("products.product_detail", product_id=product_id))
    return render_template("products/sub_form.html", sub=dict(sub), action="edit",
                           product=product, sub_id=sub_id)


@bp.route("/<int:product_id>/sub/<int:sub_id>/delete", methods=["POST"])
@login_required
@permission_required("products", "delete")
def delete_sub_product(product_id, sub_id):
    product_service.delete_sub_product(sub_id)
    flash("Sub-product deleted.", "success")
    return redirect(url_for("products.product_detail", product_id=product_id))


@bp.route("/<int:product_id>/sub/<int:sub_id>/stock", methods=["POST"])
@login_required
@permission_required("products", "edit")
def sub_stock_action(product_id, sub_id):
    bucket    = request.form.get("bucket", "warehouse")
    direction = request.form.get("direction", "increase")
    qty_raw   = request.form.get("qty", "0")
    notes     = request.form.get("notes", "")
    from_page = request.form.get("from_page", "product")
    try:
        qty = float(qty_raw)
        if qty <= 0:
            raise ValueError
    except ValueError:
        flash("Enter a valid positive quantity.", "error")
        dest = url_for("products.sub_detail", product_id=product_id, sub_id=sub_id) \
               if from_page == "sub" else \
               url_for("products.product_detail", product_id=product_id)
        return redirect(dest)
    try:
        product_service.adjust_stock(product_id, sub_id, bucket, direction, qty, notes)
        bucket_label = {"warehouse": "Warehouse", "production": "Production", "dispatch": "Dispatch"}.get(bucket, bucket)
        flash(f"{bucket_label} stock {'increased' if direction == 'increase' else 'decreased'} by {qty}.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    if from_page == "sub":
        return redirect(url_for("products.sub_detail", product_id=product_id, sub_id=sub_id))
    return redirect(url_for("products.product_detail", product_id=product_id))


# ── Sub-product detail page ───────────────────────────────────────────────────

@bp.route("/<int:product_id>/sub/<int:sub_id>")
@login_required
@permission_required("products", "view")
def sub_detail(product_id, sub_id):
    product = product_service.get_product(product_id)
    sub     = product_service.get_sub_product(sub_id)
    if not product or not sub:
        flash("Not found.", "error")
        return redirect(url_for("products.list_products"))
    warehouse_history  = product_service.get_stock_history(
        sub_id=sub_id,
        movement_types=["opening", "add", "arrival", "transit_arrival", "correction",
                        "warehouse_add", "warehouse_deduct", "sale", "sale_cancelled"])
    production_history = product_service.get_stock_history(
        sub_id=sub_id,
        movement_types=["production", "production_add", "production_deduct"])
    transit_history    = product_service.get_stock_history(
        sub_id=sub_id,
        movement_types=["dispatch", "transit_dispatch", "dispatch_add", "dispatch_deduct"])
    return render_template("products/sub_detail.html",
                           product=product, sub=sub,
                           warehouse_history=warehouse_history,
                           production_history=production_history,
                           transit_history=transit_history)


# ── Inline sub-product save ───────────────────────────────────────────────────

@bp.route("/<int:product_id>/sub/<int:sub_id>/inline", methods=["POST"])
@login_required
@permission_required("products", "edit")
def inline_sub_save(product_id, sub_id):
    data = request.get_json(silent=True) or {}
    allowed = {"name", "unit_price", "tax_rate", "min_quantity", "use_parent_price"}
    update = {k: v for k, v in data.items() if k in allowed}
    if not update.get("name"):
        return jsonify({"ok": False, "error": "Name is required"}), 400
    product_service.update_sub_product(sub_id, update)
    sub = product_service.get_sub_product(sub_id)
    return jsonify({"ok": True, "sub": dict(sub)})


# ── JSON API for invoice builder ──────────────────────────────────────────────

@bp.route("/api/list")
@login_required
@permission_required("products", "view")
def api_list():
    products = product_service.get_all_products(active_only=True)
    result = []
    for p in products:
        subs = product_service.get_sub_products(p["id"])
        if subs:
            for s in subs:
                if not s["is_active"]:
                    continue
                result.append({
                    "id":         f"sub_{s['id']}",
                    "name":       f"{p['name']} — {s['name']}",
                    "sku":        s["sku"],
                    "unit_price": p["unit_price"] if s["use_parent_price"] else s["unit_price"],
                    "tax_rate":   p["tax_rate"],
                    "type":       "sub_product",
                })
        else:
            result.append({
                "id":         p["id"],
                "name":       p["name"],
                "sku":        p["sku"],
                "unit_price": p["unit_price"],
                "tax_rate":   p["tax_rate"],
                "type":       "product",
            })
    return jsonify(result)

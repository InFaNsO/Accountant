from ..database import get_db


def _to_float(val, default=0.0):
    """Convert form value to float, tolerating commas and empty strings."""
    if val is None or str(val).strip() == "":
        return default
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


# ── Categories ────────────────────────────────────────────────────────────────

def get_all_categories():
    return get_db().execute("SELECT * FROM categories ORDER BY name").fetchall()


def get_category(cat_id):
    return get_db().execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()


def create_category(data):
    db = get_db()
    cur = db.execute(
        "INSERT INTO categories (name, description) VALUES (?,?)",
        (data["name"], data.get("description")),
    )
    db.commit()
    return cur.lastrowid


def update_category(cat_id, data):
    db = get_db()
    db.execute("UPDATE categories SET name=?, description=? WHERE id=?",
               (data["name"], data.get("description"), cat_id))
    db.commit()


def delete_category(cat_id):
    db = get_db()
    db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    db.commit()


# ── Products ──────────────────────────────────────────────────────────────────

def get_all_products(active_only=False):
    q = """SELECT p.*, c.name AS category_name
           FROM products p LEFT JOIN categories c ON p.category_id = c.id"""
    if active_only:
        q += " WHERE p.is_active=1"
    q += " ORDER BY c.name, p.name"
    return get_db().execute(q).fetchall()


def get_products_with_subs(active_only=False):
    """Return products grouped with their sub-products."""
    products = get_all_products(active_only)
    result = []
    for p in products:
        subs = get_sub_products(p["id"])
        result.append({"product": p, "subs": subs})
    return result


def get_product(product_id):
    return get_db().execute(
        """SELECT p.*, c.name AS category_name
           FROM products p LEFT JOIN categories c ON p.category_id=c.id
           WHERE p.id=?""",
        (product_id,),
    ).fetchone()


def create_product(data):
    db = get_db()
    opening_qty = _to_float(data.get("stock_qty"))
    cur = db.execute(
        """INSERT INTO products
               (category_id, name, sku, description, unit_price, tax_rate,
                track_inventory, stock_qty, min_quantity, is_active)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (
            data.get("category_id") or None,
            data["name"], data.get("sku"), data.get("description"),
            _to_float(data.get("unit_price")),
            _to_float(data.get("tax_rate"), 18.0),
            1,
            opening_qty,
            _to_float(data.get("min_quantity")),
        ),
    )
    product_id = cur.lastrowid
    if opening_qty > 0:
        db.execute(
            "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes) VALUES (?,?,?,?,?)",
            (product_id, None, "opening", opening_qty, "Opening stock"),
        )
    db.commit()
    return product_id


def update_product(product_id, data):
    db = get_db()
    new_tax = _to_float(data.get("tax_rate"), 18.0)
    db.execute(
        """UPDATE products SET category_id=?, name=?, sku=?, description=?,
               unit_price=?, tax_rate=?, track_inventory=1,
               min_quantity=?, is_active=? WHERE id=?""",
        (
            data.get("category_id") or None,
            data["name"], data.get("sku"), data.get("description"),
            _to_float(data.get("unit_price")),
            new_tax,
            _to_float(data.get("min_quantity")),
            1 if data.get("is_active", True) else 0,
            product_id,
        ),
    )
    # Keep sub-product tax rates in sync with parent
    db.execute("UPDATE sub_products SET tax_rate=? WHERE product_id=?", (new_tax, product_id))
    db.commit()


def delete_product(product_id):
    db = get_db()
    # Delete movements for all sub-products first, then sub-products, then product
    db.execute(
        "DELETE FROM stock_movements WHERE sub_product_id IN "
        "(SELECT id FROM sub_products WHERE product_id=?)", (product_id,)
    )
    db.execute("DELETE FROM stock_movements WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM sub_products WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM products WHERE id=?", (product_id,))
    db.commit()


# ── Sub-products ──────────────────────────────────────────────────────────────

def get_sub_products(product_id):
    return get_db().execute(
        "SELECT * FROM sub_products WHERE product_id=? ORDER BY name",
        (product_id,),
    ).fetchall()


def get_sub_product(sub_id):
    return get_db().execute(
        "SELECT s.*, p.name AS parent_name, p.unit_price AS parent_price, p.min_quantity AS parent_min_qty "
        "FROM sub_products s JOIN products p ON s.product_id=p.id WHERE s.id=?",
        (sub_id,),
    ).fetchone()


def create_sub_product(data):
    db = get_db()
    use_parent = 1 if data.get("use_parent_price") else 0
    parent = get_product(data["product_id"])
    parent_tax = _to_float(parent["tax_rate"], 18.0) if parent else 18.0
    opening_qty = _to_float(data.get("stock_qty"))
    cur = db.execute(
        """INSERT INTO sub_products
               (product_id, name, sku, description, use_parent_price, unit_price,
                tax_rate, track_inventory, stock_qty, min_quantity, is_active)
           VALUES (?,?,?,?,?,?,?,1,?,?,1)""",
        (
            data["product_id"], data["name"], data.get("sku"), data.get("description"),
            use_parent,
            _to_float(data.get("unit_price")),
            parent_tax,
            opening_qty,
            _to_float(data.get("min_quantity")),
        ),
    )
    sub_id = cur.lastrowid
    if opening_qty > 0:
        db.execute(
            "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes) VALUES (?,?,?,?,?)",
            (data["product_id"], sub_id, "opening", opening_qty, "Opening stock"),
        )
    db.commit()
    return sub_id


def update_sub_product(sub_id, data):
    db = get_db()
    use_parent = 1 if data.get("use_parent_price") else 0
    sub_row = get_db().execute("SELECT product_id FROM sub_products WHERE id=?", (sub_id,)).fetchone()
    parent = get_product(sub_row["product_id"]) if sub_row else None
    parent_tax = _to_float(parent["tax_rate"], 18.0) if parent else 18.0
    db.execute(
        """UPDATE sub_products SET name=?, sku=?, description=?, use_parent_price=?,
               unit_price=?, tax_rate=?, min_quantity=?, is_active=? WHERE id=?""",
        (
            data["name"], data.get("sku"), data.get("description"),
            use_parent, _to_float(data.get("unit_price")),
            parent_tax,
            _to_float(data.get("min_quantity")),
            1 if data.get("is_active", True) else 0,
            sub_id,
        ),
    )
    db.commit()


def delete_sub_product(sub_id):
    db = get_db()
    db.execute("DELETE FROM stock_movements WHERE sub_product_id=?", (sub_id,))
    db.execute("DELETE FROM sub_products WHERE id=?", (sub_id,))
    db.commit()


# ── Stock movements ───────────────────────────────────────────────────────────

def add_stock(product_id, sub_id, qty, notes=""):
    """Add to available stock."""
    db = get_db()
    if sub_id:
        db.execute("UPDATE sub_products SET stock_qty=stock_qty+? WHERE id=?", (qty, sub_id))
    else:
        db.execute("UPDATE products SET stock_qty=stock_qty+? WHERE id=?", (qty, product_id))
    db.execute(
        "INSERT INTO stock_movements (product_id,sub_product_id,movement_type,quantity,notes) VALUES (?,?,'add',?,?)",
        (product_id or None, sub_id or None, qty, notes),
    )
    db.commit()


def send_to_production(product_id, sub_id, qty, notes=""):
    """Move qty from available → production."""
    db = get_db()
    if sub_id:
        db.execute(
            "UPDATE sub_products SET stock_qty=stock_qty-?, production_qty=production_qty+? WHERE id=?",
            (qty, qty, sub_id),
        )
    else:
        db.execute(
            "UPDATE products SET stock_qty=stock_qty-?, production_qty=production_qty+? WHERE id=?",
            (qty, qty, product_id),
        )
    db.execute(
        "INSERT INTO stock_movements (product_id,sub_product_id,movement_type,quantity,notes) VALUES (?,?,'production',?,?)",
        (product_id or None, sub_id or None, qty, notes),
    )
    db.commit()


def dispatch_from_production(product_id, sub_id, qty, expected_arrival, notes=""):
    """Move qty from production → in-transit."""
    db = get_db()
    if sub_id:
        db.execute(
            "UPDATE sub_products SET production_qty=production_qty-?, in_transit_qty=in_transit_qty+? WHERE id=?",
            (qty, qty, sub_id),
        )
    else:
        db.execute(
            "UPDATE products SET production_qty=production_qty-?, in_transit_qty=in_transit_qty+? WHERE id=?",
            (qty, qty, product_id),
        )
    db.execute(
        """INSERT INTO stock_movements
               (product_id, sub_product_id, movement_type, quantity, notes, expected_arrival)
           VALUES (?,?,'dispatch',?,?,?)""",
        (product_id or None, sub_id or None, qty, notes, expected_arrival),
    )
    db.commit()


def mark_arrived(product_id, sub_id, qty, notes=""):
    """Move qty from in-transit → available stock."""
    db = get_db()
    if sub_id:
        db.execute(
            "UPDATE sub_products SET in_transit_qty=in_transit_qty-?, stock_qty=stock_qty+? WHERE id=?",
            (qty, qty, sub_id),
        )
    else:
        db.execute(
            "UPDATE products SET in_transit_qty=in_transit_qty-?, stock_qty=stock_qty+? WHERE id=?",
            (qty, qty, product_id),
        )
    db.execute(
        "INSERT INTO stock_movements (product_id,sub_product_id,movement_type,quantity,notes) VALUES (?,?,'arrival',?,?)",
        (product_id or None, sub_id or None, qty, notes),
    )
    db.commit()


def adjust_stock(product_id, sub_id, bucket, direction, qty, notes=""):
    """Directly adjust one stock bucket by qty.
    bucket: 'warehouse' | 'production' | 'dispatch'
    direction: 'increase' | 'decrease'
    """
    db = get_db()
    field_map = {
        'warehouse':  'stock_qty',
        'production': 'production_qty',
        'dispatch':   'in_transit_qty',
    }
    field = field_map.get(bucket, 'stock_qty')
    delta = qty if direction == 'increase' else -qty
    tbl = "sub_products" if sub_id else "products"
    pk  = sub_id if sub_id else product_id
    db.execute(f"UPDATE {tbl} SET {field}={field}+? WHERE id=?", (delta, pk))
    movement_type = f"{bucket}_{'add' if direction == 'increase' else 'deduct'}"
    db.execute(
        "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes) VALUES (?,?,?,?,?)",
        (product_id or None, sub_id or None, movement_type, qty, notes or None),
    )
    db.commit()


def get_stock_movements(product_id=None, sub_id=None, limit=50):
    db = get_db()
    if sub_id:
        return db.execute(
            "SELECT * FROM stock_movements WHERE sub_product_id=? ORDER BY created_at DESC LIMIT ?",
            (sub_id, limit),
        ).fetchall()
    return db.execute(
        "SELECT * FROM stock_movements WHERE product_id=? ORDER BY created_at DESC LIMIT ?",
        (product_id, limit),
    ).fetchall()


def get_stock_history(product_id=None, sub_id=None, movement_types=None, limit=100):
    """Return enriched stock movement history joined with dispatches/invoices/clients."""
    db = get_db()
    where_parts = []
    params = []
    if sub_id:
        where_parts.append("sm.sub_product_id=?")
        params.append(sub_id)
    elif product_id:
        where_parts.append("sm.product_id=?")
        params.append(product_id)
    if movement_types:
        placeholders = ",".join("?" for _ in movement_types)
        where_parts.append(f"sm.movement_type IN ({placeholders})")
        params.extend(movement_types)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)
    rows = db.execute(
        f"""SELECT sm.*,
                   d.name  AS dispatch_name,
                   i.invoice_number,
                   c.name  AS client_name
            FROM stock_movements sm
            LEFT JOIN dispatches d  ON sm.dispatch_id = d.id
            LEFT JOIN invoices   i  ON sm.invoice_id  = i.id
            LEFT JOIN clients    c  ON i.client_id    = c.id
            {where_clause}
            ORDER BY sm.created_at DESC
            LIMIT ?""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ── Dashboard stock alerts ────────────────────────────────────────────────────

def get_stock_alerts():
    db = get_db()

    # Products below minimum
    low_products = db.execute(
        """SELECT p.*, c.name AS category_name
           FROM products p LEFT JOIN categories c ON p.category_id=c.id
           WHERE p.track_inventory=1 AND p.min_quantity>0 AND p.stock_qty < p.min_quantity
           ORDER BY (p.stock_qty - p.min_quantity) ASC""",
    ).fetchall()

    low_subs = db.execute(
        """SELECT s.*, p.name AS parent_name, p.min_quantity AS parent_min_qty,
                  c.name AS category_name
           FROM sub_products s
           JOIN products p ON s.product_id=p.id
           LEFT JOIN categories c ON p.category_id=c.id
           WHERE s.track_inventory=1
             AND (CASE WHEN s.min_quantity>0 THEN s.min_quantity ELSE p.min_quantity END) > 0
             AND s.stock_qty < (CASE WHEN s.min_quantity>0 THEN s.min_quantity ELSE p.min_quantity END)
           ORDER BY s.stock_qty ASC""",
    ).fetchall()

    # In production
    in_production_p = db.execute(
        "SELECT p.*, c.name AS category_name FROM products p LEFT JOIN categories c ON p.category_id=c.id WHERE p.production_qty>0"
    ).fetchall()
    in_production_s = db.execute(
        "SELECT s.*, p.name AS parent_name FROM sub_products s JOIN products p ON s.product_id=p.id WHERE s.production_qty>0"
    ).fetchall()

    # In transit
    in_transit_p = db.execute(
        """SELECT p.*, c.name AS category_name,
                  m.expected_arrival, m.quantity AS dispatch_qty
           FROM products p
           LEFT JOIN categories c ON p.category_id=c.id
           JOIN stock_movements m ON m.product_id=p.id AND m.sub_product_id IS NULL
                                 AND m.movement_type IN ('dispatch', 'transit_dispatch')
           WHERE p.in_transit_qty>0
           GROUP BY p.id ORDER BY m.expected_arrival ASC"""
    ).fetchall()
    in_transit_s = db.execute(
        """SELECT s.*, p.name AS parent_name,
                  m.expected_arrival, m.quantity AS dispatch_qty
           FROM sub_products s
           JOIN products p ON s.product_id=p.id
           JOIN stock_movements m ON m.sub_product_id=s.id
                                 AND m.movement_type IN ('dispatch', 'transit_dispatch')
           WHERE s.in_transit_qty>0
           GROUP BY s.id ORDER BY m.expected_arrival ASC"""
    ).fetchall()

    return {
        "low_stock":     [dict(r) for r in low_products] + [dict(r) for r in low_subs],
        "in_production": [dict(r) for r in in_production_p] + [dict(r) for r in in_production_s],
        "in_transit":    [dict(r) for r in in_transit_p] + [dict(r) for r in in_transit_s],
    }

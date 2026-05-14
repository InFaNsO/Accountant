from ..database import get_db


def _update_production_qty(db, product_id, sub_product_id, delta):
    if sub_product_id:
        db.execute(
            "UPDATE sub_products SET production_qty=production_qty+? WHERE id=?",
            (delta, sub_product_id),
        )
    else:
        db.execute(
            "UPDATE products SET production_qty=production_qty+? WHERE id=?",
            (delta, product_id),
        )


# ── Purchase orders ───────────────────────────────────────────────────────────

def get_all_purchase_orders():
    return get_db().execute(
        """SELECT po.*, s.name AS supplier_name,
                  COUNT(poi.id) AS item_count
           FROM purchase_orders po
           LEFT JOIN suppliers s ON po.supplier_id=s.id
           LEFT JOIN purchase_order_items poi ON poi.po_id=po.id
           GROUP BY po.id
           ORDER BY po.expected_completion ASC, po.created_at DESC"""
    ).fetchall()


def get_purchase_order(po_id):
    return get_db().execute(
        """SELECT po.*, s.name AS supplier_name
           FROM purchase_orders po
           LEFT JOIN suppliers s ON po.supplier_id=s.id
           WHERE po.id=?""",
        (po_id,),
    ).fetchone()


def get_po_items(po_id):
    return get_db().execute(
        """SELECT poi.*,
                  CASE WHEN poi.sub_product_id IS NOT NULL
                       THEN par.name || ' — ' || sub.name
                       ELSE p.name END AS display_name,
                  p.sku AS product_sku, sub.sku AS sub_sku
           FROM purchase_order_items poi
           LEFT JOIN products p      ON poi.product_id=p.id
           LEFT JOIN sub_products sub ON poi.sub_product_id=sub.id
           LEFT JOIN products par    ON sub.product_id=par.id
           WHERE poi.po_id=?
           ORDER BY poi.id""",
        (po_id,),
    ).fetchall()


def create_purchase_order(data, items):
    """Create PO and add quantities to production_qty."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO purchase_orders (name, supplier_id, expected_completion, status, notes)
           VALUES (?,?,?,?,?)""",
        (data["name"],
         data.get("supplier_id") or None,
         data.get("expected_completion") or None,
         "open",
         data.get("notes")),
    )
    po_id = cur.lastrowid

    for it in items:
        product_id     = int(it["product_id"]) if it.get("product_id") else None
        sub_product_id = int(it["sub_product_id"]) if it.get("sub_product_id") else None
        qty   = float(it["quantity"])
        price = float(it["price"]) if it.get("price") else None
        db.execute(
            """INSERT INTO purchase_order_items
                   (po_id, product_id, sub_product_id, quantity, price)
               VALUES (?,?,?,?,?)""",
            (po_id, product_id, sub_product_id, qty, price),
        )
        _update_production_qty(db, product_id, sub_product_id, qty)

    db.commit()
    return po_id


def update_purchase_order(po_id, data):
    db = get_db()
    db.execute(
        """UPDATE purchase_orders SET name=?, supplier_id=?, expected_completion=?, notes=?
           WHERE id=?""",
        (data["name"],
         data.get("supplier_id") or None,
         data.get("expected_completion") or None,
         data.get("notes"),
         po_id),
    )
    db.commit()


def close_purchase_order(po_id):
    db = get_db().execute(
        "UPDATE purchase_orders SET status='closed' WHERE id=?", (po_id,)
    )
    get_db().commit()


def delete_purchase_order(po_id):
    """Delete PO and reverse any undispatched production quantities."""
    db = get_db()
    items = get_po_items(po_id)
    for it in items:
        undispatched = it["quantity"] - (it["qty_dispatched"] or 0)
        if undispatched > 0:
            _update_production_qty(db, it["product_id"], it["sub_product_id"], -undispatched)
    db.execute("DELETE FROM purchase_orders WHERE id=?", (po_id,))
    db.commit()


def get_pos_due_soon(days=14):
    return get_db().execute(
        """SELECT po.*, s.name AS supplier_name,
                  COUNT(poi.id) AS item_count,
                  julianday(po.expected_completion) - julianday('now') AS days_remaining
           FROM purchase_orders po
           LEFT JOIN suppliers s ON po.supplier_id=s.id
           LEFT JOIN purchase_order_items poi ON poi.po_id=po.id
           WHERE po.status='open'
             AND po.expected_completion IS NOT NULL
             AND po.expected_completion <= date('now', ?||' days')
           GROUP BY po.id
           ORDER BY po.expected_completion ASC""",
        (f"+{days}",),
    ).fetchall()

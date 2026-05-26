"""
Palm Purchase service — instant warehouse stock-in.

A palm purchase represents an immediate, paid-on-the-spot procurement: when
created, it increments warehouse stock_qty for each line item and records a
'palm_purchase' stock_movement for the audit trail. Deleting reverses both.

This differs from production purchase orders (purchases / production_orders)
which increment production_qty (pending), then move through dispatches.
"""
from datetime import date
from ..database import get_db


# ── Read ─────────────────────────────────────────────────────────────────────

def get_all_palm_purchases():
    return get_db().execute(
        """SELECT pp.*,
                  s.name AS supplier_name,
                  COUNT(ppi.id) AS item_count,
                  COALESCE(SUM(ppi.quantity), 0) AS total_qty
           FROM palm_purchases pp
           LEFT JOIN suppliers s ON pp.supplier_id = s.id
           LEFT JOIN palm_purchase_items ppi ON ppi.palm_purchase_id = pp.id
           GROUP BY pp.id
           ORDER BY pp.purchase_date DESC, pp.created_at DESC"""
    ).fetchall()


def get_palm_purchase(pp_id):
    return get_db().execute(
        """SELECT pp.*, s.name AS supplier_name
           FROM palm_purchases pp
           LEFT JOIN suppliers s ON pp.supplier_id = s.id
           WHERE pp.id = ?""",
        (pp_id,),
    ).fetchone()


def get_palm_purchase_items(pp_id):
    return get_db().execute(
        """SELECT ppi.*,
                  CASE WHEN ppi.sub_product_id IS NOT NULL
                       THEN par.name || ' — ' || sub.name
                       ELSE p.name END                       AS display_name,
                  p.sku AS product_sku, sub.sku AS sub_sku
           FROM palm_purchase_items ppi
           LEFT JOIN products p       ON ppi.product_id = p.id
           LEFT JOIN sub_products sub ON ppi.sub_product_id = sub.id
           LEFT JOIN products par     ON sub.product_id = par.id
           WHERE ppi.palm_purchase_id = ?
           ORDER BY ppi.id""",
        (pp_id,),
    ).fetchall()


# ── Write ────────────────────────────────────────────────────────────────────

def _bump_stock(db, product_id, sub_product_id, delta):
    """Add `delta` (can be negative for reversal) to warehouse stock_qty."""
    if sub_product_id:
        db.execute(
            "UPDATE sub_products SET stock_qty = stock_qty + ? WHERE id = ?",
            (delta, sub_product_id),
        )
    elif product_id:
        db.execute(
            "UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
            (delta, product_id),
        )


def create_palm_purchase(data, items):
    """Create a palm purchase, bump warehouse stock_qty, log movements.

    `data` keys: name, supplier_id, purchase_date, notes
    `items`: list of dicts {product_id, sub_product_id, quantity, unit_cost, notes}

    Returns the new palm_purchase id.
    Raises ValueError if no valid items.
    """
    db = get_db()
    clean_items = []
    total_cost = 0.0
    for it in items:
        try:
            qty = float(it.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        pid  = int(it["product_id"])     if it.get("product_id")     else None
        spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
        if not pid and not spid:
            continue
        try:
            unit_cost = float(it.get("unit_cost") or 0)
        except (TypeError, ValueError):
            unit_cost = 0.0
        clean_items.append({
            "product_id":     pid,
            "sub_product_id": spid,
            "quantity":       qty,
            "unit_cost":      unit_cost,
            "notes":          (it.get("notes") or None),
        })
        total_cost += qty * unit_cost

    if not clean_items:
        raise ValueError("Palm purchase needs at least one product line with quantity > 0.")

    cur = db.execute(
        """INSERT INTO palm_purchases (name, supplier_id, purchase_date, notes, total_cost)
           VALUES (?, ?, ?, ?, ?)""",
        (
            (data.get("name") or "").strip() or None,
            int(data["supplier_id"]) if data.get("supplier_id") else None,
            data.get("purchase_date") or str(date.today()),
            (data.get("notes") or "").strip() or None,
            total_cost,
        ),
    )
    pp_id = cur.lastrowid

    for it in clean_items:
        db.execute(
            """INSERT INTO palm_purchase_items
                  (palm_purchase_id, product_id, sub_product_id, quantity, unit_cost, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (pp_id, it["product_id"], it["sub_product_id"],
             it["quantity"], it["unit_cost"], it["notes"]),
        )
        _bump_stock(db, it["product_id"], it["sub_product_id"], it["quantity"])
        note = f"Palm Purchase #{pp_id}" + (f" — {data.get('name')}" if data.get("name") else "")
        db.execute(
            """INSERT INTO stock_movements
                  (product_id, sub_product_id, movement_type, quantity, notes, palm_purchase_id)
               VALUES (?, ?, 'palm_purchase', ?, ?, ?)""",
            (it["product_id"], it["sub_product_id"], it["quantity"], note, pp_id),
        )

    db.commit()
    return pp_id


def delete_palm_purchase(pp_id):
    """Reverse stock_qty for each item, log reversal movements, delete rows."""
    db = get_db()
    items = db.execute(
        "SELECT product_id, sub_product_id, quantity FROM palm_purchase_items WHERE palm_purchase_id = ?",
        (pp_id,),
    ).fetchall()
    for it in items:
        _bump_stock(db, it["product_id"], it["sub_product_id"], -float(it["quantity"]))
        db.execute(
            """INSERT INTO stock_movements
                  (product_id, sub_product_id, movement_type, quantity, notes, palm_purchase_id)
               VALUES (?, ?, 'palm_purchase_reversed', ?, ?, ?)""",
            (it["product_id"], it["sub_product_id"], it["quantity"],
             f"Palm Purchase #{pp_id} deleted (stock reversed)", pp_id),
        )
    db.execute("DELETE FROM palm_purchases WHERE id = ?", (pp_id,))
    db.commit()

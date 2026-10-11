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


# ── Production orders ─────────────────────────────────────────────────────────

def get_all_production_orders(statuses=None):
    """Production orders ordered by expected completion, nearest first (undated last).

    statuses: optional iterable of status values to include. None = all statuses.
    """
    where, params = "", []
    if statuses:
        statuses = list(statuses)
        ph = ",".join("?" * len(statuses))
        where = f"WHERE po.status IN ({ph})"
        params = statuses
    return get_db().execute(
        f"""SELECT po.*, s.name AS supplier_name,
                   COUNT(poi.id) AS item_count
            FROM purchase_orders po
            LEFT JOIN suppliers s ON po.supplier_id=s.id
            LEFT JOIN purchase_order_items poi ON poi.po_id=po.id
            {where}
            GROUP BY po.id
            ORDER BY (po.expected_completion IS NULL), po.expected_completion ASC, po.created_at DESC""",
        params,
    ).fetchall()


def get_production_order(po_id):
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
                       WHEN poi.product_name IS NOT NULL
                       THEN poi.product_name
                       ELSE p.name END AS display_name,
                  p.sku AS product_sku, sub.sku AS sub_sku,
                  p.pcs_per_carton
           FROM purchase_order_items poi
           LEFT JOIN products p      ON poi.product_id=p.id
           LEFT JOIN sub_products sub ON poi.sub_product_id=sub.id
           LEFT JOIN products par    ON sub.product_id=par.id
           WHERE poi.po_id=?
           ORDER BY poi.id""",
        (po_id,),
    ).fetchall()


def create_production_order(data, items):
    """Create a production order.

    A normal order (status 'open') immediately adds each line's quantity to
    production_qty. A draft (data['status'] == 'draft') records the order and its
    items but holds NO stock — quantities are applied later by
    activate_production_order().
    """
    db = get_db()
    is_draft = (data.get("status") == "draft")
    cur = db.execute(
        """INSERT INTO purchase_orders (name, supplier_id, expected_completion, status, notes)
           VALUES (?,?,?,?,?)""",
        (data["name"],
         data.get("supplier_id") or None,
         data.get("expected_completion") or None,
         "draft" if is_draft else "open",
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
        if not is_draft:
            _update_production_qty(db, product_id, sub_product_id, qty)

    db.commit()
    return po_id


def activate_production_order(po_id):
    """Promote a draft order to 'open', adding each line's quantity to production_qty.

    Returns True if a draft was activated, False otherwise (already active / missing).
    """
    db = get_db()
    po = db.execute("SELECT status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po or po["status"] != "draft":
        return False
    for it in get_po_items(po_id):
        _update_production_qty(db, it["product_id"], it["sub_product_id"], it["quantity"])
    db.execute("UPDATE purchase_orders SET status='open' WHERE id=?", (po_id,))
    db.commit()
    return True


def update_production_order(po_id, data, item_updates=None, new_items=None):
    """Update the header and (optionally) the line items.

    item_updates: list of {"id", "quantity", "price"} for existing po_items.
        The ordered quantity is clamped so it can never drop below the amount
        already dispatched (in transit / received) — i.e. >= qty_dispatched.
        production_qty is adjusted by the change in the *undispatched* remainder.
    new_items: list of {"product_id", "sub_product_id", "quantity", "price"} —
        brand-new lines, added exactly like create_production_order does.
    """
    db = get_db()
    po = db.execute("SELECT status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    # A draft holds no production stock, so edits to a draft never touch production_qty.
    is_draft = bool(po and po["status"] == "draft")
    db.execute(
        """UPDATE purchase_orders SET name=?, supplier_id=?, expected_completion=?, notes=?
           WHERE id=?""",
        (data["name"],
         data.get("supplier_id") or None,
         data.get("expected_completion") or None,
         data.get("notes"),
         po_id),
    )

    if item_updates:
        existing = {row["id"]: row for row in get_po_items(po_id)}
        for upd in item_updates:
            it = existing.get(upd["id"])
            if not it:
                continue
            try:
                new_qty = float(upd["quantity"])
            except (TypeError, ValueError):
                continue
            dispatched = it["qty_dispatched"] or 0
            # Never below what's already dispatched (in transit / received).
            if new_qty < dispatched:
                new_qty = dispatched
            raw_price = upd.get("price")
            price = float(raw_price) if raw_price not in (None, "") else None
            delta = new_qty - it["quantity"]
            db.execute(
                "UPDATE purchase_order_items SET quantity=?, price=? WHERE id=?",
                (new_qty, price, upd["id"]),
            )
            if delta and not is_draft:
                _update_production_qty(db, it["product_id"], it["sub_product_id"], delta)

    if new_items:
        for it in new_items:
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
            if not is_draft:
                _update_production_qty(db, product_id, sub_product_id, qty)

    db.commit()


def close_production_order(po_id):
    get_db().execute(
        "UPDATE purchase_orders SET status='closed' WHERE id=?", (po_id,)
    )
    get_db().commit()


def delete_production_order(po_id):
    """Delete production order and reverse any undispatched production quantities.

    A draft never added to production_qty, so nothing is reversed for it.
    """
    db = get_db()
    po = db.execute("SELECT status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    is_draft = bool(po and po["status"] == "draft")
    if not is_draft:
        for it in get_po_items(po_id):
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

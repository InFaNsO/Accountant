from ..database import get_db


def _update_qty(db, product_id, sub_product_id, field, delta):
    tbl = "sub_products" if sub_product_id else "products"
    pk  = sub_product_id if sub_product_id else product_id
    db.execute(f"UPDATE {tbl} SET {field}={field}+? WHERE id=?", (delta, pk))


# ── FIFO deduction from purchase order items ─────────────────────────────────

def _deduct_production_fifo(db, product_id, sub_product_id, qty_needed):
    """Reduce oldest open PO items by qty_needed. Returns list of (po_item_id, qty_taken)."""
    rows = db.execute(
        """SELECT poi.id, poi.quantity, poi.qty_dispatched
           FROM purchase_order_items poi
           JOIN purchase_orders po ON poi.po_id=po.id
           WHERE po.status='open'
             AND (poi.product_id IS ? OR (poi.product_id IS NULL AND ? IS NULL))
             AND (poi.sub_product_id IS ? OR (poi.sub_product_id IS NULL AND ? IS NULL))
             AND poi.quantity > poi.qty_dispatched
           ORDER BY po.created_at ASC, poi.id ASC""",
        (product_id, product_id, sub_product_id, sub_product_id),
    ).fetchall()

    allocations = []
    remaining   = qty_needed
    for row in rows:
        if remaining <= 0:
            break
        available = row["quantity"] - row["qty_dispatched"]
        take      = min(available, remaining)
        db.execute(
            "UPDATE purchase_order_items SET qty_dispatched=qty_dispatched+? WHERE id=?",
            (take, row["id"]),
        )
        allocations.append((row["id"], take))
        remaining -= take
        # Auto-close PO if all its items are fully dispatched
        po_row = db.execute(
            """SELECT po_id FROM purchase_order_items WHERE id=?""", (row["id"],)
        ).fetchone()
        if po_row:
            undone = db.execute(
                """SELECT COUNT(*) AS c FROM purchase_order_items
                   WHERE po_id=? AND qty_dispatched < quantity""",
                (po_row["po_id"],),
            ).fetchone()["c"]
            if undone == 0:
                db.execute(
                    "UPDATE purchase_orders SET status='closed' WHERE id=?",
                    (po_row["po_id"],),
                )
    return allocations, remaining  # remaining > 0 means insufficient production


# ── Dispatches ────────────────────────────────────────────────────────────────

def get_all_dispatches():
    return get_db().execute(
        """SELECT d.*, s.name AS supplier_name,
                  COUNT(di.id) AS item_count
           FROM dispatches d
           LEFT JOIN suppliers s  ON d.supplier_id=s.id
           LEFT JOIN dispatch_items di ON di.dispatch_id=d.id
           GROUP BY d.id
           ORDER BY d.expected_arrival ASC, d.created_at DESC"""
    ).fetchall()


def get_dispatch(dispatch_id):
    return get_db().execute(
        """SELECT d.*, s.name AS supplier_name
           FROM dispatches d
           LEFT JOIN suppliers s ON d.supplier_id=s.id
           WHERE d.id=?""",
        (dispatch_id,),
    ).fetchone()


def get_dispatch_items(dispatch_id):
    return get_db().execute(
        """SELECT di.*,
                  CASE WHEN di.sub_product_id IS NOT NULL
                       THEN par.name || ' — ' || sub.name
                       ELSE p.name END AS display_name,
                  p.sku AS product_sku, sub.sku AS sub_sku
           FROM dispatch_items di
           LEFT JOIN products p       ON di.product_id=p.id
           LEFT JOIN sub_products sub ON di.sub_product_id=sub.id
           LEFT JOIN products par     ON sub.product_id=par.id
           WHERE di.dispatch_id=?
           ORDER BY di.id""",
        (dispatch_id,),
    ).fetchall()


def create_dispatch(data, items):
    """
    Create dispatch, run FIFO deduction from PO items, move qty from
    production_qty → in_transit_qty for each product/sub-product.
    """
    db = get_db()
    cur = db.execute(
        """INSERT INTO dispatches (name, supplier_id, dispatch_date, expected_arrival, notes)
           VALUES (?,?,?,?,?)""",
        (data["name"],
         data.get("supplier_id") or None,
         data.get("dispatch_date") or None,
         data.get("expected_arrival") or None,
         data.get("notes")),
    )
    dispatch_id = cur.lastrowid

    warnings = []
    for it in items:
        product_id     = int(it["product_id"]) if it.get("product_id") else None
        sub_product_id = int(it["sub_product_id"]) if it.get("sub_product_id") else None
        qty          = float(it["quantity"])
        price        = float(it["price"]) if it.get("price") else None
        cbm          = float(it["cbm"]) if it.get("cbm") else None
        gross_weight = float(it["gross_weight"]) if it.get("gross_weight") else None

        di_cur = db.execute(
            """INSERT INTO dispatch_items
                   (dispatch_id, product_id, sub_product_id, quantity, price, cbm, gross_weight)
               VALUES (?,?,?,?,?,?,?)""",
            (dispatch_id, product_id, sub_product_id, qty, price, cbm, gross_weight),
        )
        di_id = di_cur.lastrowid

        # FIFO deduction from open POs
        allocations, leftover = _deduct_production_fifo(
            db, product_id, sub_product_id, qty
        )
        for po_item_id, alloc_qty in allocations:
            db.execute(
                """INSERT INTO dispatch_po_allocations
                       (dispatch_item_id, po_item_id, quantity)
                   VALUES (?,?,?)""",
                (di_id, po_item_id, alloc_qty),
            )
        if leftover > 0.001:
            warnings.append(
                f"Only {qty - leftover} of {qty} units found in open POs for "
                f"item {it.get('display_name', product_id)}; {leftover} taken from production anyway."
            )

        # Move qty: production_qty → in_transit_qty
        effective = qty  # move full qty regardless of PO coverage
        _update_qty(db, product_id, sub_product_id, "production_qty", -effective)
        _update_qty(db, product_id, sub_product_id, "in_transit_qty", +effective)

        # Record stock movement for transit dispatch
        db.execute(
            """INSERT INTO stock_movements
                   (product_id, sub_product_id, movement_type, quantity, notes, dispatch_id, expected_arrival)
               VALUES (?,?,'transit_dispatch',?,?,?,?)""",
            (product_id, sub_product_id, effective,
             data.get("notes"), dispatch_id, data.get("expected_arrival") or None),
        )

    db.commit()
    return dispatch_id, warnings


def delete_dispatch(dispatch_id):
    """Reverse in_transit_qty → production_qty for unreceived items, undo PO allocations."""
    db = get_db()
    items = get_dispatch_items(dispatch_id)
    for it in items:
        unreceived = it["quantity"] - (it["qty_received"] or 0)
        if unreceived > 0:
            _update_qty(db, it["product_id"], it["sub_product_id"], "in_transit_qty",  -unreceived)
            _update_qty(db, it["product_id"], it["sub_product_id"], "production_qty",  +unreceived)
        # Reverse PO allocations
        allocs = db.execute(
            "SELECT * FROM dispatch_po_allocations WHERE dispatch_item_id=?",
            (it["id"],),
        ).fetchall()
        for a in allocs:
            db.execute(
                "UPDATE purchase_order_items SET qty_dispatched=qty_dispatched-? WHERE id=?",
                (a["quantity"], a["po_item_id"]),
            )
            # Reopen PO if it was auto-closed
            po_row = db.execute(
                "SELECT po_id FROM purchase_order_items WHERE id=?", (a["po_item_id"],)
            ).fetchone()
            if po_row:
                db.execute(
                    "UPDATE purchase_orders SET status='open' WHERE id=? AND status='closed'",
                    (po_row["po_id"],),
                )
    db.execute("DELETE FROM dispatches WHERE id=?", (dispatch_id,))
    db.commit()


def receive_items(dispatch_id, received):
    """
    received: dict of {dispatch_item_id: qty_to_receive}
    Adds to stock_qty, reduces in_transit_qty, updates qty_received on item.
    Updates dispatch status when all items fully received.
    """
    db = get_db()
    for di_id_str, qty_str in received.items():
        di_id = int(di_id_str)
        qty   = float(qty_str)
        if qty <= 0:
            continue
        row = db.execute("SELECT * FROM dispatch_items WHERE id=?", (di_id,)).fetchone()
        if not row:
            continue
        max_receivable = row["quantity"] - (row["qty_received"] or 0)
        qty = min(qty, max_receivable)
        if qty <= 0:
            continue
        db.execute(
            "UPDATE dispatch_items SET qty_received=qty_received+? WHERE id=?",
            (qty, di_id),
        )
        _update_qty(db, row["product_id"], row["sub_product_id"], "in_transit_qty", -qty)
        _update_qty(db, row["product_id"], row["sub_product_id"], "stock_qty",      +qty)
        # Record stock movement for dispatch received
        db.execute(
            """INSERT INTO stock_movements
                   (product_id, sub_product_id, movement_type, quantity, notes, dispatch_id)
               VALUES (?,?,'transit_arrival',?,?,?)""",
            (row["product_id"], row["sub_product_id"], qty,
             f"Received from dispatch #{dispatch_id}", dispatch_id),
        )

    # Refresh dispatch status
    totals = db.execute(
        """SELECT SUM(quantity) AS total_qty, SUM(qty_received) AS total_rcv
           FROM dispatch_items WHERE dispatch_id=?""",
        (dispatch_id,),
    ).fetchone()
    total   = totals["total_qty"] or 0
    rcv     = totals["total_rcv"] or 0
    if rcv <= 0:
        status = "in_transit"
    elif rcv >= total:
        status = "received"
    else:
        status = "partially_received"
    db.execute("UPDATE dispatches SET status=? WHERE id=?", (status, dispatch_id))
    db.commit()

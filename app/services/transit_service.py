from ..database import get_db


def _update_qty(db, product_id, sub_product_id, field, delta):
    tbl = "sub_products" if sub_product_id else "products"
    pk  = sub_product_id if sub_product_id else product_id
    db.execute(f"UPDATE {tbl} SET {field}={field}+? WHERE id=?", (delta, pk))


def _available_production(db, product_id, sub_product_id):
    """Current production_qty for a product / sub-product (0 if missing)."""
    tbl = "sub_products" if sub_product_id else "products"
    pk  = sub_product_id if sub_product_id else product_id
    if pk is None:
        return 0.0
    row = db.execute(f"SELECT production_qty FROM {tbl} WHERE id=?", (pk,)).fetchone()
    return float(row["production_qty"] or 0) if row else 0.0


# ── FIFO deduction from purchase order items ─────────────────────────────────

def _deduct_production_fifo(db, product_id, sub_product_id, qty_needed, supplier_id=None):
    """Reduce oldest open PO items by qty_needed for the given supplier. Returns list of (po_item_id, qty_taken)."""
    rows = db.execute(
        """SELECT poi.id, poi.quantity, poi.qty_dispatched
           FROM purchase_order_items poi
           JOIN purchase_orders po ON poi.po_id=po.id
           WHERE po.status='open'
             AND (poi.product_id IS ? OR (poi.product_id IS NULL AND ? IS NULL))
             AND (poi.sub_product_id IS ? OR (poi.sub_product_id IS NULL AND ? IS NULL))
             AND poi.quantity > poi.qty_dispatched
             AND (po.supplier_id IS ? OR (po.supplier_id IS NULL AND ? IS NULL))
           ORDER BY po.created_at ASC, poi.id ASC""",
        (product_id, product_id, sub_product_id, sub_product_id, supplier_id, supplier_id),
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

def get_all_dispatches(statuses=None):
    """Dispatches ordered by expected arrival, nearest first (undated last).

    statuses: optional iterable of status values to include. None = all statuses.
    """
    where, params = "", []
    if statuses:
        statuses = list(statuses)
        ph = ",".join("?" * len(statuses))
        where = f"WHERE d.status IN ({ph})"
        params = statuses
    return get_db().execute(
        f"""SELECT d.*, s.name AS supplier_name,
                   COUNT(di.id) AS item_count
            FROM dispatches d
            LEFT JOIN suppliers s  ON d.supplier_id=s.id
            LEFT JOIN dispatch_items di ON di.dispatch_id=d.id
            {where}
            GROUP BY d.id
            ORDER BY (d.expected_arrival IS NULL), d.expected_arrival ASC, d.created_at DESC""",
        params,
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
                       WHEN di.product_name IS NOT NULL
                       THEN di.product_name
                       ELSE p.name END AS display_name,
                  p.sku AS product_sku, sub.sku AS sub_sku,
                  p.pcs_per_carton
           FROM dispatch_items di
           LEFT JOIN products p       ON di.product_id=p.id
           LEFT JOIN sub_products sub ON di.sub_product_id=sub.id
           LEFT JOIN products par     ON sub.product_id=par.id
           WHERE di.dispatch_id=?
           ORDER BY di.id""",
        (dispatch_id,),
    ).fetchall()


def _apply_dispatch_item_stock(db, dispatch_id, di_id, product_id, sub_product_id, qty,
                               supplier_id, notes, expected_arrival, label):
    """Run FIFO PO deduction + move production_qty → in_transit_qty + log a movement
    for one dispatch item. Returns an optional warning string (or None).

    dispatch_id: parent dispatch row id (used for the stock_movement).
    di_id:       dispatch_item row id (used for the PO allocation rows).
    """
    allocations, leftover = _deduct_production_fifo(
        db, product_id, sub_product_id, qty, supplier_id
    )
    for po_item_id, alloc_qty in allocations:
        db.execute(
            """INSERT INTO dispatch_po_allocations
                   (dispatch_item_id, po_item_id, quantity)
               VALUES (?,?,?)""",
            (di_id, po_item_id, alloc_qty),
        )
    warning = None
    if leftover > 0.001:
        warning = (
            f"Only {qty - leftover} of {qty} units found in open POs for "
            f"item {label}; {leftover} taken from production anyway."
        )

    # Move qty: production_qty → in_transit_qty (full qty regardless of PO coverage)
    _update_qty(db, product_id, sub_product_id, "production_qty", -qty)
    _update_qty(db, product_id, sub_product_id, "in_transit_qty", +qty)

    db.execute(
        """INSERT INTO stock_movements
               (product_id, sub_product_id, movement_type, quantity, notes, dispatch_id, expected_arrival)
           VALUES (?,?,'transit_dispatch',?,?,?,?)""",
        (product_id, sub_product_id, qty, notes, dispatch_id, expected_arrival),
    )
    return warning


def create_dispatch(data, items):
    """
    Create a dispatch. A normal dispatch runs FIFO deduction from PO items and moves
    qty from production_qty → in_transit_qty for each product/sub-product.

    A draft (data['status'] == 'draft') records the dispatch and its items but moves
    NO stock — the production → transit move is deferred to activate_dispatch().
    """
    db = get_db()
    is_draft = (data.get("status") == "draft")
    cur = db.execute(
        """INSERT INTO dispatches (name, supplier_id, dispatch_date, expected_arrival, status, notes)
           VALUES (?,?,?,?,?,?)""",
        (data["name"],
         data.get("supplier_id") or None,
         data.get("dispatch_date") or None,
         data.get("expected_arrival") or None,
         "draft" if is_draft else "in_transit",
         data.get("notes")),
    )
    dispatch_id = cur.lastrowid

    supplier_id = data.get("supplier_id") or None
    if supplier_id:
        supplier_id = int(supplier_id)

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

        if is_draft:
            continue  # drafts hold no stock until activated

        w = _apply_dispatch_item_stock(
            db, dispatch_id, di_cur.lastrowid, product_id, sub_product_id, qty, supplier_id,
            data.get("notes"), data.get("expected_arrival") or None,
            it.get("display_name", product_id),
        )
        if w:
            warnings.append(w)

    db.commit()
    return dispatch_id, warnings


def activate_dispatch(dispatch_id):
    """Promote a draft dispatch to 'in_transit', applying the FIFO PO deduction and
    moving production_qty → in_transit_qty for every line.

    Re-checks availability first: if any line's quantity exceeds the product's current
    production_qty, nothing is applied. Returns (ok, errors, warnings).
    """
    db = get_db()
    d = db.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if not d or d["status"] != "draft":
        return False, ["This dispatch is not a draft."], []

    items = get_dispatch_items(dispatch_id)
    errors = []
    for it in items:
        available = _available_production(db, it["product_id"], it["sub_product_id"])
        if it["quantity"] > available + 1e-9:
            errors.append(
                f"{it['display_name']}: dispatch qty {it['quantity']} exceeds "
                f"production qty {available}."
            )
    if errors:
        return False, errors, []

    supplier_id = d["supplier_id"]
    warnings = []
    for it in items:
        w = _apply_dispatch_item_stock(
            db, dispatch_id, it["id"], it["product_id"], it["sub_product_id"], it["quantity"],
            supplier_id, d["notes"], d["expected_arrival"], it["display_name"],
        )
        if w:
            warnings.append(w)
    db.execute("UPDATE dispatches SET status='in_transit' WHERE id=?", (dispatch_id,))
    db.commit()
    return True, [], warnings


def delete_dispatch(dispatch_id):
    """Reverse in_transit_qty → production_qty for unreceived items, undo PO allocations.

    A draft never moved any stock, so it is simply deleted with no reversal.
    """
    db = get_db()
    d = db.execute("SELECT status FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if d and d["status"] == "draft":
        db.execute("DELETE FROM dispatches WHERE id=?", (dispatch_id,))
        db.commit()
        return
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


def get_dispatches_due_soon(days=14):
    return get_db().execute(
        """SELECT d.*, s.name AS supplier_name,
                  COUNT(di.id) AS item_count,
                  julianday(d.expected_arrival) - julianday('now') AS days_remaining
           FROM dispatches d
           LEFT JOIN suppliers s ON d.supplier_id=s.id
           LEFT JOIN dispatch_items di ON di.dispatch_id=d.id
           WHERE d.status IN ('in_transit', 'partially_received')
           GROUP BY d.id
           ORDER BY d.expected_arrival ASC"""
    ).fetchall()


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

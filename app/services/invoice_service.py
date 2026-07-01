from datetime import date
from ..database import get_db
from . import product_service
from .payment_service import _refresh_invoice_paid


def _max_seq(db, prefix):
    """Highest numeric suffix among invoice_numbers with the given prefix (e.g. 'INV-')."""
    rows = db.execute(
        "SELECT invoice_number FROM invoices WHERE invoice_number LIKE ?",
        (f"{prefix}%",),
    ).fetchall()
    mx = 0
    for r in rows:
        try:
            mx = max(mx, int(r["invoice_number"].split("-")[-1]))
        except (ValueError, AttributeError):
            pass
    return mx


def _next_invoice_number(db):
    """Next issued serial (INV-0001, ...). Ignores draft (D-###) numbers so a
    pending draft never reserves an issued serial — the serial is assigned at issue
    time, keeping issued invoices in true issue order."""
    return f"INV-{_max_seq(db, 'INV-') + 1:04d}"


def _next_draft_number(db):
    """Next draft placeholder number (D-001, ...). Replaced with a real INV-### serial
    when the draft is issued."""
    return f"D-{_max_seq(db, 'D-') + 1:03d}"


def get_all_invoices():
    return get_db().execute(
        """SELECT i.*, c.name as client_name, c.company as client_company,
                  cc.name as company_name
           FROM invoices i
           JOIN clients c ON i.client_id = c.id
           LEFT JOIN client_companies cc ON i.company_id = cc.id
           ORDER BY (i.status = 'draft') DESC,
                    COALESCE(i.issued_at, i.created_at) DESC"""
    ).fetchall()


def get_adjacent_invoices(invoice_id):
    """Return (prev_id, next_id) for navigation, following the same ordering as
    the invoice list (drafts first, then newest issued/created). 'prev' is the
    invoice above the current one in the list, 'next' the one below."""
    rows = get_db().execute(
        """SELECT id FROM invoices
           ORDER BY (status = 'draft') DESC,
                    COALESCE(issued_at, created_at) DESC, id DESC"""
    ).fetchall()
    ids = [r["id"] for r in rows]
    try:
        pos = ids.index(invoice_id)
    except ValueError:
        return (None, None)
    prev_id = ids[pos - 1] if pos > 0 else None
    next_id = ids[pos + 1] if pos < len(ids) - 1 else None
    return (prev_id, next_id)


def get_invoice(invoice_id):
    return get_db().execute(
        """SELECT i.*, c.name as client_name, c.company as client_company,
                  c.email as client_email, c.address as client_address,
                  c.city as client_city, c.country as client_country,
                  c.tax_id as client_tax_id,
                  cc.name as company_name
           FROM invoices i
           JOIN clients c ON i.client_id = c.id
           LEFT JOIN client_companies cc ON i.company_id = cc.id
           WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()


def get_invoice_items(invoice_id):
    return get_db().execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,)
    ).fetchall()


def get_invoice_payments(invoice_id):
    return get_db().execute(
        """SELECT p.*, pa.amount AS allocated_amount
           FROM payment_allocations pa
           JOIN payments p ON p.id = pa.payment_id
           WHERE pa.invoice_id = ?
           ORDER BY p.payment_date DESC, p.id DESC""",
        (invoice_id,),
    ).fetchall()


def _apply_company_ob_credit(db, client_id, company_id, invoice_id, issue_date):
    """
    If the company has a credit opening balance, apply it to the new invoice.
    Creates a 'balance' payment and reduces the company's opening_balance in-place
    so the client-level balance formula stays consistent.
    """
    co = db.execute(
        "SELECT opening_balance FROM client_companies WHERE id=?", (company_id,)
    ).fetchone()
    if not co:
        return
    ob = float(co["opening_balance"] or 0)
    if ob >= 0:
        return  # no credit

    credit = abs(ob)
    inv = db.execute("SELECT total, amount_paid FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    remaining_invoice = float(inv["total"]) - float(inv["amount_paid"])
    apply = min(credit, remaining_invoice)
    if apply < 0.01:
        return

    # Create a 'balance' payment row + allocation for the applied amount
    cur = db.execute(
        """INSERT INTO payments
               (client_id, invoice_id, company_id, amount, payment_date, method, notes)
           VALUES (?, NULL, ?, ?, ?, 'balance', 'Auto-applied from credit balance')""",
        (client_id, company_id, apply, issue_date),
    )
    db.execute(
        "INSERT INTO payment_allocations (payment_id, invoice_id, amount) VALUES (?,?,?)",
        (cur.lastrowid, invoice_id, apply),
    )
    # Reduce company OB so the credit isn't double-counted
    new_ob = ob + apply  # ob is negative, so this moves toward 0
    db.execute(
        "UPDATE client_companies SET opening_balance=? WHERE id=?", (new_ob, company_id)
    )
    # Re-sync client-level opening_balance
    row = db.execute(
        "SELECT COALESCE(SUM(opening_balance), 0) AS s FROM client_companies WHERE client_id=?",
        (client_id,),
    ).fetchone()
    db.execute(
        "UPDATE clients SET opening_balance=? WHERE id=?", (row["s"], client_id)
    )
    _refresh_invoice_paid(db, invoice_id)


def check_stock_for_issue(invoice_id):
    """Return list of catalog items in the invoice with their stock status.
    Each dict: {name, description, required, available, sufficient}
    Only includes items linked to a product or sub-product.
    """
    db = get_db()
    items = db.execute(
        """SELECT ii.description, ii.quantity, ii.product_id, ii.sub_product_id,
                  p.name AS product_name, sp.name AS sub_name
           FROM invoice_items ii
           LEFT JOIN products p ON ii.product_id = p.id
           LEFT JOIN sub_products sp ON ii.sub_product_id = sp.id
           WHERE ii.invoice_id = ?""",
        (invoice_id,),
    ).fetchall()
    result = []
    for it in items:
        pid  = it["product_id"]
        spid = it["sub_product_id"]
        if not pid and not spid:
            continue
        tbl = "sub_products" if spid else "products"
        pk  = spid if spid else pid
        row = db.execute(f"SELECT stock_qty FROM {tbl} WHERE id=?", (pk,)).fetchone()
        available = float(row["stock_qty"] or 0) if row else 0.0
        required  = float(it["quantity"])
        # Build a full name matching the line items display: "Parent — Variant"
        if it["sub_name"] and it["product_name"]:
            name = f"{it['product_name']} — {it['sub_name']}"
        elif it["sub_name"]:
            name = it["sub_name"]
        elif it["product_name"]:
            name = it["product_name"]
        else:
            name = it["description"]
        result.append({
            "name":           name,
            "description":    it["description"],
            "product_id":     pid,
            "sub_product_id": spid,
            "required":       required,
            "available":      available,
            "sufficient":     available >= required,
        })
    return result


def _item_net(it):
    """Per-line net (after item-level discount) and tax. Returns (line_net, line_tax)."""
    qty   = float(it.get("quantity", 0) or 0)
    price = float(it.get("unit_price", 0) or 0)
    dval  = float(it.get("discount_value", 0) or 0)
    dtype = (it.get("discount_type") or "percent").lower()
    if dtype == "percent":
        eff_price = price * (1.0 - dval / 100.0)
    else:
        eff_price = price - dval
    if eff_price < 0:
        eff_price = 0
    line_net = eff_price * qty
    line_tax = line_net * float(it.get("tax_rate", 0) or 0) / 100.0
    return line_net, line_tax


def _compute_totals(items, invoice_discount_value, invoice_discount_type):
    subtotal  = 0.0
    tax_total = 0.0
    for it in items:
        n, t = _item_net(it)
        subtotal  += n
        tax_total += t
    dval  = float(invoice_discount_value or 0)
    dtype = (invoice_discount_type or "value").lower()
    if dtype == "percent":
        discount_amount = (subtotal + tax_total) * dval / 100.0
    else:
        discount_amount = dval
    total = subtotal + tax_total - discount_amount
    return subtotal, tax_total, discount_amount, total


def create_invoice(data, items):
    db = get_db()
    is_draft = data.get("status", "issued") == "draft"
    invoice_number = _next_draft_number(db) if is_draft else _next_invoice_number(db)

    inv_dtype = (data.get("discount_type") or "value").lower()
    inv_dval  = float(data.get("discount_amount", 0) or 0)
    subtotal, tax_total, discount, total = _compute_totals(items, inv_dval, inv_dtype)

    company_id = int(data["company_id"]) if data.get("company_id") else None

    cur = db.execute(
        """INSERT INTO invoices (invoice_number, client_id, company_id, status, issue_date, due_date,
           notes, subtotal, tax_total, discount_amount, discount_type, discount_value, total)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            invoice_number, data["client_id"], company_id,
            data.get("status", "issued"),
            data.get("issue_date", str(date.today())),
            data.get("due_date"),
            data.get("notes"),
            subtotal, tax_total, discount, inv_dtype, inv_dval, total,
        ),
    )
    invoice_id = cur.lastrowid

    for it in items:
        pid  = it.get("product_id") or None
        spid = it.get("sub_product_id") or None
        qty  = float(it["quantity"])
        line_net, _ = _item_net(it)
        db.execute(
            """INSERT INTO invoice_items
               (invoice_id, product_id, sub_product_id, sku, description, quantity, unit_price,
                tax_rate, discount_type, discount_value, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                invoice_id, pid, spid,
                it.get("sku") or None,
                it["description"],
                qty,
                float(it["unit_price"]),
                float(it.get("tax_rate", 0)),
                (it.get("discount_type") or "percent").lower(),
                float(it.get("discount_value", 0) or 0),
                line_net,
            ),
        )
        # Deduct warehouse stock only when not saving as draft
        if not is_draft and (pid or spid):
            tbl = "sub_products" if spid else "products"
            pk  = spid if spid else pid
            db.execute(f"UPDATE {tbl} SET stock_qty = stock_qty - ? WHERE id = ?", (qty, pk))
            db.execute(
                "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes, invoice_id) VALUES (?,?,?,?,?,?)",
                (pid, spid, "sale", qty, f"Invoice {invoice_number}", invoice_id),
            )
    db.commit()

    # Auto-apply company credit OB only for non-draft invoices
    if not is_draft and company_id:
        issue_date = data.get("issue_date", str(date.today()))
        _apply_company_ob_credit(db, int(data["client_id"]), company_id, invoice_id, issue_date)

    return invoice_id


def update_invoice_status(invoice_id, status):
    """Update invoice status. Returns (ok, errors).
    errors is a list of stock shortage dicts when draft→issued fails the stock check.
    """
    db = get_db()
    inv = db.execute(
        "SELECT status, invoice_number, client_id, company_id, issue_date FROM invoices WHERE id=?",
        (invoice_id,),
    ).fetchone()
    if not inv:
        return True, []
    prev_status = inv["status"]

    # Draft → Issued: check stock first, then apply deductions + OB credit
    if prev_status == "draft" and status == "issued":
        stock_items = check_stock_for_issue(invoice_id)
        short = [s for s in stock_items if not s["sufficient"]]
        if short:
            return False, short
        # Assign the real serial now (replaces the D-### draft number) so issued
        # invoices are numbered in true issue order.
        new_number = (_next_invoice_number(db)
                      if str(inv["invoice_number"]).startswith("D-")
                      else inv["invoice_number"])
        items = db.execute(
            "SELECT product_id, sub_product_id, quantity FROM invoice_items WHERE invoice_id=?",
            (invoice_id,),
        ).fetchall()
        for it in items:
            pid  = it["product_id"]
            spid = it["sub_product_id"]
            qty  = float(it["quantity"])
            if pid or spid:
                tbl = "sub_products" if spid else "products"
                pk  = spid if spid else pid
                db.execute(f"UPDATE {tbl} SET stock_qty = stock_qty - ? WHERE id = ?", (qty, pk))
                db.execute(
                    "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes, invoice_id) VALUES (?,?,?,?,?,?)",
                    (pid, spid, "sale", qty, f"Invoice {new_number}", invoice_id),
                )
        db.execute(
            "UPDATE invoices SET status=?, invoice_number=?, issued_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, new_number, invoice_id),
        )
        db.commit()
        if inv["company_id"]:
            _apply_company_ob_credit(db, inv["client_id"], inv["company_id"], invoice_id, inv["issue_date"])
        return True, []

    db.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))

    # Restore stock when cancelling a previously-issued invoice (draft never held stock)
    if status == "cancelled" and prev_status not in ("cancelled", "draft"):
        items = db.execute(
            "SELECT product_id, sub_product_id, quantity FROM invoice_items WHERE invoice_id=?",
            (invoice_id,),
        ).fetchall()
        for it in items:
            pid  = it["product_id"]
            spid = it["sub_product_id"]
            qty  = it["quantity"]
            if pid or spid:
                tbl = "sub_products" if spid else "products"
                pk  = spid if spid else pid
                db.execute(f"UPDATE {tbl} SET stock_qty = stock_qty + ? WHERE id = ?", (qty, pk))
                db.execute(
                    "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes, invoice_id) VALUES (?,?,?,?,?,?)",
                    (pid, spid, "sale_cancelled", qty,
                     f"Cancelled: Invoice {inv['invoice_number']}", invoice_id),
                )
    db.commit()
    return True, []


def update_invoice(invoice_id, data, items):
    db = get_db()

    inv_dtype = (data.get("discount_type") or "value").lower()
    inv_dval  = float(data.get("discount_amount", 0) or 0)
    subtotal, tax_total, discount, total = _compute_totals(items, inv_dval, inv_dtype)

    company_id = int(data["company_id"]) if data.get("company_id") else None

    prev = db.execute("SELECT status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    requested_status = data.get("status", "issued")
    # Issuing a draft from the edit form: keep the row in 'draft' for this field/item
    # update, then let update_invoice_status run the real transition (stock check +
    # serial assignment). Single source of truth for draft→issued.
    issuing = bool(prev) and prev["status"] == "draft" and requested_status == "issued"
    effective_status = "draft" if issuing else requested_status

    db.execute(
        """UPDATE invoices SET client_id=?, company_id=?, status=?, issue_date=?, due_date=?,
           notes=?, subtotal=?, tax_total=?, discount_amount=?, discount_type=?, discount_value=?, total=?
           WHERE id=?""",
        (
            data["client_id"], company_id, effective_status,
            data.get("issue_date"), data.get("due_date"),
            data.get("notes"), subtotal, tax_total, discount, inv_dtype, inv_dval, total,
            invoice_id,
        ),
    )
    db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    for it in items:
        line_net, _ = _item_net(it)
        db.execute(
            """INSERT INTO invoice_items
               (invoice_id, product_id, sub_product_id, sku, description, quantity, unit_price,
                tax_rate, discount_type, discount_value, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                invoice_id,
                it.get("product_id") or None,
                it.get("sub_product_id") or None,
                it.get("sku") or None,
                it["description"],
                float(it["quantity"]),
                float(it["unit_price"]),
                float(it.get("tax_rate", 0)),
                (it.get("discount_type") or "percent").lower(),
                float(it.get("discount_value", 0) or 0),
                line_net,
            ),
        )
    db.commit()

    if issuing:
        # Runs stock check + serial assignment; returns (ok, stock-shortage errors).
        return update_invoice_status(invoice_id, "issued")
    return True, []


def delete_invoice(invoice_id):
    db = get_db()
    db.execute("DELETE FROM payment_allocations WHERE invoice_id = ?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()


def refresh_invoice_paid(invoice_id):
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as paid FROM payment_allocations WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchone()
    paid = row["paid"]
    inv = db.execute("SELECT total, status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not inv:
        return
    # Never auto-advance a draft invoice out of draft status via payment
    if inv["status"] == "draft":
        db.execute("UPDATE invoices SET amount_paid=? WHERE id=?", (paid, invoice_id))
        db.commit()
        return
    total = inv["total"]
    if paid <= 0:
        status = "issued"
    elif paid >= total:
        status = "paid"
    else:
        status = "partial"
    db.execute(
        "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
        (paid, status, invoice_id),
    )
    db.commit()

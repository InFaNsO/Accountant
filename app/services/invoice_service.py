from datetime import date
from ..database import get_db
from . import product_service
from .payment_service import _refresh_invoice_paid


def _next_invoice_number(db):
    row = db.execute(
        "SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "INV-0001"
    last = row["invoice_number"]
    try:
        num = int(last.split("-")[-1]) + 1
    except ValueError:
        num = 1
    return f"INV-{num:04d}"


def get_all_invoices():
    return get_db().execute(
        """SELECT i.*, c.name as client_name, c.company as client_company,
                  cc.name as company_name
           FROM invoices i
           JOIN clients c ON i.client_id = c.id
           LEFT JOIN client_companies cc ON i.company_id = cc.id
           ORDER BY i.created_at DESC"""
    ).fetchall()


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
        "SELECT * FROM payments WHERE invoice_id = ? ORDER BY payment_date DESC",
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

    # Create a 'balance' payment for the applied amount
    db.execute(
        """INSERT INTO payments
               (client_id, invoice_id, company_id, amount, payment_date, method, notes)
           VALUES (?, ?, ?, ?, ?, 'balance', 'Auto-applied from credit balance')""",
        (client_id, invoice_id, company_id, apply, issue_date),
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
        name = it["sub_name"] or it["product_name"] or it["description"]
        result.append({
            "name":        name,
            "description": it["description"],
            "required":    required,
            "available":   available,
            "sufficient":  available >= required,
        })
    return result


def create_invoice(data, items):
    db = get_db()
    invoice_number = _next_invoice_number(db)
    is_draft = data.get("status", "issued") == "draft"

    subtotal = sum(float(it["unit_price"]) * float(it["quantity"]) for it in items)
    tax_total = sum(
        float(it["unit_price"]) * float(it["quantity"]) * float(it.get("tax_rate", 0)) / 100
        for it in items
    )
    discount = float(data.get("discount_amount", 0))
    total = subtotal + tax_total - discount

    company_id = int(data["company_id"]) if data.get("company_id") else None

    cur = db.execute(
        """INSERT INTO invoices (invoice_number, client_id, company_id, status, issue_date, due_date,
           notes, subtotal, tax_total, discount_amount, total)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            invoice_number, data["client_id"], company_id,
            data.get("status", "issued"),
            data.get("issue_date", str(date.today())),
            data.get("due_date"),
            data.get("notes"),
            subtotal, tax_total, discount, total,
        ),
    )
    invoice_id = cur.lastrowid

    for it in items:
        pid  = it.get("product_id") or None
        spid = it.get("sub_product_id") or None
        qty  = float(it["quantity"])
        line_total = float(it["unit_price"]) * qty
        db.execute(
            """INSERT INTO invoice_items
               (invoice_id, product_id, sub_product_id, sku, description, quantity, unit_price, tax_rate, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                invoice_id, pid, spid,
                it.get("sku") or None,
                it["description"],
                qty,
                float(it["unit_price"]),
                float(it.get("tax_rate", 0)),
                line_total,
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
                    (pid, spid, "sale", qty, f"Invoice {inv['invoice_number']}", invoice_id),
                )
        db.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
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

    subtotal = sum(float(it["unit_price"]) * float(it["quantity"]) for it in items)
    tax_total = sum(
        float(it["unit_price"]) * float(it["quantity"]) * float(it.get("tax_rate", 0)) / 100
        for it in items
    )
    discount = float(data.get("discount_amount", 0))
    total = subtotal + tax_total - discount

    company_id = int(data["company_id"]) if data.get("company_id") else None

    db.execute(
        """UPDATE invoices SET client_id=?, company_id=?, status=?, issue_date=?, due_date=?,
           notes=?, subtotal=?, tax_total=?, discount_amount=?, total=?
           WHERE id=?""",
        (
            data["client_id"], company_id, data.get("status", "issued"),
            data.get("issue_date"), data.get("due_date"),
            data.get("notes"), subtotal, tax_total, discount, total,
            invoice_id,
        ),
    )
    db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    for it in items:
        line_total = float(it["unit_price"]) * float(it["quantity"])
        db.execute(
            """INSERT INTO invoice_items
               (invoice_id, product_id, sub_product_id, sku, description, quantity, unit_price, tax_rate, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                invoice_id,
                it.get("product_id") or None,
                it.get("sub_product_id") or None,
                it.get("sku") or None,
                it["description"],
                float(it["quantity"]),
                float(it["unit_price"]),
                float(it.get("tax_rate", 0)),
                line_total,
            ),
        )
    db.commit()


def delete_invoice(invoice_id):
    db = get_db()
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()


def refresh_invoice_paid(invoice_id):
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as paid FROM payments WHERE invoice_id = ?",
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

from datetime import date
from ..database import get_db


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
        """SELECT i.*, c.name as client_name, c.company as client_company
           FROM invoices i JOIN clients c ON i.client_id = c.id
           ORDER BY i.created_at DESC"""
    ).fetchall()


def get_invoice(invoice_id):
    return get_db().execute(
        """SELECT i.*, c.name as client_name, c.company as client_company,
                  c.email as client_email, c.address as client_address,
                  c.city as client_city, c.country as client_country,
                  c.tax_id as client_tax_id
           FROM invoices i JOIN clients c ON i.client_id = c.id
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


def _apply_client_credit(db, client_id, invoice_id):
    """Reallocate unallocated (NULL) payments that represent true credit to the new invoice."""
    client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    opening_debt = abs((client["opening_balance"] or 0)) if client else 0.0

    total_null = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE client_id=? AND invoice_id IS NULL",
        (client_id,),
    ).fetchone()["s"]

    available_credit = max(0.0, total_null - opening_debt)
    if available_credit < 0.01:
        return

    inv = db.execute("SELECT total FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return

    to_cover = min(available_credit, inv["total"])
    if to_cover < 0.01:
        return

    null_pmts = db.execute(
        "SELECT id, amount, payment_date, method, reference, notes FROM payments "
        "WHERE client_id=? AND invoice_id IS NULL ORDER BY created_at ASC",
        (client_id,),
    ).fetchall()

    covered = 0.0
    for pmt in null_pmts:
        if covered >= to_cover - 0.001:
            break
        take = min(pmt["amount"], to_cover - covered)
        leftover = pmt["amount"] - take
        if leftover < 0.01:
            db.execute("UPDATE payments SET invoice_id=? WHERE id=?", (invoice_id, pmt["id"]))
        else:
            db.execute("UPDATE payments SET amount=? WHERE id=?", (leftover, pmt["id"]))
            db.execute(
                """INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (client_id, invoice_id, take,
                 pmt["payment_date"], pmt["method"], pmt["reference"], pmt["notes"]),
            )
        covered += take

    if covered > 0.001:
        refresh_invoice_paid(invoice_id)


def create_invoice(data, items):
    db = get_db()
    invoice_number = _next_invoice_number(db)

    subtotal = sum(float(it["unit_price"]) * float(it["quantity"]) for it in items)
    tax_total = sum(
        float(it["unit_price"]) * float(it["quantity"]) * float(it.get("tax_rate", 0)) / 100
        for it in items
    )
    discount = float(data.get("discount_amount", 0))
    total = subtotal + tax_total - discount

    cur = db.execute(
        """INSERT INTO invoices (invoice_number, client_id, status, issue_date, due_date,
           notes, subtotal, tax_total, discount_amount, total)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            invoice_number, data["client_id"],
            data.get("status", "draft"),
            data.get("issue_date", str(date.today())),
            data.get("due_date"),
            data.get("notes"),
            subtotal, tax_total, discount, total,
        ),
    )
    invoice_id = cur.lastrowid

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

    # Auto-apply any existing client credit to this new invoice
    _apply_client_credit(db, int(data["client_id"]), invoice_id)

    return invoice_id


def update_invoice_status(invoice_id, status):
    db = get_db()
    db.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
    db.commit()


def update_invoice(invoice_id, data, items):
    db = get_db()

    subtotal = sum(float(it["unit_price"]) * float(it["quantity"]) for it in items)
    tax_total = sum(
        float(it["unit_price"]) * float(it["quantity"]) * float(it.get("tax_rate", 0)) / 100
        for it in items
    )
    discount = float(data.get("discount_amount", 0))
    total = subtotal + tax_total - discount

    db.execute(
        """UPDATE invoices SET client_id=?, status=?, issue_date=?, due_date=?,
           notes=?, subtotal=?, tax_total=?, discount_amount=?, total=?
           WHERE id=?""",
        (
            data["client_id"], data.get("status", "draft"),
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
    inv = db.execute("SELECT total FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not inv:
        return
    total = inv["total"]
    inv = db.execute("SELECT status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if paid <= 0:
        status = inv["status"] if inv and inv["status"] == "draft" else "sent"
    elif paid >= total:
        status = "paid"
    else:
        status = "partial"
    db.execute(
        "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
        (paid, status, invoice_id),
    )
    db.commit()

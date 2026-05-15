from ..database import get_db


def get_all_clients():
    db = get_db()
    return db.execute(
        """
        SELECT
            c.*,
            COALESCE(inv60.purchases_60d, 0)  AS purchases_60d,
            COALESCE(pay60.payments_60d, 0)   AS payments_60d,
            COALESCE(inv_bal.invoice_balance, 0) AS invoice_balance,
            COALESCE(ob_paid.ob_paid, 0)      AS ob_paid
        FROM clients c
        LEFT JOIN (
            SELECT client_id, SUM(total) AS purchases_60d
            FROM invoices
            WHERE issue_date >= date('now', '-60 days')
              AND status != 'cancelled'
            GROUP BY client_id
        ) inv60 ON c.id = inv60.client_id
        LEFT JOIN (
            SELECT client_id, SUM(amount) AS payments_60d
            FROM payments
            WHERE payment_date >= date('now', '-60 days')
            GROUP BY client_id
        ) pay60 ON c.id = pay60.client_id
        LEFT JOIN (
            SELECT client_id, SUM(total - amount_paid) AS invoice_balance
            FROM invoices WHERE status != 'cancelled'
            GROUP BY client_id
        ) inv_bal ON c.id = inv_bal.client_id
        LEFT JOIN (
            SELECT client_id, SUM(amount) AS ob_paid
            FROM payments WHERE invoice_id IS NULL
            GROUP BY client_id
        ) ob_paid ON c.id = ob_paid.client_id
        ORDER BY c.name
        """
    ).fetchall()


def get_client(client_id):
    return get_db().execute(
        "SELECT * FROM clients WHERE id = ?", (client_id,)
    ).fetchone()


def create_client(data):
    db = get_db()
    cur = db.execute(
        """INSERT INTO clients
               (name, company, email, phone, address, city, country, tax_id, notes, opening_balance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"], data.get("company"), data.get("email"),
            data.get("phone"), data.get("address"), data.get("city"),
            data.get("country"), data.get("tax_id"), data.get("notes"),
            _signed_opening(data),
        ),
    )
    db.commit()
    return cur.lastrowid


def _signed_opening(data):
    """Return signed opening balance: positive = debt, negative = credit."""
    amt = abs(float(data.get("opening_balance_amt") or data.get("opening_balance") or 0))
    if data.get("opening_balance_type") == "credit":
        return -amt
    return amt


def update_client(client_id, data):
    db = get_db()
    db.execute(
        """UPDATE clients
           SET name=?, company=?, email=?, phone=?, address=?,
               city=?, country=?, tax_id=?, notes=?, opening_balance=?,
               updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            data["name"], data.get("company"), data.get("email"),
            data.get("phone"), data.get("address"), data.get("city"),
            data.get("country"), data.get("tax_id"), data.get("notes"),
            _signed_opening(data),
            client_id,
        ),
    )
    db.commit()


def delete_client(client_id):
    db = get_db()
    db.execute("DELETE FROM payments WHERE client_id = ?", (client_id,))
    db.execute("DELETE FROM invoices WHERE client_id = ?", (client_id,))
    db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    db.commit()


def get_client_invoices(client_id):
    return get_db().execute(
        "SELECT * FROM invoices WHERE client_id = ? ORDER BY created_at DESC",
        (client_id,),
    ).fetchall()


def get_client_balance(client_id):
    db = get_db()
    invoice_row = db.execute(
        """SELECT COALESCE(SUM(total - amount_paid), 0) AS balance
           FROM invoices WHERE client_id = ? AND status != 'cancelled'""",
        (client_id,),
    ).fetchone()
    client_row = db.execute(
        "SELECT opening_balance FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    ob_paid_row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS ob_paid FROM payments WHERE client_id = ? AND invoice_id IS NULL",
        (client_id,),
    ).fetchone()
    invoice_balance = invoice_row["balance"] if invoice_row else 0
    opening = client_row["opening_balance"] if client_row else 0
    ob_paid = ob_paid_row["ob_paid"] if ob_paid_row else 0
    if opening >= 0:
        # opening_balance > 0: client had a starting debt
        opening_remaining = max(0.0, opening - ob_paid)
        excess = max(0.0, ob_paid - opening)
    else:
        # opening_balance < 0: client had a starting credit
        opening_remaining = 0.0
        excess = ob_paid + abs(opening)
    # negative = client owes us, positive = client has credit
    return -(opening_remaining + invoice_balance) + excess

from ..database import get_db


def get_all_clients():
    db = get_db()
    return db.execute(
        """
        SELECT
            c.*,
            COALESCE(inv60.purchases_60d, 0)    AS purchases_60d,
            COALESCE(pay60.payments_60d, 0)     AS payments_60d,
            COALESCE(t_inv.total_invoiced, 0)   AS total_invoiced,
            COALESCE(t_pay.total_paid, 0)       AS total_paid
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
            SELECT client_id, SUM(total) AS total_invoiced
            FROM invoices WHERE status != 'cancelled'
            GROUP BY client_id
        ) t_inv ON c.id = t_inv.client_id
        LEFT JOIN (
            SELECT client_id, SUM(amount) AS total_paid
            FROM payments
            GROUP BY client_id
        ) t_pay ON c.id = t_pay.client_id
        ORDER BY c.name
        """
    ).fetchall()


def get_client(client_id):
    return get_db().execute(
        "SELECT * FROM clients WHERE id = ?", (client_id,)
    ).fetchone()


def create_client(data, companies=None):
    """Create a client. If companies list provided, create them and derive OB from their sum."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO clients (name, phone, notes, opening_balance, payment_terms)
           VALUES (?, ?, ?, 0, ?)""",
        (
            data["name"],
            data.get("phone"),
            data.get("notes"),
            int(data.get("payment_terms") or 30),
        ),
    )
    client_id = cur.lastrowid

    if companies:
        for co in companies:
            if co.get("name"):
                ob = _signed_opening(co)
                db.execute(
                    """INSERT INTO client_companies
                           (client_id, name, tax_id, opening_balance)
                       VALUES (?, ?, ?, ?)""",
                    (client_id, co["name"], co.get("tax_id"), ob),
                )
        _sync_client_ob(db, client_id)

    db.commit()
    return client_id


def _signed_opening(data):
    """Return signed opening balance: positive = debt, negative = credit."""
    amt = abs(float(data.get("opening_balance_amt") or data.get("opening_balance") or 0))
    if data.get("opening_balance_type") == "credit":
        return -amt
    return amt


def _sync_client_ob(db, client_id):
    """Set clients.opening_balance = SUM of all company opening balances."""
    row = db.execute(
        "SELECT COALESCE(SUM(opening_balance), 0) AS s FROM client_companies WHERE client_id=?",
        (client_id,),
    ).fetchone()
    db.execute(
        "UPDATE clients SET opening_balance=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (row["s"], client_id),
    )


def update_client(client_id, data, companies=None):
    """Update a client's basic info. If companies list provided, sync company records."""
    db = get_db()
    db.execute(
        """UPDATE clients
           SET name=?, phone=?, notes=?,
               payment_terms=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            data["name"],
            data.get("phone"),
            data.get("notes"),
            int(data.get("payment_terms") or 30),
            client_id,
        ),
    )

    if companies is not None:
        existing_ids = {
            row["id"]
            for row in db.execute(
                "SELECT id FROM client_companies WHERE client_id=?", (client_id,)
            ).fetchall()
        }
        submitted_ids = set()
        for co in companies:
            if not co.get("name"):
                continue
            ob = _signed_opening(co)
            co_id = co.get("id")
            if co_id:
                co_id = int(co_id)
                db.execute(
                    "UPDATE client_companies SET name=?, tax_id=?, opening_balance=? WHERE id=?",
                    (co["name"], co.get("tax_id"), ob, co_id),
                )
                submitted_ids.add(co_id)
            else:
                row = db.execute(
                    """INSERT INTO client_companies (client_id, name, tax_id, opening_balance)
                       VALUES (?, ?, ?, ?)""",
                    (client_id, co["name"], co.get("tax_id"), ob),
                )
                submitted_ids.add(row.lastrowid)
        # Delete companies removed from form
        for gone_id in existing_ids - submitted_ids:
            db.execute("UPDATE invoices SET company_id=NULL WHERE company_id=?", (gone_id,))
            db.execute("UPDATE payments SET company_id=NULL WHERE company_id=?", (gone_id,))
            db.execute("DELETE FROM client_companies WHERE id=?", (gone_id,))
        _sync_client_ob(db, client_id)

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
    """
    Returns signed balance: negative = client owes us, positive = client has credit.

    Formula:
      balance = (total_payments + credit_ob) - (total_invoiced + debit_ob)

    This is independent of invoice.amount_paid so it stays correct whether or not
    the OB credit has been baked into amount_paid via Recalculate.
    """
    db = get_db()
    client_row = db.execute(
        "SELECT opening_balance FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if not client_row:
        return 0.0

    ob       = float(client_row["opening_balance"] or 0)
    debit_ob = max(0.0, ob)    # positive OB = client owes us from before
    credit_ob = max(0.0, -ob)  # negative OB = client pre-paid credit

    total_invoiced = float(db.execute(
        "SELECT COALESCE(SUM(total), 0) AS s FROM invoices WHERE client_id=? AND status != 'cancelled'",
        (client_id,),
    ).fetchone()["s"])

    total_paid = float(db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE client_id=?",
        (client_id,),
    ).fetchone()["s"])

    return (total_paid + credit_ob) - (total_invoiced + debit_ob)


# ── Client companies ──────────────────────────────────────────────────────────

def get_companies(client_id):
    return get_db().execute(
        "SELECT * FROM client_companies WHERE client_id=? ORDER BY name",
        (client_id,),
    ).fetchall()


def create_company(client_id, data):
    db = get_db()
    ob = _signed_opening(data)
    cur = db.execute(
        """INSERT INTO client_companies (client_id, name, tax_id, opening_balance)
           VALUES (?, ?, ?, ?)""",
        (client_id, data["name"], data.get("tax_id"), ob),
    )
    _sync_client_ob(db, client_id)
    db.commit()
    return cur.lastrowid


def update_company(company_id, data):
    db = get_db()
    ob = _signed_opening(data)
    db.execute(
        "UPDATE client_companies SET name=?, tax_id=?, opening_balance=? WHERE id=?",
        (data["name"], data.get("tax_id"), ob, company_id),
    )
    row = db.execute("SELECT client_id FROM client_companies WHERE id=?", (company_id,)).fetchone()
    if row:
        _sync_client_ob(db, row["client_id"])
    db.commit()


def get_company_balance(company_id, client_id):
    """Returns signed balance for a single company: positive = credit, negative = owes."""
    db = get_db()
    co = db.execute(
        "SELECT opening_balance FROM client_companies WHERE id=?", (company_id,)
    ).fetchone()
    if not co:
        return 0.0
    ob        = float(co["opening_balance"] or 0)
    credit_ob = max(0.0, -ob)
    debit_ob  = max(0.0, ob)
    total_invoiced = float(db.execute(
        "SELECT COALESCE(SUM(total), 0) AS s FROM invoices "
        "WHERE company_id=? AND client_id=? AND status != 'cancelled'",
        (company_id, client_id),
    ).fetchone()["s"])
    total_paid = float(db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payments "
        "WHERE company_id=? AND client_id=?",
        (company_id, client_id),
    ).fetchone()["s"])
    return (total_paid + credit_ob) - (total_invoiced + debit_ob)


def delete_company(company_id):
    db = get_db()
    row = db.execute("SELECT client_id FROM client_companies WHERE id=?", (company_id,)).fetchone()
    db.execute("UPDATE invoices SET company_id=NULL WHERE company_id=?", (company_id,))
    db.execute("UPDATE payments SET company_id=NULL WHERE company_id=?", (company_id,))
    db.execute("DELETE FROM client_companies WHERE id=?", (company_id,))
    if row:
        _sync_client_ob(db, row["client_id"])
    db.commit()

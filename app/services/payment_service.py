from ..database import get_db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_opening_balance_remaining(db, client_id):
    """How much of the client's opening balance debt is still unpaid.
    Both positive and negative opening_balance represent a debt to us."""
    client = db.execute(
        "SELECT opening_balance FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if not client:
        return 0.0
    opening_debt = abs(client["opening_balance"] or 0)
    if opening_debt == 0:
        return 0.0
    ob_paid = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE client_id=? AND invoice_id IS NULL",
        (client_id,),
    ).fetchone()["s"]
    return max(0.0, opening_debt - ob_paid)


def _oldest_unpaid_invoices(db, client_id):
    """Return non-cancelled invoices with a remaining balance, oldest first."""
    return db.execute(
        """SELECT id, total, amount_paid, (total - amount_paid) AS remaining
           FROM invoices
           WHERE client_id = ? AND status NOT IN ('paid','cancelled')
             AND (total - amount_paid) > 0
           ORDER BY issue_date ASC, id ASC""",
        (client_id,),
    ).fetchall()


def _refresh_invoice_paid(db, invoice_id):
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS paid FROM payments WHERE invoice_id=?",
        (invoice_id,),
    ).fetchone()
    paid = row["paid"]
    inv  = db.execute("SELECT total, status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    if paid <= 0:
        status = inv["status"] if inv["status"] == "draft" else "issued"
    elif paid >= inv["total"]:
        status = "paid"
    else:
        status = "partial"
    db.execute(
        "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
        (paid, status, invoice_id),
    )


# ── Public API ────────────────────────────────────────────────────────────────

def create_payment(data):
    """
    Record a payment against a client ledger.

    Rules (when invoice_id not explicitly set):
      1. Apply to unpaid opening balance first.
      2. Apply remainder to oldest unpaid invoices in order.

    If invoice_id IS supplied, apply directly to that invoice only.
    """
    db = get_db()
    client_id  = int(data["client_id"])
    amount     = float(data["amount"])
    explicit   = data.get("invoice_id")

    inserted_ids = []

    if explicit:
        # Direct allocation to a specific invoice
        cur = db.execute(
            """INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_id, int(explicit), amount,
             data["payment_date"], data.get("method","cash"),
             data.get("reference"), data.get("notes")),
        )
        inserted_ids.append(cur.lastrowid)
        _refresh_invoice_paid(db, int(explicit))
    else:
        remaining = amount

        # Step 1 — clear opening balance
        ob_remaining = _get_opening_balance_remaining(db, client_id)
        if ob_remaining > 0 and remaining > 0:
            apply = min(ob_remaining, remaining)
            cur = db.execute(
                """INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes)
                   VALUES (?, NULL, ?, ?, ?, ?, ?)""",
                (client_id, apply, data["payment_date"],
                 data.get("method","cash"), data.get("reference"), data.get("notes")),
            )
            inserted_ids.append(cur.lastrowid)
            remaining -= apply

        # Step 2 — apply to oldest unpaid invoices
        if remaining > 0:
            for inv in _oldest_unpaid_invoices(db, client_id):
                if remaining <= 0:
                    break
                apply = min(inv["remaining"], remaining)
                cur = db.execute(
                    """INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (client_id, inv["id"], apply, data["payment_date"],
                     data.get("method","cash"), data.get("reference"), data.get("notes")),
                )
                inserted_ids.append(cur.lastrowid)
                _refresh_invoice_paid(db, inv["id"])
                remaining -= apply

            # Step 3 — any leftover goes as unallocated (invoice_id NULL)
            if remaining > 0.001:
                cur = db.execute(
                    """INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes)
                       VALUES (?, NULL, ?, ?, ?, ?, ?)""",
                    (client_id, remaining, data["payment_date"],
                     data.get("method","cash"), data.get("reference"), data.get("notes")),
                )
                inserted_ids.append(cur.lastrowid)

    db.commit()
    return inserted_ids[0] if inserted_ids else None


def delete_payment(payment_id):
    db = get_db()
    row = db.execute("SELECT invoice_id FROM payments WHERE id=?", (payment_id,)).fetchone()
    db.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    db.commit()
    if row and row["invoice_id"]:
        _refresh_invoice_paid(db, row["invoice_id"])


def get_all_payments():
    return get_db().execute(
        """SELECT p.*, c.name AS client_name,
                  COALESCE(i.invoice_number, '— opening balance —') AS invoice_number
           FROM payments p
           JOIN clients c ON p.client_id = c.id
           LEFT JOIN invoices i ON p.invoice_id = i.id
           ORDER BY p.payment_date DESC, p.created_at DESC"""
    ).fetchall()


def get_payment(payment_id):
    return get_db().execute(
        """SELECT p.*, c.name AS client_name,
                  COALESCE(i.invoice_number, '— opening balance —') AS invoice_number
           FROM payments p
           JOIN clients c ON p.client_id = c.id
           LEFT JOIN invoices i ON p.invoice_id = i.id
           WHERE p.id=?""",
        (payment_id,),
    ).fetchone()


def get_client_payments(client_id):
    return get_db().execute(
        """SELECT p.*,
                  COALESCE(i.invoice_number, '— opening balance —') AS invoice_number
           FROM payments p
           LEFT JOIN invoices i ON p.invoice_id = i.id
           WHERE p.client_id = ?
           ORDER BY p.payment_date DESC, p.created_at DESC""",
        (client_id,),
    ).fetchall()


# ── Dashboard stats (unchanged) ───────────────────────────────────────────────

def get_dashboard_stats(date_from=None, date_to=None):
    db = get_db()
    windowed = bool(date_from and date_to)

    # ── Revenue: payments received in the window (or all-time) ──────────────
    if windowed:
        total_revenue = db.execute(
            "SELECT COALESCE(SUM(amount),0) AS v FROM payments WHERE payment_date BETWEEN ? AND ?",
            (date_from, date_to),
        ).fetchone()["v"]
        total_invoiced = db.execute(
            "SELECT COALESCE(SUM(total),0) AS v FROM invoices "
            "WHERE issue_date BETWEEN ? AND ? AND status != 'cancelled'",
            (date_from, date_to),
        ).fetchone()["v"]
        invoice_count = db.execute(
            "SELECT COUNT(*) AS v FROM invoices "
            "WHERE issue_date BETWEEN ? AND ? AND status != 'cancelled'",
            (date_from, date_to),
        ).fetchone()["v"]
    else:
        total_revenue  = db.execute(
            "SELECT COALESCE(SUM(amount),0) AS v FROM payments"
        ).fetchone()["v"]
        total_invoiced = None
        invoice_count  = None

    # ── Always-current stats ─────────────────────────────────────────────────
    outstanding = db.execute(
        "SELECT COALESCE(SUM(total - amount_paid),0) AS v "
        "FROM invoices WHERE status NOT IN ('paid','cancelled')"
    ).fetchone()["v"]

    overdue = db.execute(
        "SELECT COUNT(*) AS v FROM invoices "
        "WHERE status NOT IN ('paid','cancelled') AND due_date < date('now')"
    ).fetchone()["v"]

    total_clients = db.execute("SELECT COUNT(*) AS v FROM clients").fetchone()["v"]

    # ── Monthly revenue for chart ────────────────────────────────────────────
    if windowed:
        monthly = db.execute(
            "SELECT strftime('%Y-%m', payment_date) AS month, SUM(amount) AS total "
            "FROM payments WHERE payment_date BETWEEN ? AND ? "
            "GROUP BY month ORDER BY month",
            (date_from, date_to),
        ).fetchall()
    else:
        monthly = db.execute(
            "SELECT strftime('%Y-%m', payment_date) AS month, SUM(amount) AS total "
            "FROM payments WHERE payment_date >= date('now','-12 months') "
            "GROUP BY month ORDER BY month"
        ).fetchall()

    # ── Recent / windowed invoices ───────────────────────────────────────────
    if windowed:
        recent_invoices = db.execute(
            "SELECT i.*, c.name AS client_name FROM invoices i "
            "JOIN clients c ON i.client_id = c.id "
            "WHERE i.issue_date BETWEEN ? AND ? "
            "ORDER BY i.issue_date DESC, i.id DESC LIMIT 10",
            (date_from, date_to),
        ).fetchall()
    else:
        recent_invoices = db.execute(
            "SELECT i.*, c.name AS client_name FROM invoices i "
            "JOIN clients c ON i.client_id = c.id "
            "ORDER BY i.created_at DESC LIMIT 5"
        ).fetchall()

    return {
        "total_revenue":   total_revenue,
        "total_invoiced":  total_invoiced,
        "invoice_count":   invoice_count,
        "outstanding":     outstanding,
        "overdue_count":   overdue,
        "total_clients":   total_clients,
        "monthly_revenue": [dict(r) for r in monthly],
        "recent_invoices": recent_invoices,
    }

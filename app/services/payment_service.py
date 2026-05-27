from ..database import get_db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_unallocated_total(db, client_id):
    """Total payment money for the client that isn't tied to any invoice."""
    row = db.execute(
        """SELECT COALESCE(SUM(p.amount),0) - COALESCE(SUM(pa.amount),0) AS unallocated
           FROM payments p
           LEFT JOIN (
               SELECT payment_id, SUM(amount) AS amount
               FROM payment_allocations
               GROUP BY payment_id
           ) pa ON pa.payment_id = p.id
           WHERE p.client_id = ?""",
        (client_id,),
    ).fetchone()
    return float(row["unallocated"] or 0)


def _get_opening_balance_remaining(db, client_id):
    """How much of the client's opening balance debt is still unpaid.
    OB is covered by the unallocated portion of payments, capped at the OB amount."""
    client = db.execute(
        "SELECT opening_balance FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if not client:
        return 0.0
    opening_debt = abs(client["opening_balance"] or 0)
    if opening_debt == 0:
        return 0.0
    unallocated = _client_unallocated_total(db, client_id)
    return max(0.0, opening_debt - unallocated)


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
    """Recompute amount_paid + status for an invoice from payment_allocations."""
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS paid FROM payment_allocations WHERE invoice_id=?",
        (invoice_id,),
    ).fetchone()
    paid = float(row["paid"] or 0)
    inv = db.execute("SELECT total, status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    if paid <= 0:
        status = inv["status"] if inv["status"] == "draft" else "issued"
    elif paid >= float(inv["total"]):
        status = "paid"
    else:
        status = "partial"
    db.execute(
        "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
        (paid, status, invoice_id),
    )


# ── Public API ────────────────────────────────────────────────────────────────

def recalculate_client_balance(client_id):
    """Re-run payment allocation from scratch for a client.

    1. Clear payment_allocations for this client.
    2. Reset every non-cancelled invoice's amount_paid to 0 / status to 'issued'.
    3. Walk payments oldest-first; skip the OB-coverage portion; allocate the rest to
       the oldest unpaid invoices.
    4. Apply credit opening balance (ob < 0) directly to oldest invoices' amount_paid.
    """
    db = get_db()

    client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return

    ob = float(client["opening_balance"] or 0)
    ob_coverage = max(0.0, ob)
    ob_credit   = abs(min(0.0, ob))

    # 1 — clear allocations belonging to this client's payments
    db.execute(
        "DELETE FROM payment_allocations "
        "WHERE payment_id IN (SELECT id FROM payments WHERE client_id=?)",
        (client_id,),
    )
    # 2 — reset invoice paid amounts
    db.execute(
        """UPDATE invoices
              SET amount_paid = 0,
                  status = CASE WHEN status = 'draft' THEN 'draft' ELSE 'issued' END
            WHERE client_id = ? AND status != 'cancelled'""",
        (client_id,),
    )

    # 3 — allocate payment surplus (beyond OB) to invoices
    payments = db.execute(
        "SELECT id, amount, payment_date FROM payments "
        "WHERE client_id=? ORDER BY payment_date ASC, id ASC",
        (client_id,),
    ).fetchall()

    open_invoices = db.execute(
        "SELECT id, total FROM invoices "
        "WHERE client_id=? AND status NOT IN ('cancelled') "
        "ORDER BY issue_date ASC, id ASC",
        (client_id,),
    ).fetchall()
    inv_gaps  = {inv["id"]: float(inv["total"]) for inv in open_invoices}
    inv_order = [inv["id"] for inv in open_invoices]
    affected  = set()

    ob_budget = ob_coverage
    for pmt in payments:
        pmt_amount = float(pmt["amount"])
        ob_take    = min(pmt_amount, ob_budget)
        ob_budget -= ob_take
        release    = pmt_amount - ob_take
        if release < 0.001:
            continue
        remaining = release
        for inv_id in inv_order:
            if remaining < 0.001:
                break
            gap = inv_gaps.get(inv_id, 0)
            if gap < 0.001:
                continue
            alloc = min(remaining, gap)
            inv_gaps[inv_id] = gap - alloc
            remaining -= alloc
            db.execute(
                "INSERT INTO payment_allocations (payment_id, invoice_id, amount) VALUES (?,?,?)",
                (pmt["id"], inv_id, alloc),
            )
            affected.add(inv_id)

    # 4 — apply credit OB directly to oldest invoices (no payment record needed)
    if ob_credit > 0.001:
        remaining = ob_credit
        for inv_id in inv_order:
            if remaining < 0.001:
                break
            gap = inv_gaps.get(inv_id, 0)
            if gap < 0.001:
                continue
            alloc = min(gap, remaining)
            inv_gaps[inv_id] = gap - alloc
            remaining -= alloc
            inv_row = db.execute(
                "SELECT total, amount_paid FROM invoices WHERE id=?", (inv_id,)
            ).fetchone()
            new_paid = float(inv_row["amount_paid"]) + alloc
            new_status = "paid" if new_paid >= float(inv_row["total"]) else "partial"
            db.execute(
                "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
                (new_paid, new_status, inv_id),
            )

    for inv_id in affected:
        _refresh_invoice_paid(db, inv_id)

    db.commit()


def create_payment(data):
    """Record a payment against a client ledger as a single row.

    Allocations are inserted into payment_allocations:
      - If invoice_id is supplied → one allocation to that invoice.
      - Otherwise → opening-balance coverage stays unallocated; surplus is allocated
        to the oldest unpaid invoices in order; any leftover stays unallocated (credit).
    """
    db = get_db()
    client_id  = int(data["client_id"])
    amount     = float(data["amount"])
    explicit   = data.get("invoice_id")
    company_id = int(data["company_id"]) if data.get("company_id") else None

    cur = db.execute(
        """INSERT INTO payments (client_id, invoice_id, company_id, amount, payment_date, method, reference, notes)
           VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
        (client_id, company_id, amount, data["payment_date"],
         data.get("method", "cash"), data.get("reference"), data.get("notes")),
    )
    payment_id = cur.lastrowid

    affected = set()

    if explicit:
        db.execute(
            "INSERT INTO payment_allocations (payment_id, invoice_id, amount) VALUES (?,?,?)",
            (payment_id, int(explicit), amount),
        )
        affected.add(int(explicit))
    else:
        remaining = amount

        # Skip the portion that goes toward opening balance — leave it unallocated.
        ob_remaining = _get_opening_balance_remaining(db, client_id)
        # _get_opening_balance_remaining already accounts for the row we just inserted
        # (which is fully unallocated). Subtract the share that came from THIS payment.
        # Simpler: recompute ignoring this payment.
        prior_unallocated = _client_unallocated_total(db, client_id) - amount
        client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
        opening_debt = abs(float(client["opening_balance"] or 0)) if client else 0.0
        ob_already_covered = min(prior_unallocated, opening_debt)
        ob_gap = max(0.0, opening_debt - ob_already_covered)
        ob_take = min(remaining, ob_gap)
        remaining -= ob_take

        if remaining > 0.001:
            for inv in _oldest_unpaid_invoices(db, client_id):
                if remaining < 0.001:
                    break
                apply = min(float(inv["remaining"]), remaining)
                db.execute(
                    "INSERT INTO payment_allocations (payment_id, invoice_id, amount) VALUES (?,?,?)",
                    (payment_id, inv["id"], apply),
                )
                affected.add(inv["id"])
                remaining -= apply
        # Any further leftover stays unallocated on the payment row → credit.

    for inv_id in affected:
        _refresh_invoice_paid(db, inv_id)

    db.commit()
    return payment_id


def delete_payment(payment_id):
    db = get_db()
    affected = [r["invoice_id"] for r in db.execute(
        "SELECT invoice_id FROM payment_allocations WHERE payment_id=?", (payment_id,)
    ).fetchall()]
    db.execute("DELETE FROM payment_allocations WHERE payment_id=?", (payment_id,))
    db.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    db.commit()
    for inv_id in affected:
        _refresh_invoice_paid(db, inv_id)
    db.commit()


def get_all_payments():
    return get_db().execute(
        """SELECT p.*, c.name AS client_name,
                  (SELECT GROUP_CONCAT(i.invoice_number, ', ')
                     FROM payment_allocations pa
                     JOIN invoices i ON i.id = pa.invoice_id
                    WHERE pa.payment_id = p.id) AS invoice_number,
                  (SELECT COALESCE(SUM(amount),0) FROM payment_allocations WHERE payment_id=p.id) AS allocated
           FROM payments p
           JOIN clients c ON p.client_id = c.id
           ORDER BY p.payment_date DESC, p.created_at DESC"""
    ).fetchall()


def get_payment(payment_id):
    return get_db().execute(
        """SELECT p.*, c.name AS client_name,
                  (SELECT GROUP_CONCAT(i.invoice_number, ', ')
                     FROM payment_allocations pa
                     JOIN invoices i ON i.id = pa.invoice_id
                    WHERE pa.payment_id = p.id) AS invoice_number,
                  (SELECT COALESCE(SUM(amount),0) FROM payment_allocations WHERE payment_id=p.id) AS allocated
           FROM payments p
           JOIN clients c ON p.client_id = c.id
           WHERE p.id=?""",
        (payment_id,),
    ).fetchone()


def get_client_payments(client_id):
    return get_db().execute(
        """SELECT p.*,
                  (SELECT GROUP_CONCAT(i.invoice_number, ', ')
                     FROM payment_allocations pa
                     JOIN invoices i ON i.id = pa.invoice_id
                    WHERE pa.payment_id = p.id) AS invoice_number,
                  (SELECT COALESCE(SUM(amount),0) FROM payment_allocations WHERE payment_id=p.id) AS allocated
           FROM payments p
           WHERE p.client_id = ?
           ORDER BY p.payment_date DESC, p.created_at DESC""",
        (client_id,),
    ).fetchall()


def get_payment_allocations(payment_id):
    """Detail rows showing which invoices a payment was applied against."""
    return get_db().execute(
        """SELECT pa.invoice_id, pa.amount, i.invoice_number, i.issue_date, i.total
           FROM payment_allocations pa
           JOIN invoices i ON i.id = pa.invoice_id
           WHERE pa.payment_id = ?
           ORDER BY i.issue_date, i.id""",
        (payment_id,),
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
        total_tax = db.execute(
            "SELECT COALESCE(SUM(tax_total),0) AS v FROM invoices "
            "WHERE issue_date BETWEEN ? AND ? AND status != 'cancelled'",
            (date_from, date_to),
        ).fetchone()["v"]
        payment_count = db.execute(
            "SELECT COUNT(*) AS v FROM payments WHERE payment_date BETWEEN ? AND ?",
            (date_from, date_to),
        ).fetchone()["v"]
        unique_payers = db.execute(
            "SELECT COUNT(DISTINCT client_id) AS v FROM payments WHERE payment_date BETWEEN ? AND ?",
            (date_from, date_to),
        ).fetchone()["v"]
    else:
        total_revenue  = db.execute(
            "SELECT COALESCE(SUM(amount),0) AS v FROM payments"
        ).fetchone()["v"]
        total_invoiced = None
        invoice_count  = None
        total_tax      = None
        payment_count  = None
        unique_payers  = None

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

    # ── Daily sales and revenue for chart ───────────────────────────────────
    if windowed:
        daily_sales = db.execute(
            "SELECT issue_date AS day, SUM(total) AS total "
            "FROM invoices WHERE issue_date BETWEEN ? AND ? AND status != 'cancelled' "
            "GROUP BY day ORDER BY day",
            (date_from, date_to),
        ).fetchall()
        daily_revenue = db.execute(
            "SELECT payment_date AS day, SUM(amount) AS total "
            "FROM payments WHERE payment_date BETWEEN ? AND ? "
            "GROUP BY day ORDER BY day",
            (date_from, date_to),
        ).fetchall()
    else:
        daily_sales = db.execute(
            "SELECT issue_date AS day, SUM(total) AS total "
            "FROM invoices WHERE status != 'cancelled' "
            "GROUP BY day ORDER BY day"
        ).fetchall()
        daily_revenue = db.execute(
            "SELECT payment_date AS day, SUM(amount) AS total "
            "FROM payments "
            "GROUP BY day ORDER BY day"
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

    # Merge daily sales and revenue by day
    sales_dict = {dict(r)['day']: dict(r)['total'] for r in daily_sales}
    revenue_dict = {dict(r)['day']: dict(r)['total'] for r in daily_revenue}
    all_days = sorted(set(sales_dict.keys()) | set(revenue_dict.keys()))
    monthly_merged = [
        {
            'month': d,
            'sales': sales_dict.get(d, 0),
            'revenue': revenue_dict.get(d, 0)
        }
        for d in all_days
    ]

    return {
        "total_revenue":   total_revenue,
        "total_invoiced":  total_invoiced,
        "invoice_count":   invoice_count,
        "total_tax":       total_tax,
        "payment_count":   payment_count,
        "unique_payers":   unique_payers,
        "outstanding":     outstanding,
        "overdue_count":   overdue,
        "total_clients":   total_clients,
        "monthly_revenue": monthly_merged,
        "recent_invoices": recent_invoices,
    }

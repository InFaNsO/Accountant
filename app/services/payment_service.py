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

def recalculate_client_balance(client_id):
    """
    Re-run payment allocation from scratch for a client.

    Steps:
      1. Reset every payment's invoice_id to NULL.
      2. Reset every non-cancelled invoice's amount_paid to 0 / status to 'issued'.
      3. Re-allocate: opening-balance coverage (NULL) first, then oldest invoices.
      4. Apply credit opening balance (ob < 0) directly to oldest invoices.
      5. Commit.
    """
    db = get_db()

    client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return

    ob = float(client["opening_balance"] or 0)
    # Positive OB = client owes us; needs NULL-payment coverage
    ob_coverage = max(0.0, ob)
    # Negative OB = client pre-paid credit; applies directly to oldest invoices
    ob_credit   = abs(min(0.0, ob))

    # 1 & 2 — reset all allocations
    db.execute("UPDATE payments SET invoice_id=NULL WHERE client_id=?", (client_id,))
    db.execute(
        """UPDATE invoices
           SET amount_paid = 0,
               status = CASE WHEN status = 'draft' THEN 'draft' ELSE 'issued' END
           WHERE client_id = ? AND status != 'cancelled'""",
        (client_id,),
    )

    total_payments = float(db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE client_id=?",
        (client_id,),
    ).fetchone()[0])

    # 3 — re-allocate surplus beyond OB coverage to invoices
    keep_for_ob = min(total_payments, ob_coverage)
    to_release  = total_payments - keep_for_ob

    if to_release > 0.001:
        _release_null_to_invoices(db, client_id, keep_for_ob, to_release)

    # 4 — apply credit OB directly to oldest invoices (no payment record needed)
    if ob_credit > 0.001:
        _apply_credit_ob_to_invoices(db, client_id, ob_credit)

    db.commit()


def _release_null_to_invoices(db, client_id, keep_for_ob, to_release):
    """Walk NULL payments oldest-first; skip the OB-coverage portion; send the rest to invoices."""
    null_pmts = db.execute(
        "SELECT id, amount, payment_date, method, reference, notes "
        "FROM payments WHERE client_id=? AND invoice_id IS NULL "
        "ORDER BY payment_date ASC, id ASC",
        (client_id,),
    ).fetchall()

    open_invoices = db.execute(
        "SELECT id, total, amount_paid FROM invoices "
        "WHERE client_id=? AND status NOT IN ('cancelled') AND total > amount_paid "
        "ORDER BY issue_date ASC, id ASC",
        (client_id,),
    ).fetchall()

    if not null_pmts or not open_invoices:
        return

    affected  = set()
    ob_budget = keep_for_ob
    inv_gaps  = {inv["id"]: float(inv["total"]) - float(inv["amount_paid"]) for inv in open_invoices}
    inv_order = [inv["id"] for inv in open_invoices]

    for pmt in null_pmts:
        pmt_amount     = float(pmt["amount"])
        ob_take        = min(pmt_amount, max(0.0, ob_budget))
        ob_budget     -= ob_take
        release_amount = pmt_amount - ob_take

        if release_amount < 0.001:
            continue

        if ob_take < 0.001:
            # Entire payment is released — reuse the record for the first invoice
            remaining  = release_amount
            first_used = False
            for inv_id in inv_order:
                if remaining < 0.001:
                    break
                if inv_gaps.get(inv_id, 0) < 0.001:
                    continue
                alloc = min(remaining, inv_gaps[inv_id])
                inv_gaps[inv_id] -= alloc
                remaining -= alloc
                affected.add(inv_id)
                if not first_used:
                    db.execute(
                        "UPDATE payments SET invoice_id=?, amount=? WHERE id=?",
                        (inv_id, alloc, pmt["id"]),
                    )
                    first_used = True
                else:
                    db.execute(
                        "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (client_id, inv_id, alloc, pmt["payment_date"],
                         pmt["method"] or "cash", pmt["reference"], pmt["notes"]),
                    )
        else:
            # Split: ob_take stays NULL (reduce original), release_amount goes to invoices
            db.execute("UPDATE payments SET amount=? WHERE id=?", (ob_take, pmt["id"]))
            remaining = release_amount
            for inv_id in inv_order:
                if remaining < 0.001:
                    break
                if inv_gaps.get(inv_id, 0) < 0.001:
                    continue
                alloc = min(remaining, inv_gaps[inv_id])
                inv_gaps[inv_id] -= alloc
                remaining -= alloc
                affected.add(inv_id)
                db.execute(
                    "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (client_id, inv_id, alloc, pmt["payment_date"],
                     pmt["method"] or "cash", pmt["reference"], pmt["notes"]),
                )

    for inv_id in affected:
        _refresh_invoice_paid(db, inv_id)


def _apply_credit_ob_to_invoices(db, client_id, credit_amount):
    """Apply a credit opening balance directly to oldest invoices by updating amount_paid.
    No payment record is created — the OB itself is the source of credit."""
    invoices = db.execute(
        "SELECT id, total, amount_paid FROM invoices "
        "WHERE client_id=? AND status NOT IN ('cancelled') AND total > amount_paid "
        "ORDER BY issue_date ASC, id ASC",
        (client_id,),
    ).fetchall()

    remaining = credit_amount
    for inv in invoices:
        if remaining < 0.001:
            break
        gap   = float(inv["total"]) - float(inv["amount_paid"])
        alloc = min(gap, remaining)
        remaining -= alloc
        new_paid = float(inv["amount_paid"]) + alloc
        new_status = "paid" if new_paid >= float(inv["total"]) else "partial"
        db.execute(
            "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
            (new_paid, new_status, inv["id"]),
        )


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
    company_id = int(data["company_id"]) if data.get("company_id") else None

    inserted_ids = []

    if explicit:
        # Direct allocation to a specific invoice
        cur = db.execute(
            """INSERT INTO payments (client_id, invoice_id, company_id, amount, payment_date, method, reference, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_id, int(explicit), company_id, amount,
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
                """INSERT INTO payments (client_id, invoice_id, company_id, amount, payment_date, method, reference, notes)
                   VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
                (client_id, company_id, apply, data["payment_date"],
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
                    """INSERT INTO payments (client_id, invoice_id, company_id, amount, payment_date, method, reference, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (client_id, inv["id"], company_id, apply, data["payment_date"],
                     data.get("method","cash"), data.get("reference"), data.get("notes")),
                )
                inserted_ids.append(cur.lastrowid)
                _refresh_invoice_paid(db, inv["id"])
                remaining -= apply

            # Step 3 — any leftover goes as unallocated (invoice_id NULL)
            if remaining > 0.001:
                cur = db.execute(
                    """INSERT INTO payments (client_id, invoice_id, company_id, amount, payment_date, method, reference, notes)
                       VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
                    (client_id, company_id, remaining, data["payment_date"],
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

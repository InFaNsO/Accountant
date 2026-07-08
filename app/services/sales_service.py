"""Sales-manager dashboard: stats and per-staff breakdown scoped to a manager's
team of sales reps and the clients they manage. All money figures are in ₹.
"""
from ..database import get_db

# UTC → IST for visit timestamps (visits store UTC; the business runs on IST)
_IST = "'+5 hours', '+30 minutes'"


def _in(ids):
    """Return a ('(?,?,…)', params) tuple for an IN clause; empty → matches none."""
    ids = list(ids)
    if not ids:
        return "(NULL)", []
    return "(" + ",".join("?" * len(ids)) + ")", ids


def get_scoped_stats(client_ids, date_from=None, date_to=None):
    """Headline numbers for a sales manager: sales (invoiced) and money collected
    in the window, plus current outstanding, over the given set of clients."""
    db = get_db()
    ph, cids = _in(client_ids)
    windowed = bool(date_from and date_to)

    if windowed:
        total_invoiced = db.execute(
            f"SELECT COALESCE(SUM(total),0) v FROM invoices "
            f"WHERE client_id IN {ph} AND issue_date BETWEEN ? AND ? AND status != 'cancelled'",
            (*cids, date_from, date_to),
        ).fetchone()["v"]
        invoice_count = db.execute(
            f"SELECT COUNT(*) v FROM invoices "
            f"WHERE client_id IN {ph} AND issue_date BETWEEN ? AND ? AND status != 'cancelled'",
            (*cids, date_from, date_to),
        ).fetchone()["v"]
        total_collected = db.execute(
            f"SELECT COALESCE(SUM(amount),0) v FROM payments "
            f"WHERE client_id IN {ph} AND payment_date BETWEEN ? AND ?",
            (*cids, date_from, date_to),
        ).fetchone()["v"]
        payment_count = db.execute(
            f"SELECT COUNT(*) v FROM payments "
            f"WHERE client_id IN {ph} AND payment_date BETWEEN ? AND ?",
            (*cids, date_from, date_to),
        ).fetchone()["v"]
    else:
        total_invoiced = None
        invoice_count = None
        total_collected = db.execute(
            f"SELECT COALESCE(SUM(amount),0) v FROM payments WHERE client_id IN {ph}",
            cids,
        ).fetchone()["v"]
        payment_count = None

    outstanding = db.execute(
        f"SELECT COALESCE(SUM(total - amount_paid),0) v FROM invoices "
        f"WHERE client_id IN {ph} AND status NOT IN ('paid','cancelled')",
        cids,
    ).fetchone()["v"]
    overdue = db.execute(
        f"SELECT COUNT(*) v FROM invoices "
        f"WHERE client_id IN {ph} AND status NOT IN ('paid','cancelled') AND due_date < date('now')",
        cids,
    ).fetchone()["v"]
    client_count = len(list(client_ids))

    # Chart: daily sales (invoiced) and revenue (collected)
    if windowed:
        daily_sales = db.execute(
            f"SELECT issue_date day, SUM(total) total FROM invoices "
            f"WHERE client_id IN {ph} AND issue_date BETWEEN ? AND ? AND status != 'cancelled' "
            f"GROUP BY day ORDER BY day",
            (*cids, date_from, date_to),
        ).fetchall()
        daily_rev = db.execute(
            f"SELECT payment_date day, SUM(amount) total FROM payments "
            f"WHERE client_id IN {ph} AND payment_date BETWEEN ? AND ? "
            f"GROUP BY day ORDER BY day",
            (*cids, date_from, date_to),
        ).fetchall()
    else:
        daily_sales = db.execute(
            f"SELECT issue_date day, SUM(total) total FROM invoices "
            f"WHERE client_id IN {ph} AND status != 'cancelled' GROUP BY day ORDER BY day",
            cids,
        ).fetchall()
        daily_rev = db.execute(
            f"SELECT payment_date day, SUM(amount) total FROM payments "
            f"WHERE client_id IN {ph} GROUP BY day ORDER BY day",
            cids,
        ).fetchall()

    sales_by_day = {r["day"]: r["total"] for r in daily_sales}
    rev_by_day   = {r["day"]: r["total"] for r in daily_rev}
    all_days = sorted(set(sales_by_day) | set(rev_by_day))
    chart = [{"month": d, "sales": sales_by_day.get(d, 0), "revenue": rev_by_day.get(d, 0)}
             for d in all_days]

    return {
        "total_invoiced":  total_invoiced,
        "invoice_count":   invoice_count,
        "total_collected": total_collected,
        "payment_count":   payment_count,
        "outstanding":     outstanding,
        "overdue_count":   overdue,
        "client_count":    client_count,
        "chart":           chart,
    }


def get_team_breakdown(staff, date_from=None, date_to=None):
    """Per-staff-member figures for a manager's team. `staff` is a list of user
    rows/dicts (id, name). For each: clients managed, sales (invoiced) and
    collected in the window over their clients, and visit count in the window."""
    db = get_db()
    windowed = bool(date_from and date_to)
    out = []
    for member in staff:
        uid = member["id"]
        client_ids = [r["id"] for r in db.execute(
            "SELECT id FROM clients WHERE sales_rep_id=?", (uid,)
        ).fetchall()]
        ph, cids = _in(client_ids)

        if windowed:
            sales = db.execute(
                f"SELECT COALESCE(SUM(total),0) v FROM invoices "
                f"WHERE client_id IN {ph} AND issue_date BETWEEN ? AND ? AND status != 'cancelled'",
                (*cids, date_from, date_to),
            ).fetchone()["v"]
            collected = db.execute(
                f"SELECT COALESCE(SUM(amount),0) v FROM payments "
                f"WHERE client_id IN {ph} AND payment_date BETWEEN ? AND ?",
                (*cids, date_from, date_to),
            ).fetchone()["v"]
            visits = db.execute(
                f"SELECT COUNT(*) v FROM client_visits "
                f"WHERE user_id=? AND date(checked_in_at, {_IST}) BETWEEN ? AND ?",
                (uid, date_from, date_to),
            ).fetchone()["v"]
        else:
            sales = db.execute(
                f"SELECT COALESCE(SUM(total),0) v FROM invoices "
                f"WHERE client_id IN {ph} AND status != 'cancelled'", cids,
            ).fetchone()["v"]
            collected = db.execute(
                f"SELECT COALESCE(SUM(amount),0) v FROM payments WHERE client_id IN {ph}", cids,
            ).fetchone()["v"]
            visits = db.execute(
                "SELECT COUNT(*) v FROM client_visits WHERE user_id=?", (uid,),
            ).fetchone()["v"]

        outstanding = db.execute(
            f"SELECT COALESCE(SUM(total - amount_paid),0) v FROM invoices "
            f"WHERE client_id IN {ph} AND status NOT IN ('paid','cancelled')", cids,
        ).fetchone()["v"]

        out.append({
            "id":           uid,
            "name":         member["name"],
            "is_active":    member.get("is_active", 1) if isinstance(member, dict) else member["is_active"],
            "client_count": len(client_ids),
            "sales":        sales,
            "collected":    collected,
            "outstanding":  outstanding,
            "visits":       visits,
        })
    return out

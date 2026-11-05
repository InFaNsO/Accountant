import sqlite3
from datetime import date
from ..database import get_db
from . import product_service
from .payment_service import _refresh_invoice_paid, apply_client_credit

# Statuses of an invoice that is out: it has its INV serial and its stock is gone.
LIVE_STATUSES = ("issued", "sent", "partial", "paid")


class AlreadySaved(Exception):
    """The new-invoice form carrying this token was saved before (browser Back,
    double click, retry). `invoice_id` is the invoice that save created."""

    def __init__(self, invoice_id):
        super().__init__(invoice_id)
        self.invoice_id = invoice_id


def _invoice_id_for_token(db, token):
    row = db.execute("SELECT id FROM invoices WHERE form_token = ?", (token,)).fetchone()
    return row["id"] if row else None


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


def _item_name(db, pid, spid, fallback=""):
    """'Parent — Variant' for a sub-product, the product name otherwise."""
    row = db.execute(
        """SELECT p.name AS product_name, sp.name AS sub_name
           FROM (SELECT ? AS pid, ? AS spid) k
           LEFT JOIN sub_products sp ON sp.id = k.spid
           LEFT JOIN products p ON p.id = COALESCE(sp.product_id, k.pid)""",
        (pid, spid),
    ).fetchone()
    if row["sub_name"] and row["product_name"]:
        return f"{row['product_name']} — {row['sub_name']}"
    return row["sub_name"] or row["product_name"] or fallback


def _warehouse_qty(db, pid, spid):
    tbl = "sub_products" if spid else "products"
    row = db.execute(f"SELECT stock_qty FROM {tbl} WHERE id=?", (spid or pid,)).fetchone()
    return float(row["stock_qty"] or 0) if row else 0.0


def _move_stock(db, invoice_id, pid, spid, qty, movement, note):
    """Take `qty` out of the warehouse ('sale') or put it back ('sale_cancelled'),
    logging the stock movement against the invoice."""
    tbl = "sub_products" if spid else "products"
    sign = -1 if movement == "sale" else 1
    db.execute(f"UPDATE {tbl} SET stock_qty = stock_qty + ? WHERE id = ?", (sign * qty, spid or pid))
    db.execute(
        "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes, invoice_id) VALUES (?,?,?,?,?,?)",
        (pid, spid, movement, qty, note, invoice_id),
    )


def _linked_qty(lines):
    """{(product_id, sub_product_id): total quantity} over the lines that are linked
    to the catalog. Two lines of the same item add up."""
    out = {}
    for it in lines:
        pid  = int(it["product_id"]) if it["product_id"] else None
        spid = int(it["sub_product_id"]) if it["sub_product_id"] else None
        if pid or spid:
            out[(pid, spid)] = out.get((pid, spid), 0.0) + float(it["quantity"])
    return out


def _invoice_lines(db, invoice_id):
    return db.execute(
        "SELECT product_id, sub_product_id, quantity, description FROM invoice_items WHERE invoice_id=? ORDER BY id",
        (invoice_id,),
    ).fetchall()


def check_stock_for_issue(invoice_id):
    """Warehouse check for issuing: one entry per catalog item on the invoice, its
    lines added up. Each dict: {name, description, product_id, sub_product_id,
    required, available, sufficient}. Lines not linked to the catalog are left out.
    """
    db = get_db()
    lines = _invoice_lines(db, invoice_id)
    first_desc = {}
    for it in lines:
        key = (it["product_id"] or None, it["sub_product_id"] or None)
        first_desc.setdefault(key, it["description"])
    result = []
    for (pid, spid), required in _linked_qty(lines).items():
        available = _warehouse_qty(db, pid, spid)
        result.append({
            "name":           _item_name(db, pid, spid, first_desc.get((pid, spid), "")),
            "description":    first_desc.get((pid, spid), ""),
            "product_id":     pid,
            "sub_product_id": spid,
            "required":       required,
            "available":      available,
            "sufficient":     available >= required - 1e-9,
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


# A bill typed in twice looks like this: same client, same total, saved within a
# few days. Only ever a prompt before saving or issuing (repeat orders can match).
DUPLICATE_WINDOW_DAYS = 7


def find_possible_duplicate(client_id, total, exclude_id=None):
    """Newest live (not cancelled) invoice for the client whose total is within
    Rs 1 of `total`, created in the last DUPLICATE_WINDOW_DAYS days, or None."""
    return get_db().execute(
        """SELECT i.id, i.invoice_number, i.status, i.total, i.issue_date, i.created_at,
                  c.name AS client_name
           FROM invoices i JOIN clients c ON c.id = i.client_id
           WHERE i.client_id = ? AND i.status != 'cancelled'
             AND ABS(i.total - ?) < 1
             AND i.created_at >= datetime('now', ?)
             AND i.id != ?
           ORDER BY i.id DESC LIMIT 1""",
        (int(client_id), float(total), f"-{DUPLICATE_WINDOW_DAYS} days", int(exclude_id or 0)),
    ).fetchone()


def find_possible_duplicate_for_form(data, items, exclude_id=None):
    """find_possible_duplicate for a submitted invoice form, using the same total
    create_invoice / update_invoice would store."""
    inv_dtype = (data.get("discount_type") or "value").lower()
    inv_dval  = float(data.get("discount_amount", 0) or 0)
    total = _compute_totals(items, inv_dval, inv_dtype)[3]
    return find_possible_duplicate(data["client_id"], total, exclude_id)


def create_invoice(data, items):
    """Create an invoice and return its id. Raises AlreadySaved when
    data["form_token"] was already used, so one form can only ever make one invoice."""
    db = get_db()
    form_token = (data.get("form_token") or "").strip() or None
    if form_token:
        existing = _invoice_id_for_token(db, form_token)
        if existing:
            raise AlreadySaved(existing)
    is_draft = data.get("status", "issued") == "draft"
    # A locked client can still get drafts, never a direct issue — demote silently;
    # callers that want to warn the user pre-check get_client_lock_status themselves.
    if not is_draft:
        from .client_service import get_client_lock_status
        if get_client_lock_status(int(data["client_id"]))["locked"]:
            is_draft = True
            data = {**data, "status": "draft"}
    invoice_number = _next_draft_number(db) if is_draft else _next_invoice_number(db)

    inv_dtype = (data.get("discount_type") or "value").lower()
    inv_dval  = float(data.get("discount_amount", 0) or 0)
    subtotal, tax_total, discount, total = _compute_totals(items, inv_dval, inv_dtype)

    company_id = int(data["company_id"]) if data.get("company_id") else None

    try:
        cur = db.execute(
            """INSERT INTO invoices (invoice_number, client_id, company_id, status, issue_date, due_date,
               notes, subtotal, tax_total, discount_amount, discount_type, discount_value, total,
               form_token)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                invoice_number, data["client_id"], company_id,
                data.get("status", "issued"),
                data.get("issue_date", str(date.today())),
                data.get("due_date"),
                data.get("notes"),
                subtotal, tax_total, discount, inv_dtype, inv_dval, total,
                form_token,
            ),
        )
    except sqlite3.IntegrityError:
        # Two POSTs of the same form arrived together and the other one committed
        # first: the unique index on form_token refuses this copy.
        db.rollback()
        existing = form_token and _invoice_id_for_token(db, form_token)
        if existing:
            raise AlreadySaved(existing)
        raise
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
            _move_stock(db, invoice_id, pid, spid, qty, "sale", f"Invoice {invoice_number}")

    # Credit the client already has goes on an invoice that is out (a draft
    # waits until it is issued).
    if not is_draft:
        if company_id:
            issue_date = data.get("issue_date", str(date.today()))
            _apply_company_ob_credit(db, int(data["client_id"]), company_id, invoice_id, issue_date)
        apply_client_credit(db, int(data["client_id"]), invoice_id)
    db.commit()
    return invoice_id


def update_invoice_status(invoice_id, status):
    """Move an invoice to `status`. Returns (ok, errors).

    Only issuing takes an invoice out of draft: choosing any live status for a draft
    (issued, sent, partial, paid) issues it, and so does reopening a cancelled
    invoice. Nothing goes back to draft once issued (errors type 'undraft'): its
    serial is given and its stock is gone, so cancel it instead.
    errors also holds 'locked' dicts and stock-shortage dicts (see check_stock_for_issue).
    """
    db = get_db()
    inv = db.execute(
        "SELECT status, invoice_number, client_id, company_id, issue_date FROM invoices WHERE id=?",
        (invoice_id,),
    ).fetchone()
    if not inv:
        return True, []
    prev_status = inv["status"]

    if status == "draft":
        if prev_status == "draft":
            return True, []
        return False, [{"type": "undraft", "number": inv["invoice_number"]}]

    if prev_status in ("draft", "cancelled") and status in LIVE_STATUSES:
        return _issue(db, inv, invoice_id)

    db.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
    # Restore stock when cancelling an invoice that took it (a draft never did)
    if status == "cancelled" and prev_status in LIVE_STATUSES:
        for it in _invoice_lines(db, invoice_id):
            if it["product_id"] or it["sub_product_id"]:
                _move_stock(db, invoice_id, it["product_id"], it["sub_product_id"], float(it["quantity"]),
                            "sale_cancelled", f"Cancelled: Invoice {inv['invoice_number']}")
    db.commit()
    return True, []


def _issue(db, inv, invoice_id):
    """Send out a draft, or reopen a cancelled invoice. Checks the client lock and
    the warehouse, gives a draft its real serial (D-### becomes the next INV-####, so
    issued invoices are numbered in true issue order), takes the stock, applies any
    credit waiting for the client, then lets the payments decide issued / partial /
    paid: money recorded against the draft counts from here. All in one commit."""
    from .client_service import get_client_lock_status
    lock = get_client_lock_status(inv["client_id"])
    if lock["locked"]:
        return False, [{"type": "locked", "reasons": lock["reasons"]}]
    short = [s for s in check_stock_for_issue(invoice_id) if not s["sufficient"]]
    if short:
        return False, short

    number = (_next_invoice_number(db)
              if str(inv["invoice_number"]).startswith("D-")
              else inv["invoice_number"])
    for it in _invoice_lines(db, invoice_id):
        if it["product_id"] or it["sub_product_id"]:
            _move_stock(db, invoice_id, it["product_id"], it["sub_product_id"], float(it["quantity"]),
                        "sale", f"Invoice {number}")
    db.execute(
        """UPDATE invoices SET status='issued', invoice_number=?,
                  issued_at=COALESCE(issued_at, CURRENT_TIMESTAMP)
           WHERE id=?""",
        (number, invoice_id),
    )
    if inv["company_id"]:
        _apply_company_ob_credit(db, inv["client_id"], inv["company_id"], invoice_id, inv["issue_date"])
    apply_client_credit(db, inv["client_id"], invoice_id)
    _refresh_invoice_paid(db, invoice_id)
    db.commit()
    return True, []


def update_invoice(invoice_id, data, items):
    """Save the edit form. Returns (ok, errors) like update_invoice_status.

    Header and lines are saved under the invoice's current status, then a status
    change goes through update_invoice_status, which owns every move between
    statuses (issuing a draft, cancelling, ...). A draft holds no stock, so its lines
    are simply rewritten. On a live invoice the warehouse follows the lines: more of
    an item takes the difference out of stock, less (or a removed line) puts it back.
    If stock cannot cover an increase nothing is saved (errors type 'short_edit').
    """
    db = get_db()
    prev = db.execute(
        "SELECT status, invoice_number, amount_paid FROM invoices WHERE id=?", (invoice_id,)
    ).fetchone()
    if not prev:
        return False, [{"type": "missing"}]
    prev_status = prev["status"]
    live = prev_status in LIVE_STATUSES
    requested_status = data.get("status") or prev_status

    deltas = {}
    if live:
        before = _linked_qty(_invoice_lines(db, invoice_id))
        after  = _linked_qty(items)
        deltas = {k: after.get(k, 0.0) - before.get(k, 0.0) for k in set(before) | set(after)}
        deltas = {k: d for k, d in deltas.items() if abs(d) > 1e-9}
        short = []
        for (pid, spid), d in deltas.items():
            available = _warehouse_qty(db, pid, spid)
            if d > 0 and available < d - 1e-9:
                short.append({"type": "short_edit", "name": _item_name(db, pid, spid),
                              "required": d, "available": available})
        if short:
            return False, short

    inv_dtype = (data.get("discount_type") or "value").lower()
    inv_dval  = float(data.get("discount_amount", 0) or 0)
    subtotal, tax_total, discount, total = _compute_totals(items, inv_dval, inv_dtype)

    company_id = int(data["company_id"]) if data.get("company_id") else None

    db.execute(
        """UPDATE invoices SET client_id=?, company_id=?, issue_date=?, due_date=?,
           notes=?, subtotal=?, tax_total=?, discount_amount=?, discount_type=?, discount_value=?, total=?
           WHERE id=?""",
        (
            data["client_id"], company_id,
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
    for (pid, spid), d in deltas.items():
        _move_stock(db, invoice_id, pid, spid, abs(d), "sale" if d > 0 else "sale_cancelled",
                    f"Edited: Invoice {prev['invoice_number']}")
    # With payments on it, a live invoice's paid / partial follows its new total.
    if live and float(prev["amount_paid"] or 0) > 0:
        _refresh_invoice_paid(db, invoice_id)
    db.commit()

    if requested_status != prev_status:
        return update_invoice_status(invoice_id, requested_status)
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

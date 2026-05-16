"""
Ledger MCP Server — Full God-Level Access
──────────────────────────────────────────
Create / read / update any entry, same as the god account.
Delete operations always require explicit user permission before the tool is called.

Setup (Claude Desktop) — edit %APPDATA%/Claude/claude_desktop_config.json:
{
  "mcpServers": {
    "ledger": {
      "command": "C:/Users/Bhavil/miniconda3/python.exe",
      "args": ["D:/Code/Claude/Accountant/mcp_server.py"]
    }
  }
}
"""

import sqlite3
from pathlib import Path
from datetime import date, datetime
from mcp.server.fastmcp import FastMCP

DB_PATH = Path(__file__).parent / "data" / "ledger.db"


# ───────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ───────────────────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _inr(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    return f"₹{abs(n):,.2f}"


def _f(v, default=0.0):
    """Coerce value to float, tolerating None and empty strings."""
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _next_invoice_number(db):
    row = db.execute(
        "SELECT invoice_number FROM invoices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "INV-0001"
    try:
        num = int(row["invoice_number"].split("-")[-1]) + 1
    except ValueError:
        num = 1
    return f"INV-{num:04d}"


def _refresh_invoice_paid(db, invoice_id):
    """Recompute amount_paid and status for an invoice."""
    paid = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS paid FROM payments WHERE invoice_id=?",
        (invoice_id,),
    ).fetchone()["paid"]
    inv = db.execute("SELECT total FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    status = "paid" if paid >= inv["total"] else ("partial" if paid > 0 else "issued")
    db.execute(
        "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
        (paid, status, invoice_id),
    )


def _apply_client_credit(db, client_id, invoice_id):
    """Auto-apply any unallocated credit payments to a newly created invoice."""
    client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    opening_debt = max(0.0, _f(client["opening_balance"])) if client else 0.0
    total_null = _f(db.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE client_id=? AND invoice_id IS NULL",
        (client_id,),
    ).fetchone()["s"])
    credit = max(0.0, total_null - opening_debt)
    if credit < 0.01:
        return
    inv = db.execute("SELECT total FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    to_cover = min(credit, _f(inv["total"]))
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
        take = min(_f(pmt["amount"]), to_cover - covered)
        leftover = _f(pmt["amount"]) - take
        if leftover < 0.01:
            db.execute("UPDATE payments SET invoice_id=? WHERE id=?", (invoice_id, pmt["id"]))
        else:
            db.execute("UPDATE payments SET amount=? WHERE id=?", (leftover, pmt["id"]))
            db.execute(
                "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (client_id, invoice_id, take,
                 pmt["payment_date"], pmt["method"], pmt["reference"], pmt["notes"]),
            )
        covered += take
    if covered > 0.001:
        _refresh_invoice_paid(db, invoice_id)


def _ob_remaining(db, client_id):
    """How much of the opening-balance debt is still unpaid (used for smart payment allocation)."""
    client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return 0.0
    debt = abs(_f(client["opening_balance"]))
    if debt == 0:
        return 0.0
    paid = _f(db.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE client_id=? AND invoice_id IS NULL",
        (client_id,),
    ).fetchone()["s"])
    return max(0.0, debt - paid)


def _deduct_production_fifo(db, product_id, sub_product_id, qty_needed):
    """FIFO-deduct from open purchase-order items when creating a dispatch."""
    rows = db.execute(
        """SELECT poi.id, poi.quantity, poi.qty_dispatched
           FROM purchase_order_items poi
           JOIN purchase_orders po ON poi.po_id=po.id
           WHERE po.status='open'
             AND (poi.product_id IS ? OR (poi.product_id IS NULL AND ? IS NULL))
             AND (poi.sub_product_id IS ? OR (poi.sub_product_id IS NULL AND ? IS NULL))
             AND poi.quantity > poi.qty_dispatched
           ORDER BY po.created_at ASC, poi.id ASC""",
        (product_id, product_id, sub_product_id, sub_product_id),
    ).fetchall()
    remaining = qty_needed
    for row in rows:
        if remaining <= 0:
            break
        take = min(row["quantity"] - row["qty_dispatched"], remaining)
        db.execute(
            "UPDATE purchase_order_items SET qty_dispatched=qty_dispatched+? WHERE id=?",
            (take, row["id"]),
        )
        remaining -= take
        # Auto-close PO if fully dispatched
        po_row = db.execute(
            "SELECT po_id FROM purchase_order_items WHERE id=?", (row["id"],)
        ).fetchone()
        if po_row:
            undone = db.execute(
                "SELECT COUNT(*) AS c FROM purchase_order_items WHERE po_id=? AND qty_dispatched < quantity",
                (po_row["po_id"],),
            ).fetchone()["c"]
            if undone == 0:
                db.execute(
                    "UPDATE purchase_orders SET status='closed' WHERE id=?", (po_row["po_id"],)
                )
    return remaining  # leftover > 0 means insufficient coverage


def _update_qty(db, product_id, sub_product_id, field, delta):
    tbl = "sub_products" if sub_product_id else "products"
    pk  = sub_product_id if sub_product_id else product_id
    db.execute(f"UPDATE {tbl} SET {field}={field}+? WHERE id=?", (delta, pk))


mcp = FastMCP("Ledger", instructions=(
    "You are an AI assistant for Ledger, a small-business accounting app used in India. "
    "Currency is always Indian Rupees (₹ INR). Use Indian number formatting (lakhs/crores) for large amounts. "
    f"Today's date is {date.today().isoformat()}. "
    "Always use tools to fetch live data — never guess figures. "
    "For ALL write operations (create / update / delete) state exactly what you will do and wait for user confirmation before calling the tool. "
    "For DELETE operations always warn: 'This permanently deletes [entity] and cannot be undone. Confirm?' "
    "Never call a delete tool unless the user responds with an explicit yes/confirm in their message."
))


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENTS — READ
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_clients(query: str) -> str:
    """Search clients by name or company. Returns matching IDs and names."""
    q = f"%{query.lower()}%"
    with _db() as db:
        rows = db.execute(
            "SELECT id, name, company FROM clients "
            "WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(company,'')) LIKE ? "
            "ORDER BY name LIMIT 15",
            (q, q),
        ).fetchall()
    if not rows:
        return "No clients found matching that query."
    return "\n".join(
        f"ID {r['id']}: {r['name']}" + (f" ({r['company']})" if r["company"] else "")
        for r in rows
    )


@mcp.tool()
def get_client_details(client_id: int) -> str:
    """Get full contact info and current balance for a client."""
    with _db() as db:
        c = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not c:
            return f"Client ID {client_id} not found."
        invoices = db.execute(
            "SELECT total, amount_paid, status FROM invoices "
            "WHERE client_id=? AND status != 'cancelled'",
            (client_id,),
        ).fetchall()
        ob_paid = _f(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND invoice_id IS NULL",
            (client_id,),
        ).fetchone()[0])

    ob = _f(c["opening_balance"])
    inv_balance = sum(_f(i["total"]) - _f(i["amount_paid"]) for i in invoices)
    if ob >= 0:
        ob_remaining = max(0.0, ob - ob_paid)
        excess = max(0.0, ob_paid - ob)
    else:
        ob_remaining = 0.0
        excess = ob_paid + abs(ob)
    balance = -(ob_remaining + inv_balance) + excess
    paid_count    = sum(1 for i in invoices if i["status"] == "paid")
    pending_count = sum(1 for i in invoices if i["status"] in ("issued", "sent", "partial"))

    lines = [
        f"ID:       {c['id']}",
        f"Name:     {c['name']}" + (f" / {c['company']}" if c["company"] else ""),
        f"Email:    {c['email'] or '—'}",
        f"Phone:    {c['phone'] or '—'}",
        f"Address:  {', '.join(filter(None, [c['address'], c['city'], c['country']])) or '—'}",
        f"Tax ID:   {c['tax_id'] or '—'}",
        f"Notes:    {c['notes'] or '—'}",
        f"Balance:  {_inr(abs(balance))} {'(owes us)' if balance < 0 else ('(credit)' if balance > 0 else '(settled)')}",
        f"Invoices: {len(invoices)} total — {paid_count} paid, {pending_count} pending",
        f"Since:    {str(c['created_at'])[:10]}",
    ]
    if ob != 0:
        lines.append(f"Opening:  {_inr(abs(ob))} ({'debt' if ob > 0 else 'credit'})")
    return "\n".join(lines)


@mcp.tool()
def get_client_ledger(client_id: int) -> str:
    """Full chronological ledger for a client: opening balance, invoices, payments, running balance."""
    with _db() as db:
        c = db.execute("SELECT name, opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
        if not c:
            return f"Client ID {client_id} not found."
        invoices = db.execute(
            "SELECT invoice_number, issue_date, total, status FROM invoices "
            "WHERE client_id=? AND status != 'cancelled' ORDER BY issue_date, id",
            (client_id,),
        ).fetchall()
        payments = db.execute(
            "SELECT amount, payment_date, method, reference, invoice_id FROM payments "
            "WHERE client_id=? ORDER BY payment_date, id",
            (client_id,),
        ).fetchall()

    ob = _f(c["opening_balance"])
    entries = []
    if ob != 0:
        entries.append(("", "opening", f"Opening Balance ({'debt' if ob > 0 else 'credit'})",
                        ob if ob > 0 else 0, abs(ob) if ob < 0 else 0))

    items = sorted(
        [{"sort": r["issue_date"] or "", "kind": "invoice",  "row": dict(r)} for r in invoices] +
        [{"sort": r["payment_date"] or "", "kind": "payment", "row": dict(r)} for r in payments],
        key=lambda x: x["sort"],
    )
    for item in items:
        r = item["row"]
        if item["kind"] == "invoice":
            entries.append((r["issue_date"], "invoice", r["invoice_number"], _f(r["total"]), 0))
        else:
            label = r["method"] or "payment"
            if r["reference"]:
                label += f" / {r['reference']}"
            entries.append((r["payment_date"], "payment", label, 0, _f(r["amount"])))

    lines = [
        f"Ledger for {c['name']}", "─" * 64,
        f"{'Date':<12} {'Description':<30} {'Debit':>10} {'Credit':>10} {'Balance':>12}",
        "─" * 64,
    ]
    running = 0.0
    for dt, kind, label, debit, credit in entries:
        running += credit - debit
        lines.append(
            f"{dt or '—':<12} {label[:30]:<30} "
            f"{('₹'+f'{debit:,.0f}') if debit else '—':>10} "
            f"{('₹'+f'{credit:,.0f}') if credit else '—':>10} "
            f"{'₹'+f'{abs(running):,.0f}' + (' CR' if running > 0 else ' DR'):>12}"
        )
    lines.append("─" * 64)
    lines.append(f"Final balance: {_inr(abs(running))} "
                 f"{'CREDIT' if running > 0 else ('DEBIT – owes us' if running < 0 else 'SETTLED')}")
    return "\n".join(lines)


@mcp.tool()
def get_all_clients_summary() -> str:
    """All clients with outstanding balances sorted by most owed first."""
    with _db() as db:
        clients = db.execute("SELECT id, name, company, opening_balance FROM clients ORDER BY name").fetchall()
        result = []
        for c in clients:
            ob = _f(c["opening_balance"])
            rows = db.execute(
                "SELECT total, amount_paid FROM invoices WHERE client_id=? AND status != 'cancelled'",
                (c["id"],),
            ).fetchall()
            ob_paid = _f(db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND invoice_id IS NULL",
                (c["id"],),
            ).fetchone()[0])
            inv_bal = sum(_f(r["total"]) - _f(r["amount_paid"]) for r in rows)
            if ob >= 0:
                balance = -(max(0.0, ob - ob_paid) + inv_bal) + max(0.0, ob_paid - ob)
            else:
                balance = -inv_bal + (ob_paid + abs(ob))
            result.append((c["id"], c["name"], c["company"] or "", balance))

    result.sort(key=lambda x: x[3])
    lines = []
    for cid, name, company, balance in result:
        label = (f"owes {_inr(abs(balance))}" if balance < 0
                 else (f"credit {_inr(balance)}" if balance > 0 else "settled"))
        lines.append(f"ID {cid}: {name}" + (f" ({company})" if company else "") + f" — {label}")
    return "\n".join(lines) if lines else "No clients found."


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENTS — WRITE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_client(
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    country: str = "",
    tax_id: str = "",
    notes: str = "",
    opening_balance_amt: float = 0.0,
    opening_balance_type: str = "debt",
) -> str:
    """
    Create a new client. ONLY call after explicit user confirmation.
    opening_balance_type: 'debt' (client owes us) | 'credit' (client pre-paid)
    """
    if not name.strip():
        return "Error: Client name is required."
    ob = abs(opening_balance_amt) if opening_balance_type != "credit" else -abs(opening_balance_amt)
    with _db() as db:
        cur = db.execute(
            "INSERT INTO clients (name, company, email, phone, address, city, country, tax_id, notes, opening_balance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), company or None, email or None, phone or None,
             address or None, city or None, country or None,
             tax_id or None, notes or None, ob),
        )
        db.commit()
        client_id = cur.lastrowid
    msg = f"✓ Client '{name}' created (ID: {client_id})."
    if ob != 0:
        msg += f" Opening balance: {_inr(abs(ob))} ({'debt' if ob > 0 else 'credit'})."
    return msg


@mcp.tool()
def update_client(
    client_id: int,
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    country: str = "",
    tax_id: str = "",
    notes: str = "",
    opening_balance_amt: float = None,
    opening_balance_type: str = None,
) -> str:
    """
    Update an existing client. ONLY call after explicit user confirmation.
    Leave opening_balance_amt as None to keep the existing opening balance unchanged.
    opening_balance_type: 'debt' | 'credit'
    """
    if not name.strip():
        return "Error: Client name is required."
    with _db() as db:
        existing = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not existing:
            return f"Client ID {client_id} not found."
        if opening_balance_amt is not None:
            ob_type = opening_balance_type or ("credit" if _f(existing["opening_balance"]) < 0 else "debt")
            ob = abs(float(opening_balance_amt)) if ob_type != "credit" else -abs(float(opening_balance_amt))
        else:
            ob = _f(existing["opening_balance"])
        db.execute(
            """UPDATE clients SET name=?, company=?, email=?, phone=?, address=?,
               city=?, country=?, tax_id=?, notes=?, opening_balance=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (name.strip(), company or None, email or None, phone or None,
             address or None, city or None, country or None,
             tax_id or None, notes or None, ob, client_id),
        )
        db.commit()
    return f"✓ Client ID {client_id} ('{name}') updated."


@mcp.tool()
def delete_client(client_id: int) -> str:
    """
    PERMANENTLY delete a client and ALL their invoices and payments.
    Only call after the user explicitly confirmed deletion.
    """
    with _db() as db:
        c = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
        if not c:
            return f"Client ID {client_id} not found."
        name = c["name"]
        db.execute("DELETE FROM payments WHERE client_id=?", (client_id,))
        db.execute("DELETE FROM invoice_items WHERE invoice_id IN "
                   "(SELECT id FROM invoices WHERE client_id=?)", (client_id,))
        db.execute("DELETE FROM invoices WHERE client_id=?", (client_id,))
        db.execute("DELETE FROM clients WHERE id=?", (client_id,))
        db.commit()
    return f"✓ Client '{name}' (ID: {client_id}) permanently deleted along with all their invoices and payments."


# ═══════════════════════════════════════════════════════════════════════════════
# INVOICES — READ
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_invoice_details(invoice_number: str) -> str:
    """Get full details of a specific invoice by number (e.g. INV-0001)."""
    with _db() as db:
        inv = db.execute(
            "SELECT i.*, c.name AS client_name FROM invoices i "
            "JOIN clients c ON c.id=i.client_id WHERE i.invoice_number=?",
            (invoice_number.upper(),),
        ).fetchone()
        if not inv:
            return f"Invoice {invoice_number} not found."
        items = db.execute(
            "SELECT description, quantity, unit_price, tax_rate, line_total "
            "FROM invoice_items WHERE invoice_id=?",
            (inv["id"],),
        ).fetchall()
        pmts = db.execute(
            "SELECT amount, payment_date, method, reference FROM payments "
            "WHERE invoice_id=? ORDER BY payment_date",
            (inv["id"],),
        ).fetchall()

    remaining = _f(inv["total"]) - _f(inv["amount_paid"])
    lines = [
        f"Invoice:  {inv['invoice_number']}  (ID: {inv['id']})",
        f"Client:   {inv['client_name']}  (client_id: {inv['client_id']})",
        f"Date:     {inv['issue_date']}  |  Due: {inv['due_date'] or '—'}",
        f"Status:   {inv['status']}",
        f"Subtotal: {_inr(inv['subtotal'])}",
        f"Tax:      {_inr(inv['tax_total'])}",
        f"Discount: {_inr(inv['discount_amount'])}",
        f"Total:    {_inr(inv['total'])}",
        f"Paid:     {_inr(inv['amount_paid'])}",
        f"Balance:  {_inr(remaining)}",
        "",
        "Line items:",
    ]
    for it in items:
        lines.append(f"  {it['description']} × {_f(it['quantity']):.2g} @ {_inr(it['unit_price'])}"
                     + (f" ({_f(it['tax_rate']):.0f}% tax)" if _f(it["tax_rate"]) else "")
                     + f" = {_inr(it['line_total'])}")
    if pmts:
        lines.append("\nPayments received:")
        for p in pmts:
            lines.append(f"  {p['payment_date']} — {_inr(p['amount'])} via {p['method']}"
                         + (f" (ref: {p['reference']})" if p["reference"] else ""))
    if inv["notes"]:
        lines.append(f"\nNotes: {inv['notes']}")
    return "\n".join(lines)


@mcp.tool()
def get_client_invoices(client_id: int) -> str:
    """List all invoices for a client with amounts and status."""
    today = date.today().isoformat()
    with _db() as db:
        rows = db.execute(
            "SELECT id, invoice_number, issue_date, due_date, total, amount_paid, status "
            "FROM invoices WHERE client_id=? ORDER BY issue_date DESC",
            (client_id,),
        ).fetchall()
    if not rows:
        return "No invoices found for this client."
    lines = []
    for r in rows:
        remaining = _f(r["total"]) - _f(r["amount_paid"])
        overdue = r["due_date"] and r["due_date"] < today and r["status"] not in ("paid", "cancelled")
        status_str = r["status"]
        if overdue:
            days = (date.today() - date.fromisoformat(r["due_date"])).days
            status_str += f" ⚠ {days}d overdue"
        lines.append(
            f"{r['invoice_number']} (ID:{r['id']}) | {r['issue_date']} | "
            f"total {_inr(r['total'])} | paid {_inr(r['amount_paid'])} | "
            f"balance {_inr(remaining)} | {status_str}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_overdue_invoices() -> str:
    """List all invoices past their due date and not fully paid."""
    today = date.today().isoformat()
    with _db() as db:
        rows = db.execute(
            """SELECT i.id, i.invoice_number, i.issue_date, i.due_date,
                      i.total, i.amount_paid, c.name AS client_name
               FROM invoices i JOIN clients c ON c.id=i.client_id
               WHERE i.due_date < ? AND i.status NOT IN ('paid','cancelled')
               ORDER BY i.due_date""",
            (today,),
        ).fetchall()
    if not rows:
        return "No overdue invoices — all up to date!"
    lines = []
    for r in rows:
        days = (date.today() - date.fromisoformat(r["due_date"])).days
        remaining = _f(r["total"]) - _f(r["amount_paid"])
        lines.append(
            f"{r['invoice_number']} | {r['client_name']} | "
            f"due {r['due_date']} ({days}d ago) | {_inr(remaining)} remaining"
        )
    return f"{len(lines)} overdue invoice(s):\n" + "\n".join(lines)


@mcp.tool()
def get_recent_invoices(limit: int = 10) -> str:
    """Get the most recent invoices across all clients."""
    with _db() as db:
        rows = db.execute(
            """SELECT i.invoice_number, i.issue_date, i.total, i.amount_paid, i.status, c.name
               FROM invoices i JOIN clients c ON c.id=i.client_id
               ORDER BY i.issue_date DESC, i.id DESC LIMIT ?""",
            (min(limit, 50),),
        ).fetchall()
    if not rows:
        return "No invoices found."
    return "\n".join(
        f"{r['invoice_number']} | {r['name']} | {r['issue_date']} | {_inr(r['total'])} | {r['status']}"
        for r in rows
    )


# ═══════════════════════════════════════════════════════════════════════════════
# INVOICES — WRITE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_invoice(
    client_id: int,
    items: list,
    issue_date: str = "",
    due_date: str = "",
    notes: str = "",
    discount_amount: float = 0.0,
    status: str = "issued",
) -> str:
    """
    Create a new invoice. ONLY call after explicit user confirmation.

    items: list of dicts, each with keys:
      description (required), quantity, unit_price, tax_rate (%), sku (optional)
      product_id (optional int), sub_product_id (optional int)

    Example item: {"description": "Widget A", "quantity": 2, "unit_price": 500, "tax_rate": 18}

    Deducts from warehouse stock automatically for catalog products.
    issue_date / due_date: YYYY-MM-DD format. Defaults to today.
    status: issued | sent | paid
    """
    if not items:
        return "Error: At least one line item is required."
    issue_date = issue_date or date.today().isoformat()
    try:
        datetime.strptime(issue_date, "%Y-%m-%d")
    except ValueError:
        return "Error: issue_date must be YYYY-MM-DD."
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            return "Error: due_date must be YYYY-MM-DD."

    with _db() as db:
        client = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            return f"Error: Client ID {client_id} not found."

        subtotal = sum(_f(it.get("unit_price")) * _f(it.get("quantity", 1)) for it in items)
        tax_total = sum(
            _f(it.get("unit_price")) * _f(it.get("quantity", 1)) * _f(it.get("tax_rate", 0)) / 100
            for it in items
        )
        discount = abs(float(discount_amount or 0))
        total = subtotal + tax_total - discount

        invoice_number = _next_invoice_number(db)
        cur = db.execute(
            """INSERT INTO invoices (invoice_number, client_id, status, issue_date, due_date,
               notes, subtotal, tax_total, discount_amount, total)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (invoice_number, client_id, status, issue_date,
             due_date or None, notes or None,
             subtotal, tax_total, discount, total),
        )
        invoice_id = cur.lastrowid

        for it in items:
            pid  = int(it["product_id"])     if it.get("product_id")     else None
            spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
            qty  = _f(it.get("quantity", 1))
            price = _f(it.get("unit_price"))
            line_total = price * qty
            db.execute(
                """INSERT INTO invoice_items
                   (invoice_id, product_id, sub_product_id, sku, description, quantity, unit_price, tax_rate, line_total)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (invoice_id, pid, spid,
                 it.get("sku") or None,
                 it["description"], qty, price,
                 _f(it.get("tax_rate", 0)), line_total),
            )
            if pid or spid:
                tbl = "sub_products" if spid else "products"
                pk  = spid if spid else pid
                db.execute(f"UPDATE {tbl} SET stock_qty=stock_qty-? WHERE id=?", (qty, pk))
                db.execute(
                    "INSERT INTO stock_movements "
                    "(product_id, sub_product_id, movement_type, quantity, notes, invoice_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (pid, spid, "sale", qty, f"Invoice {invoice_number}", invoice_id),
                )
        db.commit()

        _apply_client_credit(db, client_id, invoice_id)

    return (f"✓ Invoice {invoice_number} (ID: {invoice_id}) created for {client['name']}.\n"
            f"  Subtotal: {_inr(subtotal)} | Tax: {_inr(tax_total)} | Discount: {_inr(discount)} | Total: {_inr(total)}")


@mcp.tool()
def update_invoice_status(invoice_id: int, status: str) -> str:
    """
    Change the status of an invoice. ONLY call after explicit user confirmation.
    status: issued | sent | partial | paid | cancelled
    Setting to 'cancelled' restores warehouse stock for catalog items.
    """
    valid = {"issued", "sent", "partial", "paid", "cancelled"}
    if status not in valid:
        return f"Invalid status. Must be one of: {', '.join(sorted(valid))}"
    with _db() as db:
        inv = db.execute("SELECT invoice_number, status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not inv:
            return f"Invoice ID {invoice_id} not found."
        prev = inv["status"]
        db.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
        if status == "cancelled" and prev != "cancelled":
            rows = db.execute(
                "SELECT product_id, sub_product_id, quantity FROM invoice_items WHERE invoice_id=?",
                (invoice_id,),
            ).fetchall()
            for r in rows:
                if r["product_id"] or r["sub_product_id"]:
                    tbl = "sub_products" if r["sub_product_id"] else "products"
                    pk  = r["sub_product_id"] if r["sub_product_id"] else r["product_id"]
                    db.execute(f"UPDATE {tbl} SET stock_qty=stock_qty+? WHERE id=?", (r["quantity"], pk))
                    db.execute(
                        "INSERT INTO stock_movements "
                        "(product_id, sub_product_id, movement_type, quantity, notes, invoice_id) "
                        "VALUES (?,?,?,?,?,?)",
                        (r["product_id"], r["sub_product_id"], "sale_cancelled", r["quantity"],
                         f"Cancelled: {inv['invoice_number']}", invoice_id),
                    )
        db.commit()
    return f"✓ Invoice {inv['invoice_number']} status changed: {prev} → {status}."


@mcp.tool()
def delete_invoice(invoice_id: int) -> str:
    """
    PERMANENTLY delete an invoice and its line items.
    Only call after the user explicitly confirmed deletion.
    Does NOT reverse stock or payment allocations automatically.
    """
    with _db() as db:
        inv = db.execute(
            "SELECT i.invoice_number, c.name AS client_name "
            "FROM invoices i JOIN clients c ON c.id=i.client_id WHERE i.id=?",
            (invoice_id,),
        ).fetchone()
        if not inv:
            return f"Invoice ID {invoice_id} not found."
        db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
        db.execute("UPDATE payments SET invoice_id=NULL WHERE invoice_id=?", (invoice_id,))
        db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        db.commit()
    return f"✓ Invoice {inv['invoice_number']} (for {inv['client_name']}) permanently deleted."


# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENTS — READ
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_recent_payments(limit: int = 10) -> str:
    """Get the most recent payments received across all clients."""
    with _db() as db:
        rows = db.execute(
            """SELECT p.id, p.amount, p.payment_date, p.method, p.reference,
                      c.name AS client_name, i.invoice_number
               FROM payments p
               JOIN clients c ON c.id=p.client_id
               LEFT JOIN invoices i ON i.id=p.invoice_id
               ORDER BY p.payment_date DESC, p.id DESC LIMIT ?""",
            (min(limit, 50),),
        ).fetchall()
    if not rows:
        return "No payments found."
    return "\n".join(
        f"ID {r['id']} | {r['payment_date']} | {r['client_name']} | {_inr(r['amount'])} | "
        f"{r['method']}" + (f" | {r['invoice_number']}" if r["invoice_number"] else " | unallocated") +
        (f" | ref: {r['reference']}" if r["reference"] else "")
        for r in rows
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENTS — WRITE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def record_payment(
    client_id: int,
    amount: float,
    payment_date: str,
    method: str,
    invoice_id: int = None,
    reference: str = "",
    notes: str = "",
) -> str:
    """
    Record a payment from a client. ONLY call after explicit user confirmation.

    method: cash | bank_transfer | cheque | upi | other
    payment_date: YYYY-MM-DD
    invoice_id: optional — links payment to a specific invoice.
      If omitted, automatically allocates: opening balance → oldest invoices → unallocated surplus.
    """
    valid_methods = {"cash", "bank_transfer", "cheque", "upi", "other"}
    if method not in valid_methods:
        return f"Invalid method. Must be one of: {', '.join(sorted(valid_methods))}"
    try:
        datetime.strptime(payment_date, "%Y-%m-%d")
    except ValueError:
        return "Invalid date — use YYYY-MM-DD."
    if amount <= 0:
        return "Amount must be positive."

    with _db() as db:
        client = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            return f"Client ID {client_id} not found."

        if invoice_id:
            inv = db.execute("SELECT total, amount_paid FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if not inv:
                return f"Invoice ID {invoice_id} not found."
            db.execute(
                "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (client_id, invoice_id, amount, payment_date, method,
                 reference or None, notes or "Recorded via Claude"),
            )
            _refresh_invoice_paid(db, invoice_id)
            db.commit()
            return (f"✓ {_inr(amount)} recorded for {client['name']} against invoice ID {invoice_id}.\n"
                    f"  Date: {payment_date} | Method: {method}")

        # Smart allocation: OB → oldest invoices → surplus
        remaining = amount
        allocations = []

        ob_rem = _ob_remaining(db, client_id)
        if ob_rem > 0 and remaining > 0:
            apply = min(ob_rem, remaining)
            db.execute(
                "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
                "VALUES (?,NULL,?,?,?,?,?)",
                (client_id, apply, payment_date, method, reference or None, notes or "Recorded via Claude"),
            )
            allocations.append(f"  {_inr(apply)} → opening balance")
            remaining -= apply

        if remaining > 0:
            old_invs = db.execute(
                """SELECT id, total, amount_paid, (total - amount_paid) AS remaining, invoice_number
                   FROM invoices WHERE client_id=? AND status NOT IN ('paid','cancelled')
                     AND (total - amount_paid) > 0
                   ORDER BY issue_date ASC, id ASC""",
                (client_id,),
            ).fetchall()
            for inv in old_invs:
                if remaining <= 0:
                    break
                apply = min(_f(inv["remaining"]), remaining)
                db.execute(
                    "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (client_id, inv["id"], apply, payment_date, method,
                     reference or None, notes or "Recorded via Claude"),
                )
                _refresh_invoice_paid(db, inv["id"])
                allocations.append(f"  {_inr(apply)} → {inv['invoice_number']}")
                remaining -= apply

        if remaining > 0.001:
            db.execute(
                "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
                "VALUES (?,NULL,?,?,?,?,?)",
                (client_id, remaining, payment_date, method, reference or None, notes or "Recorded via Claude"),
            )
            allocations.append(f"  {_inr(remaining)} → unallocated surplus (credit)")

        db.commit()

    result = f"✓ {_inr(amount)} payment recorded for {client['name']} on {payment_date}.\nAllocation:\n"
    result += "\n".join(allocations)
    return result


@mcp.tool()
def delete_payment(payment_id: int) -> str:
    """
    PERMANENTLY delete a payment record.
    Only call after the user explicitly confirmed deletion.
    Invoice paid status is automatically recalculated.
    """
    with _db() as db:
        p = db.execute(
            "SELECT p.amount, p.payment_date, p.invoice_id, c.name AS client_name "
            "FROM payments p JOIN clients c ON c.id=p.client_id WHERE p.id=?",
            (payment_id,),
        ).fetchone()
        if not p:
            return f"Payment ID {payment_id} not found."
        invoice_id = p["invoice_id"]
        db.execute("DELETE FROM payments WHERE id=?", (payment_id,))
        db.commit()
        if invoice_id:
            _refresh_invoice_paid(db, invoice_id)
            db.commit()
    return (f"✓ Payment ID {payment_id} ({_inr(p['amount'])} from {p['client_name']} "
            f"on {p['payment_date']}) permanently deleted.")


# ═══════════════════════════════════════════════════════════════════════════════
# MANUAL LEDGER ENTRIES
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_ledger_entry(
    client_id: int,
    entry_date: str,
    description: str,
    debit: float = 0.0,
    credit: float = 0.0,
) -> str:
    """
    Add a manual debit or credit entry to a client's ledger.
    ONLY call after explicit user confirmation.

    debit: amount client owes us (increases their balance due)
    credit: amount we owe client (reduces their balance due)
    entry_date: YYYY-MM-DD
    """
    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        return "Invalid date — use YYYY-MM-DD."
    if debit == 0 and credit == 0:
        return "Error: Enter a debit or credit amount."
    with _db() as db:
        c = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
        if not c:
            return f"Client ID {client_id} not found."
        db.execute(
            "INSERT INTO ledger_entries (client_id, entry_date, description, debit, credit) "
            "VALUES (?,?,?,?,?)",
            (client_id, entry_date, description, debit, credit),
        )
        db.commit()
    direction = f"debit {_inr(debit)}" if debit else f"credit {_inr(credit)}"
    return f"✓ Manual ledger entry added for {c['name']}: {direction} on {entry_date} — '{description}'"


# ═══════════════════════════════════════════════════════════════════════════════
# BUSINESS STATS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_business_stats() -> str:
    """Overall business snapshot: revenue, outstanding, overdue, recent activity."""
    today = date.today().isoformat()
    with _db() as db:
        total_revenue = _f(db.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0])
        outstanding   = _f(db.execute(
            "SELECT COALESCE(SUM(total-amount_paid),0) FROM invoices WHERE status NOT IN ('paid','cancelled')"
        ).fetchone()[0])
        client_count  = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        overdue_count = db.execute(
            "SELECT COUNT(*) FROM invoices WHERE due_date<? AND status NOT IN ('paid','cancelled')", (today,)
        ).fetchone()[0]
        overdue_amt   = _f(db.execute(
            "SELECT COALESCE(SUM(total-amount_paid),0) FROM invoices WHERE due_date<? AND status NOT IN ('paid','cancelled')", (today,)
        ).fetchone()[0])
        recent_pmts   = _f(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE payment_date>=date('now','-30 days')"
        ).fetchone()[0])
        recent_inv    = _f(db.execute(
            "SELECT COALESCE(SUM(total),0) FROM invoices WHERE issue_date>=date('now','-30 days') AND status!='cancelled'"
        ).fetchone()[0])
        inv_count     = db.execute("SELECT COUNT(*) FROM invoices WHERE status!='cancelled'").fetchone()[0]
        product_count = db.execute("SELECT COUNT(*) FROM products WHERE is_active=1").fetchone()[0]
        supplier_count = db.execute("SELECT COUNT(*) FROM suppliers WHERE is_active=1").fetchone()[0]

    return "\n".join([
        f"Total revenue collected : {_inr(total_revenue)}",
        f"Outstanding (unpaid)    : {_inr(outstanding)}",
        f"Overdue                 : {overdue_count} invoice(s) totalling {_inr(overdue_amt)}",
        f"Clients                 : {client_count}",
        f"Invoices (active)       : {inv_count}",
        f"Active products         : {product_count}",
        f"Active suppliers        : {supplier_count}",
        f"─── Last 30 days ───────────────────",
        f"Invoiced                : {_inr(recent_inv)}",
        f"Payments received       : {_inr(recent_pmts)}",
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTS & CATEGORIES — READ
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_categories() -> str:
    """List all product categories."""
    with _db() as db:
        rows = db.execute("SELECT id, name, description FROM categories ORDER BY name").fetchall()
    if not rows:
        return "No categories found."
    return "\n".join(
        f"ID {r['id']}: {r['name']}" + (f" — {r['description']}" if r["description"] else "")
        for r in rows
    )


@mcp.tool()
def search_products(query: str) -> str:
    """Search products and sub-products by name or SKU."""
    q = f"%{query.lower()}%"
    with _db() as db:
        products = db.execute(
            "SELECT id, name, sku, unit_price, stock_qty FROM products "
            "WHERE is_active=1 AND (LOWER(name) LIKE ? OR LOWER(COALESCE(sku,'')) LIKE ?) "
            "ORDER BY name LIMIT 10",
            (q, q),
        ).fetchall()
        subs = db.execute(
            """SELECT s.id, s.name, s.sku, s.unit_price, s.stock_qty, p.name AS parent, p.id AS parent_id
               FROM sub_products s JOIN products p ON p.id=s.product_id
               WHERE s.is_active=1 AND (LOWER(s.name) LIKE ? OR LOWER(COALESCE(s.sku,'')) LIKE ?)
               ORDER BY p.name, s.name LIMIT 10""",
            (q, q),
        ).fetchall()
    lines = []
    for p in products:
        lines.append(f"Product ID {p['id']}: {p['name']}" +
                     (f" [{p['sku']}]" if p["sku"] else "") +
                     f" | {_inr(p['unit_price'])} | stock: {_f(p['stock_qty']):.0f}")
    for s in subs:
        lines.append(f"Sub-product ID {s['id']}: {s['parent']} (ID:{s['parent_id']}) — {s['name']}" +
                     (f" [{s['sku']}]" if s["sku"] else "") +
                     f" | {_inr(s['unit_price'])} | stock: {_f(s['stock_qty']):.0f}")
    return "\n".join(lines) if lines else "No products found matching that query."


@mcp.tool()
def get_stock_summary() -> str:
    """Current stock levels for all products and sub-products (warehouse / production / transit)."""
    with _db() as db:
        products = db.execute(
            "SELECT id, name, sku, stock_qty, production_qty, in_transit_qty, min_quantity "
            "FROM products WHERE is_active=1 ORDER BY name"
        ).fetchall()
        subs = db.execute(
            """SELECT p.id AS parent_id, p.name AS parent, s.id, s.name, s.sku,
                      s.stock_qty, s.production_qty, s.in_transit_qty, s.min_quantity
               FROM sub_products s JOIN products p ON p.id=s.product_id
               WHERE s.is_active=1 ORDER BY p.name, s.name"""
        ).fetchall()

    def _row(name, sku, stock, prod, transit, min_qty):
        alert = " ⚠ LOW" if min_qty and _f(stock) < _f(min_qty) else ""
        return (f"{name}" + (f" [{sku}]" if sku else "") +
                f" | wh:{_f(stock):.0f} prod:{_f(prod):.0f} transit:{_f(transit):.0f}{alert}")

    lines = ["=== Products ==="]
    lines += [_row(p["name"], p["sku"], p["stock_qty"], p["production_qty"], p["in_transit_qty"], p["min_quantity"]) for p in products]
    if subs:
        lines.append("\n=== Sub-products ===")
        lines += [_row(f"{s['parent']} — {s['name']}", s["sku"], s["stock_qty"], s["production_qty"], s["in_transit_qty"], s["min_quantity"]) for s in subs]
    return "\n".join(lines)


@mcp.tool()
def get_low_stock_alerts() -> str:
    """List all products and sub-products currently below their minimum stock level."""
    with _db() as db:
        low_p = db.execute(
            "SELECT name, sku, stock_qty, min_quantity FROM products "
            "WHERE is_active=1 AND min_quantity>0 AND stock_qty<min_quantity ORDER BY (stock_qty-min_quantity)"
        ).fetchall()
        low_s = db.execute(
            """SELECT p.name AS parent, s.name, s.sku, s.stock_qty,
                      CASE WHEN s.min_quantity>0 THEN s.min_quantity ELSE p.min_quantity END AS eff_min
               FROM sub_products s JOIN products p ON p.id=s.product_id
               WHERE s.is_active=1
                 AND (CASE WHEN s.min_quantity>0 THEN s.min_quantity ELSE p.min_quantity END)>0
                 AND s.stock_qty < (CASE WHEN s.min_quantity>0 THEN s.min_quantity ELSE p.min_quantity END)
               ORDER BY s.stock_qty"""
        ).fetchall()
    if not low_p and not low_s:
        return "No low stock alerts — all products above minimum levels."
    lines = []
    for p in low_p:
        shortage = _f(p["min_quantity"]) - _f(p["stock_qty"])
        lines.append(f"{p['name']}" + (f" [{p['sku']}]" if p["sku"] else "") +
                     f" | stock: {_f(p['stock_qty']):.0f} / min: {_f(p['min_quantity']):.0f} | short by {shortage:.0f}")
    for s in low_s:
        shortage = _f(s["eff_min"]) - _f(s["stock_qty"])
        lines.append(f"{s['parent']} — {s['name']}" + (f" [{s['sku']}]" if s["sku"] else "") +
                     f" | stock: {_f(s['stock_qty']):.0f} / min: {_f(s['eff_min']):.0f} | short by {shortage:.0f}")
    return f"{len(lines)} low-stock alert(s):\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTS & CATEGORIES — WRITE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_category(name: str, description: str = "") -> str:
    """Create a product category. ONLY call after explicit user confirmation."""
    if not name.strip():
        return "Error: Category name is required."
    with _db() as db:
        cur = db.execute(
            "INSERT INTO categories (name, description) VALUES (?,?)",
            (name.strip(), description or None),
        )
        db.commit()
    return f"✓ Category '{name}' created (ID: {cur.lastrowid})."


@mcp.tool()
def update_category(cat_id: int, name: str, description: str = "") -> str:
    """Update a product category. ONLY call after explicit user confirmation."""
    if not name.strip():
        return "Error: Category name is required."
    with _db() as db:
        cat = db.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not cat:
            return f"Category ID {cat_id} not found."
        db.execute("UPDATE categories SET name=?, description=? WHERE id=?",
                   (name.strip(), description or None, cat_id))
        db.commit()
    return f"✓ Category ID {cat_id} updated to '{name}'."


@mcp.tool()
def delete_category(cat_id: int) -> str:
    """
    PERMANENTLY delete a category.
    Only call after the user explicitly confirmed deletion.
    """
    with _db() as db:
        cat = db.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not cat:
            return f"Category ID {cat_id} not found."
        db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        db.commit()
    return f"✓ Category '{cat['name']}' (ID: {cat_id}) permanently deleted."


@mcp.tool()
def create_product(
    name: str,
    unit_price: float = 0.0,
    tax_rate: float = 18.0,
    sku: str = "",
    description: str = "",
    min_quantity: float = 0.0,
    opening_stock: float = 0.0,
    category_id: int = None,
) -> str:
    """
    Create a new product. ONLY call after explicit user confirmation.
    Use list_categories() to get valid category_id values.
    """
    if not name.strip():
        return "Error: Product name is required."
    with _db() as db:
        if category_id:
            cat = db.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
            if not cat:
                return f"Category ID {category_id} not found. Use list_categories() to see valid IDs."
        cur = db.execute(
            """INSERT INTO products (category_id, name, sku, description, unit_price, tax_rate,
               track_inventory, stock_qty, min_quantity, is_active)
               VALUES (?,?,?,?,?,?,1,?,?,1)""",
            (category_id or None, name.strip(), sku or None, description or None,
             unit_price, tax_rate, opening_stock, min_quantity),
        )
        product_id = cur.lastrowid
        if opening_stock > 0:
            db.execute(
                "INSERT INTO stock_movements (product_id, movement_type, quantity, notes) VALUES (?,?,?,?)",
                (product_id, "opening", opening_stock, "Opening stock"),
            )
        db.commit()
    return f"✓ Product '{name}' created (ID: {product_id}) | Price: {_inr(unit_price)} | Tax: {tax_rate}% | Stock: {opening_stock:.0f}."


@mcp.tool()
def update_product(
    product_id: int,
    name: str,
    unit_price: float = None,
    tax_rate: float = None,
    sku: str = "",
    description: str = "",
    min_quantity: float = None,
    category_id: int = None,
    is_active: bool = True,
) -> str:
    """
    Update an existing product. ONLY call after explicit user confirmation.
    Pass None for numeric fields to keep existing values.
    """
    if not name.strip():
        return "Error: Product name is required."
    with _db() as db:
        p = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return f"Product ID {product_id} not found."
        new_price    = unit_price   if unit_price   is not None else _f(p["unit_price"])
        new_tax      = tax_rate     if tax_rate     is not None else _f(p["tax_rate"])
        new_min      = min_quantity if min_quantity  is not None else _f(p["min_quantity"])
        new_cat      = category_id  if category_id  is not None else p["category_id"]
        db.execute(
            """UPDATE products SET category_id=?, name=?, sku=?, description=?,
               unit_price=?, tax_rate=?, track_inventory=1, min_quantity=?, is_active=? WHERE id=?""",
            (new_cat, name.strip(), sku or None, description or None,
             new_price, new_tax, new_min, 1 if is_active else 0, product_id),
        )
        db.execute("UPDATE sub_products SET tax_rate=? WHERE product_id=?", (new_tax, product_id))
        db.commit()
    return f"✓ Product ID {product_id} ('{name}') updated."


@mcp.tool()
def delete_product(product_id: int) -> str:
    """
    PERMANENTLY delete a product and all its sub-products and stock movements.
    Only call after the user explicitly confirmed deletion.
    """
    with _db() as db:
        p = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return f"Product ID {product_id} not found."
        db.execute(
            "DELETE FROM stock_movements WHERE sub_product_id IN "
            "(SELECT id FROM sub_products WHERE product_id=?)", (product_id,)
        )
        db.execute("DELETE FROM stock_movements WHERE product_id=?", (product_id,))
        db.execute("DELETE FROM sub_products WHERE product_id=?", (product_id,))
        db.execute("DELETE FROM products WHERE id=?", (product_id,))
        db.commit()
    return f"✓ Product '{p['name']}' (ID: {product_id}) and all sub-products permanently deleted."


@mcp.tool()
def create_sub_product(
    product_id: int,
    name: str,
    sku: str = "",
    description: str = "",
    unit_price: float = 0.0,
    use_parent_price: bool = True,
    min_quantity: float = 0.0,
    opening_stock: float = 0.0,
) -> str:
    """
    Create a sub-product (variant) under an existing product.
    ONLY call after explicit user confirmation.
    use_parent_price=True: sub-product inherits the parent's unit_price.
    """
    if not name.strip():
        return "Error: Sub-product name is required."
    with _db() as db:
        parent = db.execute("SELECT name, tax_rate, unit_price FROM products WHERE id=?", (product_id,)).fetchone()
        if not parent:
            return f"Product ID {product_id} not found."
        parent_tax = _f(parent["tax_rate"])
        use_pp = 1 if use_parent_price else 0
        cur = db.execute(
            """INSERT INTO sub_products
               (product_id, name, sku, description, use_parent_price, unit_price,
                tax_rate, track_inventory, stock_qty, min_quantity, is_active)
               VALUES (?,?,?,?,?,?,?,1,?,?,1)""",
            (product_id, name.strip(), sku or None, description or None,
             use_pp, unit_price, parent_tax, opening_stock, min_quantity),
        )
        sub_id = cur.lastrowid
        if opening_stock > 0:
            db.execute(
                "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes) VALUES (?,?,?,?,?)",
                (product_id, sub_id, "opening", opening_stock, "Opening stock"),
            )
        db.commit()
    effective_price = _f(parent["unit_price"]) if use_parent_price else unit_price
    return (f"✓ Sub-product '{name}' created under '{parent['name']}' (sub-ID: {sub_id}). "
            f"Price: {_inr(effective_price)} | Stock: {opening_stock:.0f}.")


@mcp.tool()
def update_sub_product(
    sub_id: int,
    name: str,
    sku: str = "",
    description: str = "",
    unit_price: float = None,
    use_parent_price: bool = None,
    min_quantity: float = None,
    is_active: bool = True,
) -> str:
    """
    Update an existing sub-product. ONLY call after explicit user confirmation.
    Pass None for numeric fields to keep existing values.
    """
    if not name.strip():
        return "Error: Sub-product name is required."
    with _db() as db:
        s = db.execute(
            "SELECT s.*, p.name AS parent_name, p.tax_rate AS parent_tax "
            "FROM sub_products s JOIN products p ON p.id=s.product_id WHERE s.id=?",
            (sub_id,),
        ).fetchone()
        if not s:
            return f"Sub-product ID {sub_id} not found."
        use_pp    = (1 if use_parent_price else 0) if use_parent_price is not None else s["use_parent_price"]
        new_price = unit_price   if unit_price   is not None else _f(s["unit_price"])
        new_min   = min_quantity if min_quantity  is not None else _f(s["min_quantity"])
        db.execute(
            """UPDATE sub_products SET name=?, sku=?, description=?, use_parent_price=?,
               unit_price=?, tax_rate=?, min_quantity=?, is_active=? WHERE id=?""",
            (name.strip(), sku or None, description or None, use_pp,
             new_price, _f(s["parent_tax"]), new_min,
             1 if is_active else 0, sub_id),
        )
        db.commit()
    return f"✓ Sub-product '{name}' (ID: {sub_id}) under '{s['parent_name']}' updated."


@mcp.tool()
def delete_sub_product(sub_id: int) -> str:
    """
    PERMANENTLY delete a sub-product and its stock movements.
    Only call after the user explicitly confirmed deletion.
    """
    with _db() as db:
        s = db.execute(
            "SELECT s.name, p.name AS parent FROM sub_products s JOIN products p ON p.id=s.product_id WHERE s.id=?",
            (sub_id,),
        ).fetchone()
        if not s:
            return f"Sub-product ID {sub_id} not found."
        db.execute("DELETE FROM stock_movements WHERE sub_product_id=?", (sub_id,))
        db.execute("DELETE FROM sub_products WHERE id=?", (sub_id,))
        db.commit()
    return f"✓ Sub-product '{s['parent']} — {s['name']}' (ID: {sub_id}) permanently deleted."


@mcp.tool()
def adjust_stock(
    product_id: int,
    bucket: str,
    direction: str,
    quantity: float,
    notes: str = "",
    sub_product_id: int = None,
) -> str:
    """
    Directly adjust one stock bucket. ONLY call after explicit user confirmation.
    bucket: warehouse | production | dispatch
    direction: increase | decrease
    """
    if bucket not in ("warehouse", "production", "dispatch"):
        return "bucket must be: warehouse | production | dispatch"
    if direction not in ("increase", "decrease"):
        return "direction must be: increase | decrease"
    if quantity <= 0:
        return "quantity must be positive."

    field_map = {"warehouse": "stock_qty", "production": "production_qty", "dispatch": "in_transit_qty"}
    field = field_map[bucket]
    delta = quantity if direction == "increase" else -quantity
    movement = f"{bucket}_{'add' if direction == 'increase' else 'deduct'}"

    with _db() as db:
        if sub_product_id:
            row = db.execute("SELECT name FROM sub_products WHERE id=?", (sub_product_id,)).fetchone()
            if not row:
                return f"Sub-product ID {sub_product_id} not found."
            name = row["name"]
            db.execute(f"UPDATE sub_products SET {field}={field}+? WHERE id=?", (delta, sub_product_id))
        else:
            row = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
            if not row:
                return f"Product ID {product_id} not found."
            name = row["name"]
            db.execute(f"UPDATE products SET {field}={field}+? WHERE id=?", (delta, product_id))
        db.execute(
            "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes) VALUES (?,?,?,?,?)",
            (product_id, sub_product_id or None, movement, quantity, notes or "Adjusted via Claude"),
        )
        db.commit()
    return f"✓ {name} {bucket} stock {direction}d by {quantity:.0f}. Notes: {notes or '—'}"


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS — READ
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_suppliers() -> str:
    """List all active suppliers."""
    with _db() as db:
        rows = db.execute(
            "SELECT id, name, company, email, phone, address FROM suppliers WHERE is_active=1 ORDER BY name"
        ).fetchall()
    if not rows:
        return "No suppliers found."
    return "\n".join(
        f"ID {r['id']}: {r['name']}" +
        (f" ({r['company']})" if r["company"] else "") +
        (f" | {r['email']}" if r["email"] else "") +
        (f" | {r['phone']}" if r["phone"] else "") +
        (f" | {r['address']}" if r["address"] else "")
        for r in rows
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS — WRITE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_supplier(
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    notes: str = "",
) -> str:
    """Create a new supplier. ONLY call after explicit user confirmation."""
    if not name.strip():
        return "Error: Supplier name is required."
    with _db() as db:
        cur = db.execute(
            "INSERT INTO suppliers (name, company, email, phone, address, notes) VALUES (?,?,?,?,?,?)",
            (name.strip(), company or None, email or None, phone or None, address or None, notes or None),
        )
        db.commit()
    return f"✓ Supplier '{name}' created (ID: {cur.lastrowid})."


@mcp.tool()
def update_supplier(
    supplier_id: int,
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    notes: str = "",
) -> str:
    """Update an existing supplier. ONLY call after explicit user confirmation."""
    if not name.strip():
        return "Error: Supplier name is required."
    with _db() as db:
        s = db.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not s:
            return f"Supplier ID {supplier_id} not found."
        db.execute(
            "UPDATE suppliers SET name=?, company=?, email=?, phone=?, address=?, notes=? WHERE id=?",
            (name.strip(), company or None, email or None, phone or None,
             address or None, notes or None, supplier_id),
        )
        db.commit()
    return f"✓ Supplier ID {supplier_id} ('{name}') updated."


@mcp.tool()
def delete_supplier(supplier_id: int) -> str:
    """
    PERMANENTLY delete a supplier.
    Only call after the user explicitly confirmed deletion.
    """
    with _db() as db:
        s = db.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not s:
            return f"Supplier ID {supplier_id} not found."
        db.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
        db.commit()
    return f"✓ Supplier '{s['name']}' (ID: {supplier_id}) permanently deleted."


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTION ORDERS — READ
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_purchase_orders(status: str = "open") -> str:
    """List production/purchase orders. status: open | closed | all"""
    with _db() as db:
        if status == "all":
            rows = db.execute(
                """SELECT po.id, po.name, po.status, po.expected_completion, s.name AS supplier
                   FROM purchase_orders po LEFT JOIN suppliers s ON s.id=po.supplier_id
                   ORDER BY po.created_at DESC LIMIT 30"""
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT po.id, po.name, po.status, po.expected_completion, s.name AS supplier
                   FROM purchase_orders po LEFT JOIN suppliers s ON s.id=po.supplier_id
                   WHERE po.status=? ORDER BY po.expected_completion LIMIT 30""",
                (status,),
            ).fetchall()
    if not rows:
        return f"No {status} production orders found."
    lines = []
    for r in rows:
        lines.append(
            f"PO-{r['id']:04d} | {r['name']} | {r['status']} | "
            f"due: {r['expected_completion'] or '—'}" +
            (f" | supplier: {r['supplier']}" if r["supplier"] else "")
        )
    return "\n".join(lines)


@mcp.tool()
def get_purchase_order_details(po_id: int) -> str:
    """Get full details of a production order including line items."""
    with _db() as db:
        po = db.execute(
            "SELECT po.*, s.name AS supplier FROM purchase_orders po "
            "LEFT JOIN suppliers s ON s.id=po.supplier_id WHERE po.id=?",
            (po_id,),
        ).fetchone()
        if not po:
            return f"Production order ID {po_id} not found."
        items = db.execute(
            """SELECT CASE WHEN poi.sub_product_id IS NOT NULL
                          THEN par.name || ' — ' || sub.name
                          ELSE p.name END AS display_name,
                      poi.quantity, poi.qty_dispatched, poi.price
               FROM purchase_order_items poi
               LEFT JOIN products p ON p.id=poi.product_id
               LEFT JOIN sub_products sub ON sub.id=poi.sub_product_id
               LEFT JOIN products par ON par.id=sub.product_id
               WHERE poi.po_id=?""",
            (po_id,),
        ).fetchall()

    lines = [
        f"PO-{po_id:04d}: {po['name']}",
        f"Supplier: {po['supplier'] or '—'}",
        f"Status: {po['status']}",
        f"Due: {po['expected_completion'] or '—'}",
        f"Notes: {po['notes'] or '—'}",
        "",
        "Items:",
    ]
    for it in items:
        dispatched = _f(it["qty_dispatched"])
        remaining  = _f(it["quantity"]) - dispatched
        lines.append(
            f"  {it['display_name']} | ordered: {_f(it['quantity']):.0f} | "
            f"dispatched: {dispatched:.0f} | remaining: {remaining:.0f}"
            + (f" | price: {_inr(it['price'])}" if it["price"] else "")
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTION ORDERS — WRITE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_production_order(
    name: str,
    items: list,
    supplier_id: int = None,
    expected_completion: str = "",
    notes: str = "",
) -> str:
    """
    Create a production/purchase order. ONLY call after explicit user confirmation.

    items: list of dicts, each with:
      product_id (int, optional), sub_product_id (int, optional),
      quantity (float, required), price (float, optional)

    Example: [{"product_id": 5, "quantity": 100, "price": 250}]

    Adds the ordered quantities to production_qty for each item.
    """
    if not name.strip():
        return "Error: Order name is required."
    if not items:
        return "Error: At least one item is required."
    if expected_completion:
        try:
            datetime.strptime(expected_completion, "%Y-%m-%d")
        except ValueError:
            return "Error: expected_completion must be YYYY-MM-DD."

    with _db() as db:
        if supplier_id:
            s = db.execute("SELECT id FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
            if not s:
                return f"Supplier ID {supplier_id} not found."
        cur = db.execute(
            "INSERT INTO purchase_orders (name, supplier_id, expected_completion, status, notes) VALUES (?,?,?,?,?)",
            (name.strip(), supplier_id or None, expected_completion or None, "open", notes or None),
        )
        po_id = cur.lastrowid
        for it in items:
            pid  = int(it["product_id"])     if it.get("product_id")     else None
            spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
            qty  = _f(it.get("quantity"))
            price = _f(it.get("price")) or None
            if qty <= 0:
                continue
            db.execute(
                "INSERT INTO purchase_order_items (po_id, product_id, sub_product_id, quantity, price) VALUES (?,?,?,?,?)",
                (po_id, pid, spid, qty, price),
            )
            tbl = "sub_products" if spid else "products"
            pk  = spid if spid else pid
            if pk:
                db.execute(f"UPDATE {tbl} SET production_qty=production_qty+? WHERE id=?", (qty, pk))
        db.commit()
    return f"✓ Production order '{name}' created (PO-{po_id:04d}) with {len(items)} item(s). Status: open."


@mcp.tool()
def update_production_order(
    po_id: int,
    name: str,
    supplier_id: int = None,
    expected_completion: str = "",
    notes: str = "",
) -> str:
    """
    Update the header of a production order (name, supplier, due date, notes).
    ONLY call after explicit user confirmation.
    Note: line items cannot be changed after creation.
    """
    if not name.strip():
        return "Error: Order name is required."
    with _db() as db:
        po = db.execute("SELECT name FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
        if not po:
            return f"Production order ID {po_id} not found."
        db.execute(
            "UPDATE purchase_orders SET name=?, supplier_id=?, expected_completion=?, notes=? WHERE id=?",
            (name.strip(), supplier_id or None, expected_completion or None, notes or None, po_id),
        )
        db.commit()
    return f"✓ Production order PO-{po_id:04d} updated."


@mcp.tool()
def close_production_order(po_id: int) -> str:
    """Mark a production order as closed. ONLY call after explicit user confirmation."""
    with _db() as db:
        po = db.execute("SELECT name, status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
        if not po:
            return f"Production order ID {po_id} not found."
        if po["status"] == "closed":
            return f"PO-{po_id:04d} is already closed."
        db.execute("UPDATE purchase_orders SET status='closed' WHERE id=?", (po_id,))
        db.commit()
    return f"✓ Production order PO-{po_id:04d} ('{po['name']}') closed."


@mcp.tool()
def delete_production_order(po_id: int) -> str:
    """
    PERMANENTLY delete a production order and reverse its production quantities.
    Only call after the user explicitly confirmed deletion.
    """
    with _db() as db:
        po = db.execute("SELECT name FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
        if not po:
            return f"Production order ID {po_id} not found."
        items = db.execute(
            "SELECT product_id, sub_product_id, quantity, qty_dispatched FROM purchase_order_items WHERE po_id=?",
            (po_id,),
        ).fetchall()
        for it in items:
            undispatched = _f(it["quantity"]) - _f(it["qty_dispatched"])
            if undispatched > 0:
                tbl = "sub_products" if it["sub_product_id"] else "products"
                pk  = it["sub_product_id"] if it["sub_product_id"] else it["product_id"]
                if pk:
                    db.execute(f"UPDATE {tbl} SET production_qty=production_qty-? WHERE id=?",
                               (undispatched, pk))
        db.execute("DELETE FROM purchase_orders WHERE id=?", (po_id,))
        db.commit()
    return f"✓ Production order PO-{po_id:04d} ('{po['name']}') permanently deleted and quantities reversed."


# ═══════════════════════════════════════════════════════════════════════════════
# DISPATCHES — READ
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_dispatches(status: str = "in_transit") -> str:
    """List dispatches. status: in_transit | partially_received | received | all"""
    with _db() as db:
        if status == "all":
            rows = db.execute(
                """SELECT d.id, d.name, d.status, d.dispatch_date, d.expected_arrival, s.name AS supplier
                   FROM dispatches d LEFT JOIN suppliers s ON s.id=d.supplier_id
                   ORDER BY d.created_at DESC LIMIT 30"""
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT d.id, d.name, d.status, d.dispatch_date, d.expected_arrival, s.name AS supplier
                   FROM dispatches d LEFT JOIN suppliers s ON s.id=d.supplier_id
                   WHERE d.status=? ORDER BY d.expected_arrival LIMIT 30""",
                (status,),
            ).fetchall()
    if not rows:
        return f"No dispatches with status '{status}'."
    lines = []
    for r in rows:
        lines.append(
            f"DISP-{r['id']:04d} | {r['name']} | {r['status']} | "
            f"dispatched: {r['dispatch_date'] or '—'} | arrival: {r['expected_arrival'] or '—'}"
            + (f" | {r['supplier']}" if r["supplier"] else "")
        )
    return "\n".join(lines)


@mcp.tool()
def get_dispatch_details(dispatch_id: int) -> str:
    """Get full details of a dispatch including items and received quantities."""
    with _db() as db:
        d = db.execute(
            "SELECT d.*, s.name AS supplier FROM dispatches d "
            "LEFT JOIN suppliers s ON s.id=d.supplier_id WHERE d.id=?",
            (dispatch_id,),
        ).fetchone()
        if not d:
            return f"Dispatch ID {dispatch_id} not found."
        items = db.execute(
            """SELECT di.id, di.quantity, di.qty_received,
                      CASE WHEN di.sub_product_id IS NOT NULL
                           THEN par.name || ' — ' || sub.name
                           ELSE p.name END AS display_name
               FROM dispatch_items di
               LEFT JOIN products p ON p.id=di.product_id
               LEFT JOIN sub_products sub ON sub.id=di.sub_product_id
               LEFT JOIN products par ON par.id=sub.product_id
               WHERE di.dispatch_id=? ORDER BY di.id""",
            (dispatch_id,),
        ).fetchall()

    lines = [
        f"DISP-{dispatch_id:04d}: {d['name']}",
        f"Supplier: {d['supplier'] or '—'}",
        f"Status: {d['status']}",
        f"Dispatched: {d['dispatch_date'] or '—'} | Expected arrival: {d['expected_arrival'] or '—'}",
        f"Notes: {d['notes'] or '—'}",
        "",
        "Items (di_id | name | dispatched | received | pending):",
    ]
    for it in items:
        received = _f(it["qty_received"])
        pending  = _f(it["quantity"]) - received
        lines.append(
            f"  di-{it['id']} | {it['display_name']} | "
            f"dispatched: {_f(it['quantity']):.0f} | received: {received:.0f} | pending: {pending:.0f}"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# DISPATCHES — WRITE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_dispatch(
    name: str,
    items: list,
    supplier_id: int = None,
    dispatch_date: str = "",
    expected_arrival: str = "",
    notes: str = "",
) -> str:
    """
    Create a new dispatch. ONLY call after explicit user confirmation.

    items: list of dicts, each with:
      product_id (int, optional), sub_product_id (int, optional),
      quantity (float, required), price (float, optional)

    Moves qty from production_qty → in_transit_qty.
    Automatically allocates against open PO items (FIFO).
    Requires sufficient production stock — check get_stock_summary() first.
    """
    if not name.strip():
        return "Error: Dispatch name is required."
    if not items:
        return "Error: At least one item is required."
    for d in [dispatch_date, expected_arrival]:
        if d:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                return f"Error: dates must be YYYY-MM-DD, got '{d}'."

    with _db() as db:
        # Validate stock
        errors = []
        for it in items:
            pid  = int(it["product_id"])     if it.get("product_id")     else None
            spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
            qty  = _f(it.get("quantity"))
            if spid:
                row = db.execute("SELECT name, production_qty FROM sub_products WHERE id=?", (spid,)).fetchone()
            else:
                row = db.execute("SELECT name, production_qty FROM products WHERE id=?", (pid,)).fetchone()
            if not row:
                errors.append(f"Product/sub-product not found.")
                continue
            available = _f(row["production_qty"])
            if qty > available:
                errors.append(f"{row['name']}: dispatch qty {qty:.0f} exceeds production qty {available:.0f}.")
        if errors:
            return "Error — insufficient production stock:\n" + "\n".join(errors)

        cur = db.execute(
            "INSERT INTO dispatches (name, supplier_id, dispatch_date, expected_arrival, notes) VALUES (?,?,?,?,?)",
            (name.strip(), supplier_id or None,
             dispatch_date or None, expected_arrival or None, notes or None),
        )
        dispatch_id = cur.lastrowid

        warnings = []
        for it in items:
            pid  = int(it["product_id"])     if it.get("product_id")     else None
            spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
            qty  = _f(it.get("quantity"))
            price = _f(it.get("price")) or None

            di_cur = db.execute(
                "INSERT INTO dispatch_items (dispatch_id, product_id, sub_product_id, quantity, price) VALUES (?,?,?,?,?)",
                (dispatch_id, pid, spid, qty, price),
            )
            di_id = di_cur.lastrowid

            leftover = _deduct_production_fifo(db, pid, spid, qty)
            if leftover > 0.001:
                warnings.append(f"{qty - leftover:.0f}/{qty:.0f} units matched to open POs; remainder taken from production.")

            # Move production → transit
            _update_qty(db, pid, spid, "production_qty", -qty)
            _update_qty(db, pid, spid, "in_transit_qty", +qty)

            db.execute(
                "INSERT INTO stock_movements "
                "(product_id, sub_product_id, movement_type, quantity, notes, dispatch_id, expected_arrival) "
                "VALUES (?,?,'transit_dispatch',?,?,?,?)",
                (pid, spid, qty, notes or None, dispatch_id,
                 expected_arrival or None),
            )

        db.commit()

    result = f"✓ Dispatch '{name}' created (DISP-{dispatch_id:04d}) with {len(items)} item(s)."
    if warnings:
        result += "\nWarnings:\n" + "\n".join(f"  ⚠ {w}" for w in warnings)
    return result


@mcp.tool()
def receive_dispatch_items(dispatch_id: int, received_items: list) -> str:
    """
    Record receipt of items from a dispatch. ONLY call after explicit user confirmation.

    received_items: list of dicts: [{"dispatch_item_id": 12, "quantity": 50}, ...]
    Use get_dispatch_details() to find dispatch_item_ids.
    Moves qty from in_transit_qty → stock_qty (warehouse).
    """
    if not received_items:
        return "Error: Provide at least one item with dispatch_item_id and quantity."
    with _db() as db:
        d = db.execute("SELECT name, status FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if not d:
            return f"Dispatch ID {dispatch_id} not found."
        if d["status"] == "received":
            return f"Dispatch DISP-{dispatch_id:04d} is already fully received."

        lines = []
        for it in received_items:
            di_id = int(it["dispatch_item_id"])
            qty   = _f(it.get("quantity"))
            if qty <= 0:
                continue
            row = db.execute("SELECT * FROM dispatch_items WHERE id=? AND dispatch_id=?",
                             (di_id, dispatch_id)).fetchone()
            if not row:
                lines.append(f"  ⚠ dispatch_item_id {di_id} not found in this dispatch — skipped.")
                continue
            max_rcv = _f(row["quantity"]) - _f(row["qty_received"])
            qty = min(qty, max_rcv)
            if qty <= 0:
                lines.append(f"  ⚠ Item {di_id} already fully received — skipped.")
                continue
            db.execute("UPDATE dispatch_items SET qty_received=qty_received+? WHERE id=?", (qty, di_id))
            _update_qty(db, row["product_id"], row["sub_product_id"], "in_transit_qty", -qty)
            _update_qty(db, row["product_id"], row["sub_product_id"], "stock_qty",      +qty)
            db.execute(
                "INSERT INTO stock_movements "
                "(product_id, sub_product_id, movement_type, quantity, notes, dispatch_id) "
                "VALUES (?,?,'transit_arrival',?,?,?)",
                (row["product_id"], row["sub_product_id"], qty,
                 f"Received from DISP-{dispatch_id:04d}", dispatch_id),
            )
            lines.append(f"  ✓ Item {di_id}: {qty:.0f} units received → warehouse stock")

        # Refresh dispatch status
        totals = db.execute(
            "SELECT SUM(quantity) AS tq, SUM(qty_received) AS tr FROM dispatch_items WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        tq, tr = _f(totals["tq"]), _f(totals["tr"])
        new_status = "received" if tr >= tq else ("partially_received" if tr > 0 else "in_transit")
        db.execute("UPDATE dispatches SET status=? WHERE id=?", (new_status, dispatch_id))
        db.commit()

    result = f"✓ Dispatch DISP-{dispatch_id:04d} ('{d['name']}') updated — status: {new_status}.\n"
    result += "\n".join(lines)
    return result


@mcp.tool()
def delete_dispatch(dispatch_id: int) -> str:
    """
    PERMANENTLY delete a dispatch and reverse all stock movements.
    Only call after the user explicitly confirmed deletion.
    Moves unreceived qty back from in_transit → production.
    """
    with _db() as db:
        d = db.execute("SELECT name FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if not d:
            return f"Dispatch ID {dispatch_id} not found."
        items = db.execute("SELECT * FROM dispatch_items WHERE dispatch_id=?", (dispatch_id,)).fetchall()
        for it in items:
            unreceived = _f(it["quantity"]) - _f(it["qty_received"])
            if unreceived > 0:
                _update_qty(db, it["product_id"], it["sub_product_id"], "in_transit_qty", -unreceived)
                _update_qty(db, it["product_id"], it["sub_product_id"], "production_qty", +unreceived)
            # Reverse PO allocations
            allocs = db.execute(
                "SELECT * FROM dispatch_po_allocations WHERE dispatch_item_id=?", (it["id"],)
            ).fetchall()
            for a in allocs:
                db.execute(
                    "UPDATE purchase_order_items SET qty_dispatched=qty_dispatched-? WHERE id=?",
                    (a["quantity"], a["po_item_id"]),
                )
                po_row = db.execute(
                    "SELECT po_id FROM purchase_order_items WHERE id=?", (a["po_item_id"],)
                ).fetchone()
                if po_row:
                    db.execute(
                        "UPDATE purchase_orders SET status='open' WHERE id=? AND status='closed'",
                        (po_row["po_id"],),
                    )
        db.execute("DELETE FROM dispatches WHERE id=?", (dispatch_id,))
        db.commit()
    return f"✓ Dispatch DISP-{dispatch_id:04d} ('{d['name']}') permanently deleted and stock movements reversed."


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()

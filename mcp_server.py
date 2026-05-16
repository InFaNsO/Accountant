"""
Ledger MCP Server
─────────────────
Exposes the Ledger accounting database as tools for Claude Desktop.

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


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _inr(n):
    """Format a number as Indian Rupees string."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    return f"₹{abs(n):,.2f}"


mcp = FastMCP("Ledger", instructions=(
    "You are an AI assistant for Ledger, a small-business accounting app used in India. "
    "Currency is always Indian Rupees (₹ INR). Use Indian number formatting (lakhs/crores) when presenting large numbers. "
    f"Today's date is {date.today().isoformat()}. "
    "Always use the tools to fetch live data before answering any data question — never guess. "
    "For any write operation (record_payment, adjust_stock, create_invoice) state exactly what you will do "
    "and ask the user to confirm before calling the tool."
))


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENTS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_clients(query: str) -> str:
    """Search clients by name or company. Returns matching client IDs and names."""
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
        ob = float(c["opening_balance"] or 0)
        invoices = db.execute(
            "SELECT total, amount_paid, status FROM invoices "
            "WHERE client_id=? AND status != 'cancelled'",
            (client_id,),
        ).fetchall()
        ob_paid = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND invoice_id IS NULL",
            (client_id,),
        ).fetchone()[0]

    inv_balance = sum(float(i["total"]) - float(i["amount_paid"]) for i in invoices)
    if ob >= 0:
        ob_remaining = max(0.0, ob - float(ob_paid))
        excess = max(0.0, float(ob_paid) - ob)
    else:
        ob_remaining = 0.0
        excess = float(ob_paid) + abs(ob)
    balance = -(ob_remaining + inv_balance) + excess

    paid_count    = sum(1 for i in invoices if i["status"] == "paid")
    pending_count = sum(1 for i in invoices if i["status"] in ("issued", "sent", "partial"))

    lines = [
        f"Name:     {c['name']}" + (f" / {c['company']}" if c["company"] else ""),
        f"Email:    {c['email'] or '—'}",
        f"Phone:    {c['phone'] or '—'}",
        f"City:     {c['city'] or '—'}",
        f"Balance:  {_inr(abs(balance))} {'(owes us)' if balance < 0 else ('(credit)' if balance > 0 else '(settled)')}",
        f"Invoices: {len(invoices)} total — {paid_count} paid, {pending_count} pending",
    ]
    if ob != 0:
        lines.append(f"Opening balance: {_inr(abs(ob))} ({'debt' if ob > 0 else 'credit'})")
    return "\n".join(lines)


@mcp.tool()
def get_client_ledger(client_id: int) -> str:
    """Get a full chronological ledger for a client: opening balance, invoices, payments, running balance."""
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

    ob = float(c["opening_balance"] or 0)
    entries = []
    if ob != 0:
        entries.append(("", "opening", f"Opening Balance ({'debt' if ob > 0 else 'credit'})", ob if ob > 0 else 0, abs(ob) if ob < 0 else 0))

    inv_list = [{"sort": r["issue_date"] or "", "kind": "invoice", "row": dict(r)} for r in invoices]
    pay_list = [{"sort": r["payment_date"] or "", "kind": "payment", "row": dict(r)} for r in payments]
    for item in sorted(inv_list + pay_list, key=lambda x: x["sort"]):
        r = item["row"]
        if item["kind"] == "invoice":
            entries.append((r["issue_date"], "invoice", r["invoice_number"], float(r["total"]), 0))
        else:
            label = r["method"] or "payment"
            if r["reference"]: label += f" / {r['reference']}"
            entries.append((r["payment_date"], "payment", label, 0, float(r["amount"])))

    lines = [f"Ledger for {c['name']}", f"{'─'*60}",
             f"{'Date':<12} {'Description':<28} {'Debit':>10} {'Credit':>10} {'Balance':>12}",
             f"{'─'*60}"]
    running = 0.0
    for dt, kind, label, debit, credit in entries:
        running += credit - debit
        lines.append(
            f"{dt or '—':<12} {label[:28]:<28} "
            f"{('₹'+f'{debit:,.0f}') if debit else '—':>10} "
            f"{('₹'+f'{credit:,.0f}') if credit else '—':>10} "
            f"{'₹'+f'{abs(running):,.0f}' + (' CR' if running > 0 else ' DR'):>12}"
        )
    lines.append(f"{'─'*60}")
    status = "CREDIT" if running > 0 else ("DEBIT (owes us)" if running < 0 else "SETTLED")
    lines.append(f"Final balance: {_inr(abs(running))} {status}")
    return "\n".join(lines)


@mcp.tool()
def get_all_clients_summary() -> str:
    """Get all clients with outstanding balances, sorted by most owed first."""
    with _db() as db:
        clients = db.execute("SELECT id, name, company, opening_balance FROM clients ORDER BY name").fetchall()
        result = []
        for c in clients:
            ob = float(c["opening_balance"] or 0)
            rows = db.execute(
                "SELECT total, amount_paid FROM invoices WHERE client_id=? AND status != 'cancelled'",
                (c["id"],),
            ).fetchall()
            ob_paid = float(db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND invoice_id IS NULL",
                (c["id"],),
            ).fetchone()[0])
            inv_bal = sum(float(r["total"]) - float(r["amount_paid"]) for r in rows)
            if ob >= 0:
                balance = -(max(0.0, ob - ob_paid) + inv_bal) + max(0.0, ob_paid - ob)
            else:
                balance = -(inv_bal) + (ob_paid + abs(ob))
            result.append((c["id"], c["name"], c["company"] or "", balance))

    result.sort(key=lambda x: x[3])
    lines = []
    for cid, name, company, balance in result:
        label = f"owes {_inr(abs(balance))}" if balance < 0 else (f"credit {_inr(balance)}" if balance > 0 else "settled")
        lines.append(f"ID {cid}: {name}" + (f" ({company})" if company else "") + f" — {label}")
    return "\n".join(lines) if lines else "No clients found."


# ═══════════════════════════════════════════════════════════════════════════════
# INVOICES
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_invoice_details(invoice_number: str) -> str:
    """Get full details of a specific invoice by number (e.g. INV-0001)."""
    with _db() as db:
        inv = db.execute(
            "SELECT i.*, c.name AS client_name FROM invoices i "
            "JOIN clients c ON c.id = i.client_id WHERE i.invoice_number=?",
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

    remaining = float(inv["total"]) - float(inv["amount_paid"])
    lines = [
        f"Invoice:  {inv['invoice_number']}",
        f"Client:   {inv['client_name']}",
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
        lines.append(f"  {it['description']} × {float(it['quantity'])} @ {_inr(it['unit_price'])} = {_inr(it['line_total'])}")
    if pmts:
        lines.append("\nPayments received:")
        for p in pmts:
            lines.append(f"  {p['payment_date']} — {_inr(p['amount'])} via {p['method']}" +
                         (f" (ref: {p['reference']})" if p["reference"] else ""))
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
        remaining = float(r["total"]) - float(r["amount_paid"])
        overdue = r["due_date"] and r["due_date"] < today and r["status"] not in ("paid", "cancelled")
        status_str = r["status"]
        if overdue:
            days = (date.today() - date.fromisoformat(r["due_date"])).days
            status_str += f" ⚠ {days}d overdue"
        lines.append(
            f"{r['invoice_number']} | {r['issue_date']} | "
            f"{_inr(r['total'])} | paid {_inr(r['amount_paid'])} | "
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
               FROM invoices i JOIN clients c ON c.id = i.client_id
               WHERE i.due_date < ? AND i.status NOT IN ('paid','cancelled')
               ORDER BY i.due_date""",
            (today,),
        ).fetchall()
    if not rows:
        return "No overdue invoices — all up to date!"
    lines = []
    for r in rows:
        days = (date.today() - date.fromisoformat(r["due_date"])).days
        remaining = float(r["total"]) - float(r["amount_paid"])
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
               FROM invoices i JOIN clients c ON c.id = i.client_id
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
# PAYMENTS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_recent_payments(limit: int = 10) -> str:
    """Get the most recent payments received across all clients."""
    with _db() as db:
        rows = db.execute(
            """SELECT p.amount, p.payment_date, p.method, p.reference,
                      c.name AS client_name, i.invoice_number
               FROM payments p
               JOIN clients c ON c.id = p.client_id
               LEFT JOIN invoices i ON i.id = p.invoice_id
               ORDER BY p.payment_date DESC, p.id DESC LIMIT ?""",
            (min(limit, 50),),
        ).fetchall()
    if not rows:
        return "No payments found."
    return "\n".join(
        f"{r['payment_date']} | {r['client_name']} | {_inr(r['amount'])} | "
        f"{r['method']}" + (f" | {r['invoice_number']}" if r["invoice_number"] else " | unallocated") +
        (f" | ref: {r['reference']}" if r["reference"] else "")
        for r in rows
    )


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
    invoice_id: optional — links payment to a specific invoice
    """
    valid_methods = {"cash", "bank_transfer", "cheque", "upi", "other"}
    if method not in valid_methods:
        return f"Invalid method '{method}'. Must be one of: {', '.join(sorted(valid_methods))}"
    try:
        datetime.strptime(payment_date, "%Y-%m-%d")
    except ValueError:
        return "Invalid date format — use YYYY-MM-DD."
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
            (client_id, invoice_id or None, amount, payment_date, method,
             reference or None, notes or "Recorded via Claude Desktop"),
        )

        if invoice_id:
            inv = db.execute("SELECT total, amount_paid FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            new_paid = float(inv["amount_paid"]) + amount
            status = "paid" if new_paid >= float(inv["total"]) else ("partial" if new_paid > 0 else "issued")
            db.execute("UPDATE invoices SET amount_paid=?, status=? WHERE id=?", (new_paid, status, invoice_id))
        db.commit()

    return (
        f"✓ Payment of {_inr(amount)} recorded for {client['name']}.\n"
        f"  Date: {payment_date} | Method: {method}"
        + (f" | Invoice ID: {invoice_id}" if invoice_id else " | Unallocated")
        + (f" | Ref: {reference}" if reference else "")
    )


# ═══════════════════════════════════════════════════════════════════════════════
# BUSINESS STATS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_business_stats() -> str:
    """Overall business snapshot: revenue, outstanding, overdue, recent activity."""
    today = date.today().isoformat()
    with _db() as db:
        total_revenue = float(db.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0])
        outstanding   = float(db.execute(
            "SELECT COALESCE(SUM(total-amount_paid),0) FROM invoices WHERE status NOT IN ('paid','cancelled')"
        ).fetchone()[0])
        client_count  = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        overdue_count = db.execute(
            "SELECT COUNT(*) FROM invoices WHERE due_date<? AND status NOT IN ('paid','cancelled')", (today,)
        ).fetchone()[0]
        overdue_amt   = float(db.execute(
            "SELECT COALESCE(SUM(total-amount_paid),0) FROM invoices WHERE due_date<? AND status NOT IN ('paid','cancelled')", (today,)
        ).fetchone()[0])
        recent_pmts   = float(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE payment_date>=date('now','-30 days')"
        ).fetchone()[0])
        recent_inv    = float(db.execute(
            "SELECT COALESCE(SUM(total),0) FROM invoices WHERE issue_date>=date('now','-30 days') AND status!='cancelled'"
        ).fetchone()[0])
        inv_count     = db.execute("SELECT COUNT(*) FROM invoices WHERE status!='cancelled'").fetchone()[0]
        product_count = db.execute("SELECT COUNT(*) FROM products WHERE is_active=1").fetchone()[0]

    return "\n".join([
        f"Total revenue collected : {_inr(total_revenue)}",
        f"Outstanding (unpaid)    : {_inr(outstanding)}",
        f"Overdue                 : {overdue_count} invoice(s) totalling {_inr(overdue_amt)}",
        f"Clients                 : {client_count}",
        f"Invoices (active)       : {inv_count}",
        f"Active products         : {product_count}",
        f"─── Last 30 days ───────────────────",
        f"Invoiced                : {_inr(recent_inv)}",
        f"Payments received       : {_inr(recent_pmts)}",
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTS & STOCK
# ═══════════════════════════════════════════════════════════════════════════════

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
            """SELECT s.id, s.name, s.sku, s.unit_price, s.stock_qty, p.name AS parent
               FROM sub_products s JOIN products p ON p.id=s.product_id
               WHERE s.is_active=1 AND (LOWER(s.name) LIKE ? OR LOWER(COALESCE(s.sku,'')) LIKE ?)
               ORDER BY p.name, s.name LIMIT 10""",
            (q, q),
        ).fetchall()
    lines = []
    for p in products:
        lines.append(f"Product ID {p['id']}: {p['name']}" +
                     (f" [{p['sku']}]" if p["sku"] else "") +
                     f" | {_inr(p['unit_price'])} | stock: {float(p['stock_qty'] or 0):.0f}")
    for s in subs:
        lines.append(f"Sub-product ID {s['id']}: {s['parent']} — {s['name']}" +
                     (f" [{s['sku']}]" if s["sku"] else "") +
                     f" | {_inr(s['unit_price'])} | stock: {float(s['stock_qty'] or 0):.0f}")
    return "\n".join(lines) if lines else "No products found matching that query."


@mcp.tool()
def get_stock_summary() -> str:
    """Current stock levels for all products and sub-products (warehouse / production / transit)."""
    with _db() as db:
        products = db.execute(
            "SELECT name, sku, stock_qty, production_qty, in_transit_qty, min_quantity "
            "FROM products WHERE is_active=1 ORDER BY name"
        ).fetchall()
        subs = db.execute(
            """SELECT p.name AS parent, s.name, s.sku, s.stock_qty, s.production_qty,
                      s.in_transit_qty, s.min_quantity
               FROM sub_products s JOIN products p ON p.id=s.product_id
               WHERE s.is_active=1 ORDER BY p.name, s.name"""
        ).fetchall()

    def _row(name, sku, stock, prod, transit, min_qty):
        alert = " ⚠ LOW" if min_qty and float(stock or 0) < float(min_qty) else ""
        return (f"{name}" + (f" [{sku}]" if sku else "") +
                f" | wh:{float(stock or 0):.0f} prod:{float(prod or 0):.0f} transit:{float(transit or 0):.0f}{alert}")

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
            """SELECT name, sku, stock_qty, min_quantity FROM products
               WHERE is_active=1 AND min_quantity>0 AND stock_qty<min_quantity ORDER BY (stock_qty-min_quantity)"""
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
        shortage = float(p["min_quantity"]) - float(p["stock_qty"] or 0)
        lines.append(f"{p['name']}" + (f" [{p['sku']}]" if p["sku"] else "") +
                     f" | stock: {float(p['stock_qty'] or 0):.0f} / min: {float(p['min_quantity']):.0f} | short by {shortage:.0f}")
    for s in low_s:
        shortage = float(s["eff_min"]) - float(s["stock_qty"] or 0)
        lines.append(f"{s['parent']} — {s['name']}" + (f" [{s['sku']}]" if s["sku"] else "") +
                     f" | stock: {float(s['stock_qty'] or 0):.0f} / min: {float(s['eff_min']):.0f} | short by {shortage:.0f}")
    return f"{len(lines)} low-stock alert(s):\n" + "\n".join(lines)


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
    Adjust stock for a product. ONLY call after explicit user confirmation.
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
            (product_id, sub_product_id or None, movement, quantity, notes or "Adjusted via Claude Desktop"),
        )
        db.commit()

    return f"✓ {name} {bucket} stock {direction}d by {quantity:.0f}. Notes: {notes or '—'}"


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_suppliers() -> str:
    """List all active suppliers."""
    with _db() as db:
        rows = db.execute(
            "SELECT id, name, company, email, phone FROM suppliers WHERE is_active=1 ORDER BY name"
        ).fetchall()
    if not rows:
        return "No suppliers found."
    return "\n".join(
        f"ID {r['id']}: {r['name']}" +
        (f" ({r['company']})" if r.get("company") else "") +
        (f" | {r['email']}" if r["email"] else "") +
        (f" | {r['phone']}" if r["phone"] else "")
        for r in rows
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PURCHASE ORDERS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_purchase_orders(status: str = "open") -> str:
    """
    List purchase orders. status: open | closed | all
    """
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
        return f"No {status} purchase orders found."
    lines = []
    for r in rows:
        lines.append(
            f"PO-{r['id']:04d} {r['name']} | {r['status']} | "
            f"due: {r['expected_completion'] or '—'}" +
            (f" | supplier: {r['supplier']}" if r["supplier"] else "")
        )
    return "\n".join(lines)


@mcp.tool()
def get_purchase_order_details(po_id: int) -> str:
    """Get full details of a purchase order including line items."""
    with _db() as db:
        po = db.execute(
            "SELECT po.*, s.name AS supplier FROM purchase_orders po "
            "LEFT JOIN suppliers s ON s.id=po.supplier_id WHERE po.id=?",
            (po_id,),
        ).fetchone()
        if not po:
            return f"Purchase order ID {po_id} not found."
        items = db.execute(
            """SELECT p.name AS product, s.name AS sub, poi.quantity, poi.qty_dispatched, poi.price
               FROM purchase_order_items poi
               LEFT JOIN products p ON p.id=poi.product_id
               LEFT JOIN sub_products s ON s.id=poi.sub_product_id
               WHERE poi.po_id=?""",
            (po_id,),
        ).fetchall()

    lines = [
        f"PO: {po['name']}",
        f"Supplier: {po['supplier'] or '—'}",
        f"Status: {po['status']}",
        f"Expected completion: {po['expected_completion'] or '—'}",
        f"Notes: {po['notes'] or '—'}",
        "",
        "Items:",
    ]
    for it in items:
        name = it["product"] or "?"
        if it["sub"]:
            name += f" — {it['sub']}"
        dispatched = float(it["qty_dispatched"] or 0)
        lines.append(
            f"  {name} | ordered: {float(it['quantity']):.0f} | "
            f"dispatched: {dispatched:.0f} | remaining: {float(it['quantity'])-dispatched:.0f}"
            + (f" | price: {_inr(it['price'])}" if it["price"] else "")
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# DISPATCHES / TRANSIT
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_dispatches(status: str = "in_transit") -> str:
    """
    List dispatches. status: in_transit | partially_received | received | all
    """
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
        return f"No dispatches with status '{status}' found."
    lines = []
    for r in rows:
        lines.append(
            f"DISP-{r['id']:04d} {r['name']} | {r['status']} | "
            f"dispatched: {r['dispatch_date'] or '—'} | "
            f"expected arrival: {r['expected_arrival'] or '—'}"
            + (f" | {r['supplier']}" if r["supplier"] else "")
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()

"""
Ledger MCP Server
─────────────────
Exposes the Ledger accounting database as tools for Claude Desktop.
Claude Desktop connects to this server; no Anthropic API key needed.

Setup (Claude Desktop):
  Add to %APPDATA%/Claude/claude_desktop_config.json:
  {
    "mcpServers": {
      "ledger": {
        "command": "C:/Users/Lenovo/miniconda3/python.exe",
        "args": ["C:/Users/Lenovo/Documents/GitHub/Accountant/mcp_server.py"]
      }
    }
  }
"""

import sqlite3
import json
from pathlib import Path
from datetime import date
from mcp.server.fastmcp import FastMCP

# ── DB path ───────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "data" / "ledger.db"


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("Ledger", instructions=(
    "You are an AI assistant for Ledger, a small business accounting app used in India. "
    "Currency is always Indian Rupees (₹ INR). Use Indian number formatting (lakhs/crores). "
    f"Today's date is {date.today().isoformat()}. "
    "Use the tools to fetch live data before answering any data question. "
    "Before calling record_payment, state exactly what you will record and ask the user to confirm."
))


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_clients(query: str) -> str:
    """Search for clients by name or company. Returns matching client IDs and names."""
    q = f"%{query.lower()}%"
    with _db() as db:
        rows = db.execute(
            "SELECT id, name, company FROM clients "
            "WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(company,'')) LIKE ? "
            "ORDER BY name LIMIT 10",
            (q, q),
        ).fetchall()
    if not rows:
        return "No clients found matching that name."
    lines = [f"ID {r['id']}: {r['name']}" + (f" ({r['company']})" if r["company"] else "") for r in rows]
    return "\n".join(lines)


@mcp.tool()
def get_client_details(client_id: int) -> str:
    """Get full details and current balance for a client."""
    with _db() as db:
        c = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not c:
            return "Client not found."

        # Compute balance from invoices + payments + opening_balance
        invoices = db.execute(
            "SELECT total, amount_paid, status FROM invoices WHERE client_id=? AND status != 'cancelled'",
            (client_id,),
        ).fetchall()
        inv_total  = sum(float(i["total"]) for i in invoices)
        inv_paid   = sum(float(i["amount_paid"]) for i in invoices)
        ob = float(c["opening_balance"] or 0)
        # Positive opening_balance = client was already in debt
        balance = -(inv_total - inv_paid) - ob  # negative = owes us money

        paid_count    = sum(1 for i in invoices if i["status"] == "paid")
        pending_count = sum(1 for i in invoices if i["status"] in ("issued", "partial"))

    lines = [
        f"Name:     {c['name']}" + (f" / {c['company']}" if c["company"] else ""),
        f"Email:    {c['email'] or '—'}",
        f"Phone:    {c['phone'] or '—'}",
        f"Balance:  ₹{abs(balance):,.2f} {'(owes us)' if balance < 0 else ('(credit)' if balance > 0 else '(settled)')}",
        f"Invoices: {len(invoices)} total — {paid_count} paid, {pending_count} pending",
        f"Total invoiced: ₹{inv_total:,.2f}",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_client_invoices(client_id: int) -> str:
    """List all invoices for a client with amounts and payment status."""
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
            f"₹{float(r['total']):,.2f} | paid ₹{float(r['amount_paid']):,.2f} | "
            f"remaining ₹{remaining:,.2f} | {status_str}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_all_clients_summary() -> str:
    """Get all clients with their current outstanding balances, sorted by most owed."""
    with _db() as db:
        clients = db.execute("SELECT id, name, company, opening_balance FROM clients ORDER BY name").fetchall()
        result = []
        for c in clients:
            ob = float(c["opening_balance"] or 0)
            invoices = db.execute(
                "SELECT total, amount_paid FROM invoices WHERE client_id=? AND status != 'cancelled'",
                (c["id"],),
            ).fetchall()
            inv_total = sum(float(i["total"]) for i in invoices)
            inv_paid  = sum(float(i["amount_paid"]) for i in invoices)
            balance = -(inv_total - inv_paid) - ob
            result.append((c["id"], c["name"], c["company"] or "", balance))

    result.sort(key=lambda x: x[3])   # most owed first (negative = owes)
    lines = []
    for cid, name, company, balance in result:
        label = f"owes ₹{abs(balance):,.2f}" if balance < 0 else (f"credit ₹{balance:,.2f}" if balance > 0 else "settled")
        lines.append(f"ID {cid}: {name}" + (f" ({company})" if company else "") + f" — {label}")
    return "\n".join(lines) if lines else "No clients found."


@mcp.tool()
def get_overdue_invoices() -> str:
    """List all invoices that are past their due date and not yet paid."""
    today = date.today().isoformat()
    with _db() as db:
        rows = db.execute(
            """SELECT i.id, i.invoice_number, i.issue_date, i.due_date,
                      i.total, i.amount_paid, c.name AS client_name
               FROM invoices i
               JOIN clients c ON c.id = i.client_id
               WHERE i.due_date < ? AND i.status NOT IN ('paid','cancelled')
               ORDER BY i.due_date""",
            (today,),
        ).fetchall()
    if not rows:
        return "No overdue invoices."
    lines = []
    for r in rows:
        days = (date.today() - date.fromisoformat(r["due_date"])).days
        remaining = float(r["total"]) - float(r["amount_paid"])
        lines.append(
            f"{r['invoice_number']} | {r['client_name']} | due {r['due_date']} "
            f"({days}d ago) | ₹{remaining:,.2f} remaining"
        )
    return f"{len(lines)} overdue invoice(s):\n" + "\n".join(lines)


@mcp.tool()
def get_business_stats() -> str:
    """Get overall business stats: revenue, outstanding, client count, overdue invoices."""
    today = date.today().isoformat()
    with _db() as db:
        total_revenue = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments"
        ).fetchone()[0]
        outstanding = db.execute(
            "SELECT COALESCE(SUM(total - amount_paid),0) FROM invoices "
            "WHERE status NOT IN ('paid','cancelled')"
        ).fetchone()[0]
        client_count = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        overdue_count = db.execute(
            "SELECT COUNT(*) FROM invoices WHERE due_date < ? AND status NOT IN ('paid','cancelled')",
            (today,),
        ).fetchone()[0]
        recent = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments "
            "WHERE payment_date >= date('now','-30 days')"
        ).fetchone()[0]

    return "\n".join([
        f"Total revenue collected: ₹{float(total_revenue):,.2f}",
        f"Outstanding (unpaid):    ₹{float(outstanding):,.2f}",
        f"Total clients:           {client_count}",
        f"Overdue invoices:        {overdue_count}",
        f"Payments last 30 days:   ₹{float(recent):,.2f}",
    ])


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
    lines = [
        f"{r['invoice_number']} | {r['name']} | {r['issue_date']} | "
        f"₹{float(r['total']):,.2f} | {r['status']}"
        for r in rows
    ]
    return "\n".join(lines)


@mcp.tool()
def get_invoice_details(invoice_number: str) -> str:
    """Get full details of a specific invoice by invoice number (e.g. INV-0001)."""
    with _db() as db:
        inv = db.execute(
            "SELECT i.*, c.name AS client_name FROM invoices i "
            "JOIN clients c ON c.id = i.client_id "
            "WHERE i.invoice_number=?",
            (invoice_number.upper(),),
        ).fetchone()
        if not inv:
            return f"Invoice {invoice_number} not found."
        items = db.execute(
            "SELECT description, quantity, unit_price, tax_rate, line_total FROM invoice_items WHERE invoice_id=?",
            (inv["id"],),
        ).fetchall()

    lines = [
        f"Invoice:  {inv['invoice_number']}",
        f"Client:   {inv['client_name']}",
        f"Date:     {inv['issue_date']}  |  Due: {inv['due_date'] or '—'}",
        f"Status:   {inv['status']}",
        f"Subtotal: ₹{float(inv['subtotal']):,.2f}",
        f"Tax:      ₹{float(inv['tax_total']):,.2f}",
        f"Total:    ₹{float(inv['total']):,.2f}",
        f"Paid:     ₹{float(inv['amount_paid']):,.2f}",
        f"Balance:  ₹{float(inv['total']) - float(inv['amount_paid']):,.2f}",
        "",
        "Line items:",
    ]
    for it in items:
        lines.append(f"  {it['description']} × {float(it['quantity'])} @ ₹{float(it['unit_price']):,.2f} = ₹{float(it['line_total']):,.2f}")
    return "\n".join(lines)


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
    Record a payment from a client.
    method must be one of: cash, bank_transfer, cheque, upi, other
    payment_date must be YYYY-MM-DD.
    Only call this after the user has explicitly confirmed.
    """
    valid_methods = {"cash", "bank_transfer", "cheque", "upi", "other"}
    if method not in valid_methods:
        return f"Invalid method '{method}'. Must be one of: {', '.join(sorted(valid_methods))}"

    with _db() as db:
        client = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            return f"Client ID {client_id} not found."

        db.execute(
            "INSERT INTO payments (client_id, invoice_id, amount, payment_date, method, reference, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (client_id, invoice_id or None, amount, payment_date, method, reference,
             notes or "Recorded via Claude Desktop"),
        )

        # Update invoice amount_paid if linked
        if invoice_id:
            inv = db.execute("SELECT total, amount_paid FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if inv:
                new_paid = float(inv["amount_paid"]) + amount
                new_status = "paid" if new_paid >= float(inv["total"]) else ("partial" if new_paid > 0 else "issued")
                db.execute(
                    "UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
                    (new_paid, new_status, invoice_id),
                )
        db.commit()

    return (
        f"✓ Payment of ₹{amount:,.2f} recorded for {client['name']}.\n"
        f"  Date: {payment_date} | Method: {method}"
        + (f" | Invoice: {invoice_id}" if invoice_id else "")
        + (f" | Ref: {reference}" if reference else "")
    )


@mcp.tool()
def get_stock_summary() -> str:
    """Get a summary of current stock levels: warehouse, production, and transit quantities."""
    with _db() as db:
        products = db.execute(
            "SELECT name, sku, stock_qty, production_qty, in_transit_qty, min_quantity "
            "FROM products WHERE is_active=1 ORDER BY name"
        ).fetchall()
        subs = db.execute(
            """SELECT p.name AS parent, s.name, s.sku, s.stock_qty, s.production_qty,
                      s.in_transit_qty, s.min_quantity
               FROM sub_products s JOIN products p ON p.id = s.product_id
               WHERE s.is_active=1 ORDER BY p.name, s.name"""
        ).fetchall()

    lines = ["=== Products ==="]
    for p in products:
        alert = " ⚠ LOW" if p["min_quantity"] and float(p["stock_qty"] or 0) < float(p["min_quantity"]) else ""
        lines.append(
            f"{p['name']}" + (f" [{p['sku']}]" if p["sku"] else "") +
            f" | warehouse:{float(p['stock_qty'] or 0):.0f}"
            f" prod:{float(p['production_qty'] or 0):.0f}"
            f" transit:{float(p['in_transit_qty'] or 0):.0f}{alert}"
        )

    if subs:
        lines.append("\n=== Sub-products ===")
        for s in subs:
            alert = " ⚠ LOW" if s["min_quantity"] and float(s["stock_qty"] or 0) < float(s["min_quantity"]) else ""
            lines.append(
                f"{s['parent']} — {s['name']}" + (f" [{s['sku']}]" if s["sku"] else "") +
                f" | warehouse:{float(s['stock_qty'] or 0):.0f}"
                f" prod:{float(s['production_qty'] or 0):.0f}"
                f" transit:{float(s['in_transit_qty'] or 0):.0f}{alert}"
            )

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()

"""
Ledger Flask API Blueprint
─────────────────────────
Exposes all 48 MCP tool operations over HTTP at /api/.
Protected by X-MCP-Key header checked against MCP_API_KEY env var.

Response format:
  Success: {"result": "...text string..."}, 200
  Error:   {"error": "...message..."},      400 | 401 | 404 | 503
"""

import os
from datetime import date, datetime
from functools import wraps

from flask import Blueprint, jsonify, request

from ..database import get_db

bp = Blueprint("api", __name__, url_prefix="/api")


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _api_key():
    return os.environ.get("MCP_API_KEY", "")


def _auth_check():
    key = _api_key()
    if not key:
        return jsonify({"error": "MCP_API_KEY not configured on server"}), 503
    if request.headers.get("X-MCP-Key", "") != key:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        err = _auth_check()
        if err:
            return err
        return f(*args, **kwargs)
    return decorated


# ── Formatting / computation helpers ─────────────────────────────────────────

def _inr(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    return f"₹{abs(n):,.2f}"


def _f(v, default=0.0):
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
    paid = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS paid FROM payment_allocations WHERE invoice_id=?",
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


def _client_unallocated(db, client_id):
    row = db.execute(
        """SELECT COALESCE(SUM(p.amount),0) - COALESCE(SUM(pa.amount),0) AS unallocated
           FROM payments p
           LEFT JOIN (
               SELECT payment_id, SUM(amount) AS amount
               FROM payment_allocations GROUP BY payment_id
           ) pa ON pa.payment_id = p.id
           WHERE p.client_id = ?""",
        (client_id,),
    ).fetchone()
    return _f(row["unallocated"])


def _ob_remaining(db, client_id):
    client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return 0.0
    debt = abs(_f(client["opening_balance"]))
    if debt == 0:
        return 0.0
    return max(0.0, debt - _client_unallocated(db, client_id))


def _apply_client_credit(db, client_id, invoice_id):
    """If the client has surplus (unallocated payment money beyond OB), allocate it
    to this freshly-issued invoice via payment_allocations rows."""
    client = db.execute("SELECT opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    opening_debt = max(0.0, _f(client["opening_balance"])) if client else 0.0
    unallocated = _client_unallocated(db, client_id)
    credit = max(0.0, unallocated - opening_debt)
    if credit < 0.01:
        return
    inv = db.execute("SELECT total, amount_paid FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        return
    gap = _f(inv["total"]) - _f(inv["amount_paid"])
    to_cover = min(credit, gap)
    if to_cover < 0.01:
        return

    # Walk payments oldest-first, allocating their unallocated portion.
    pmts = db.execute(
        """SELECT p.id, p.amount, COALESCE(SUM(pa.amount),0) AS allocated
           FROM payments p
           LEFT JOIN payment_allocations pa ON pa.payment_id = p.id
           WHERE p.client_id = ?
           GROUP BY p.id
           HAVING p.amount - COALESCE(SUM(pa.amount),0) > 0.001
           ORDER BY p.created_at ASC, p.id ASC""",
        (client_id,),
    ).fetchall()
    covered = 0.0
    for pmt in pmts:
        if covered >= to_cover - 0.001:
            break
        avail = _f(pmt["amount"]) - _f(pmt["allocated"])
        take  = min(avail, to_cover - covered)
        if take < 0.001:
            continue
        db.execute(
            "INSERT INTO payment_allocations (payment_id, invoice_id, amount) VALUES (?,?,?)",
            (pmt["id"], invoice_id, take),
        )
        covered += take
    if covered > 0.001:
        _refresh_invoice_paid(db, invoice_id)


def _deduct_production_fifo(db, product_id, sub_product_id, qty_needed):
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
    return remaining


def _update_qty(db, product_id, sub_product_id, field, delta):
    tbl = "sub_products" if sub_product_id else "products"
    pk = sub_product_id if sub_product_id else product_id
    db.execute(f"UPDATE {tbl} SET {field}={field}+? WHERE id=?", (delta, pk))


def _jb():
    """Parse JSON body; return empty dict on failure."""
    try:
        data = request.get_json(force=True, silent=True)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ═════════════════════════════════════════════════════════════════════════════
# CLIENTS — READ
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/clients/search")
@require_auth
def search_clients():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    q = f"%{query.lower()}%"
    db = get_db()
    # Match on client name or any of their company names
    rows = db.execute(
        """SELECT DISTINCT c.id, c.name
           FROM clients c
           LEFT JOIN client_companies cc ON cc.client_id = c.id
           WHERE LOWER(c.name) LIKE ? OR LOWER(COALESCE(cc.name,'')) LIKE ?
           ORDER BY c.name LIMIT 15""",
        (q, q),
    ).fetchall()
    if not rows:
        return jsonify({"result": "No clients found matching that query."})
    lines = []
    for r in rows:
        cos = db.execute(
            "SELECT name FROM client_companies WHERE client_id=? ORDER BY name",
            (r["id"],),
        ).fetchall()
        co_str = ", ".join(c["name"] for c in cos)
        lines.append(f"ID {r['id']}: {r['name']}" + (f" [{co_str}]" if co_str else ""))
    return jsonify({"result": "\n".join(lines)})


@bp.route("/clients/summary")
@require_auth
def get_all_clients_summary():
    db = get_db()
    clients = db.execute(
        "SELECT id, name, company, opening_balance FROM clients ORDER BY name"
    ).fetchall()
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
    return jsonify({"result": "\n".join(lines) if lines else "No clients found."})


@bp.route("/clients/<int:client_id>")
@require_auth
def get_client_details(client_id):
    db = get_db()
    c = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404
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
    paid_count = sum(1 for i in invoices if i["status"] == "paid")
    pending_count = sum(1 for i in invoices if i["status"] in ("issued", "sent", "partial"))
    lines = [
        f"ID:       {c['id']}",
        f"Name:     {c['name']}",
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
    # Companies
    companies = db.execute(
        "SELECT id, name, tax_id, opening_balance FROM client_companies WHERE client_id=? ORDER BY name",
        (client_id,),
    ).fetchall()
    if companies:
        lines.append(f"\nCompanies ({len(companies)}):")
        for co in companies:
            co_ob = _f(co["opening_balance"])
            co_inv_rows = db.execute(
                "SELECT total, amount_paid FROM invoices WHERE client_id=? AND company_id=? AND status!='cancelled'",
                (client_id, co["id"]),
            ).fetchall()
            co_paid = _f(db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND company_id=?",
                (client_id, co["id"]),
            ).fetchone()[0])
            co_inv_bal = sum(_f(r["total"]) - _f(r["amount_paid"]) for r in co_inv_rows)
            credit_ob = max(0.0, -co_ob)
            debit_ob  = max(0.0, co_ob)
            co_balance = (co_paid + credit_ob) - (co_inv_bal + debit_ob)
            bal_label = (f"{_inr(abs(co_balance))} owes us" if co_balance < 0
                         else (f"{_inr(co_balance)} credit" if co_balance > 0 else "settled"))
            tax_str = f" | GST: {co['tax_id']}" if co["tax_id"] else ""
            lines.append(f"  ID {co['id']}: {co['name']}{tax_str} — {bal_label}")
    return jsonify({"result": "\n".join(lines)})


@bp.route("/clients/<int:client_id>/ledger")
@require_auth
def get_client_ledger(client_id):
    """Client ledger. Default returns a text table (backward-compatible).
    Pass ?format=json for structured rows; supports date_from/date_to filtering."""
    db = get_db()
    c = db.execute("SELECT name, opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404
    company_id = request.args.get("company_id", type=int)
    fmt        = (request.args.get("format") or "text").lower()
    date_from  = request.args.get("date_from")
    date_to    = request.args.get("date_to")
    try:
        if date_from: datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:   datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date_from / date_to must be YYYY-MM-DD."}), 400

    if company_id:
        co_row = db.execute("SELECT name, opening_balance FROM client_companies WHERE id=? AND client_id=?",
                            (company_id, client_id)).fetchone()
        if not co_row:
            return jsonify({"error": f"Company ID {company_id} not found for this client."}), 404
        ledger_name = f"{c['name']} / {co_row['name']}"
        ob = _f(co_row["opening_balance"])
        inv_where  = "WHERE client_id=? AND company_id=? AND status != 'cancelled'"
        inv_params = [client_id, company_id]
        pay_where  = "WHERE client_id=? AND company_id=?"
        pay_params = [client_id, company_id]
    else:
        ledger_name = c["name"]
        ob = _f(c["opening_balance"])
        inv_where  = "WHERE client_id=? AND status != 'cancelled'"
        inv_params = [client_id]
        pay_where  = "WHERE client_id=?"
        pay_params = [client_id]

    if date_from:
        inv_where += " AND issue_date >= ?";    inv_params.append(date_from)
        pay_where += " AND payment_date >= ?";  pay_params.append(date_from)
    if date_to:
        inv_where += " AND issue_date <= ?";    inv_params.append(date_to)
        pay_where += " AND payment_date <= ?";  pay_params.append(date_to)

    invoices = db.execute(
        f"SELECT invoice_number, issue_date, total, status FROM invoices "
        f"{inv_where} ORDER BY issue_date, id",
        inv_params,
    ).fetchall()
    payments = db.execute(
        f"SELECT id, amount, payment_date, method, reference, notes FROM payments "
        f"{pay_where} ORDER BY payment_date, id",
        pay_params,
    ).fetchall()

    entries = []
    # Opening balance only appears when not date-windowed (matches existing UI behavior)
    if ob != 0 and not date_from:
        entries.append({
            "date": "", "type": "opening",
            "label": f"Opening Balance ({'debt' if ob > 0 else 'credit'})",
            "debit":  ob if ob > 0 else 0,
            "credit": abs(ob) if ob < 0 else 0,
        })

    merged = sorted(
        [{"sort": r["issue_date"] or "",  "kind": "invoice", "row": dict(r)} for r in invoices] +
        [{"sort": r["payment_date"] or "", "kind": "payment", "row": dict(r)} for r in payments],
        key=lambda x: x["sort"],
    )
    pmt_ids = [m["row"]["id"] for m in merged if m["kind"] == "payment"]
    allocs_by_pmt = {}
    if pmt_ids:
        ph = ",".join("?" * len(pmt_ids))
        for r in db.execute(
            f"""SELECT pa.payment_id, pa.amount, i.invoice_number
                FROM payment_allocations pa JOIN invoices i ON i.id = pa.invoice_id
                WHERE pa.payment_id IN ({ph})""",
            pmt_ids,
        ).fetchall():
            allocs_by_pmt.setdefault(r["payment_id"], []).append(dict(r))

    for item in merged:
        r = item["row"]
        if item["kind"] == "invoice":
            entries.append({
                "date": r["issue_date"], "type": "invoice",
                "label": r["invoice_number"], "debit": _f(r["total"]), "credit": 0,
            })
        else:
            label = r["method"] or "payment"
            if r["reference"]:
                label += f" / {r['reference']}"
            allocs = allocs_by_pmt.get(r["id"], [])
            entries.append({
                "date": r["payment_date"], "type": "payment",
                "label": label, "debit": 0, "credit": _f(r["amount"]),
                "payment_id": r["id"], "allocations": allocs,
            })

    # Compute running balance
    running = 0.0
    for e in entries:
        running += e["credit"] - e["debit"]
        e["running"] = running
    final_balance = running

    if fmt == "json":
        return jsonify({"result": {
            "client_id":     client_id,
            "ledger_name":   ledger_name,
            "company_id":    company_id,
            "date_from":     date_from,
            "date_to":       date_to,
            "entries":       entries,
            "final_balance": final_balance,
        }})

    # Text format (legacy)
    lines = [
        f"Ledger for {ledger_name}", "─" * 64,
        f"{'Date':<12} {'Description':<30} {'Debit':>10} {'Credit':>10} {'Balance':>12}",
        "─" * 64,
    ]
    for e in entries:
        dt = e["date"] or "—"
        lbl = e["label"][:30]
        debit, credit, run = e["debit"], e["credit"], e["running"]
        lines.append(
            f"{dt:<12} {lbl:<30} "
            f"{('₹'+f'{debit:,.0f}') if debit else '—':>10} "
            f"{('₹'+f'{credit:,.0f}') if credit else '—':>10} "
            f"{'₹'+f'{abs(run):,.0f}' + (' CR' if run > 0 else ' DR'):>12}"
        )
    lines.append("─" * 64)
    lines.append(f"Final balance: {_inr(abs(final_balance))} "
                 f"{'CREDIT' if final_balance > 0 else ('DEBIT – owes us' if final_balance < 0 else 'SETTLED')}")
    return jsonify({"result": "\n".join(lines)})


@bp.route("/clients/<int:client_id>/invoices")
@require_auth
def get_client_invoices(client_id):
    today = date.today().isoformat()
    db = get_db()
    rows = db.execute(
        "SELECT id, invoice_number, issue_date, due_date, total, amount_paid, status "
        "FROM invoices WHERE client_id=? ORDER BY issue_date DESC",
        (client_id,),
    ).fetchall()
    if not rows:
        return jsonify({"result": "No invoices found for this client."})
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
    return jsonify({"result": "\n".join(lines)})


# ═════════════════════════════════════════════════════════════════════════════
# CLIENTS — WRITE
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/clients", methods=["POST"])
@require_auth
def create_client():
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Client name is required."}), 400
    opening_balance_amt = _f(data.get("opening_balance_amt", 0))
    opening_balance_type = data.get("opening_balance_type", "debt")
    ob = abs(opening_balance_amt) if opening_balance_type != "credit" else -abs(opening_balance_amt)
    db = get_db()
    cur = db.execute(
        "INSERT INTO clients (name, email, phone, address, city, country, tax_id, notes, opening_balance) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (name,
         data.get("email") or None, data.get("phone") or None,
         data.get("address") or None, data.get("city") or None, data.get("country") or None,
         data.get("tax_id") or None, data.get("notes") or None, ob),
    )
    client_id = cur.lastrowid
    companies = data.get("companies") or []
    co_names = []
    for co in companies:
        co_name = (co.get("name") or "").strip()
        if not co_name:
            continue
        co_ob_amt = _f(co.get("opening_balance_amt", 0))
        co_ob_type = co.get("opening_balance_type", "debt")
        co_ob = abs(co_ob_amt) if co_ob_type != "credit" else -abs(co_ob_amt)
        db.execute(
            "INSERT INTO client_companies (client_id, name, tax_id, opening_balance) VALUES (?,?,?,?)",
            (client_id, co_name, co.get("tax_id") or None, co_ob),
        )
        co_names.append(co_name)
    db.commit()
    msg = f"✓ Client '{name}' created (ID: {client_id})."
    if ob != 0:
        msg += f" Opening balance: {_inr(abs(ob))} ({'debt' if ob > 0 else 'credit'})."
    if co_names:
        msg += f" Companies: {', '.join(co_names)}."
    return jsonify({"result": msg})


@bp.route("/clients/<int:client_id>", methods=["PUT"])
@require_auth
def update_client(client_id):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Client name is required."}), 400
    db = get_db()
    existing = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not existing:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404
    opening_balance_amt  = data.get("opening_balance_amt")
    opening_balance_type = data.get("opening_balance_type")
    old_ob = _f(existing["opening_balance"])
    if opening_balance_amt is not None:
        ob_type = opening_balance_type or ("credit" if old_ob < 0 else "debt")
        ob = abs(float(opening_balance_amt)) if ob_type != "credit" else -abs(float(opening_balance_amt))
    else:
        ob = old_ob
    db.execute(
        """UPDATE clients SET name=?, email=?, phone=?, address=?,
           city=?, country=?, tax_id=?, notes=?, opening_balance=?,
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (name,
         data.get("email") or None, data.get("phone") or None,
         data.get("address") or None, data.get("city") or None, data.get("country") or None,
         data.get("tax_id") or None, data.get("notes") or None, ob, client_id),
    )
    # Update companies if provided
    companies = data.get("companies")
    co_msgs = []
    if companies is not None:
        for co in companies:
            co_name = (co.get("name") or "").strip()
            if not co_name:
                continue
            co_ob_amt = _f(co.get("opening_balance_amt", 0))
            co_ob_type = co.get("opening_balance_type", "debt")
            co_ob = abs(co_ob_amt) if co_ob_type != "credit" else -abs(co_ob_amt)
            co_id = co.get("id")
            if co_id:
                db.execute(
                    "UPDATE client_companies SET name=?, tax_id=?, opening_balance=? WHERE id=? AND client_id=?",
                    (co_name, co.get("tax_id") or None, co_ob, int(co_id), client_id),
                )
                co_msgs.append(f"updated '{co_name}'")
            else:
                db.execute(
                    "INSERT INTO client_companies (client_id, name, tax_id, opening_balance) VALUES (?,?,?,?)",
                    (client_id, co_name, co.get("tax_id") or None, co_ob),
                )
                co_msgs.append(f"added '{co_name}'")
    db.commit()
    msg = f"✓ Client ID {client_id} ('{name}') updated."
    if co_msgs:
        msg += f" Companies: {', '.join(co_msgs)}."
    return jsonify({"result": msg})


@bp.route("/clients/<int:client_id>", methods=["DELETE"])
@require_auth
def delete_client(client_id):
    db = get_db()
    c = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404
    name = c["name"]
    db.execute("DELETE FROM payments WHERE client_id=?", (client_id,))
    db.execute(
        "DELETE FROM invoice_items WHERE invoice_id IN (SELECT id FROM invoices WHERE client_id=?)",
        (client_id,),
    )
    db.execute("DELETE FROM invoices WHERE client_id=?", (client_id,))
    db.execute("DELETE FROM client_companies WHERE client_id=?", (client_id,))
    db.execute("DELETE FROM clients WHERE id=?", (client_id,))
    db.commit()
    return jsonify({"result": f"✓ Client '{name}' (ID: {client_id}) permanently deleted along with all their invoices and payments."})


# ═════════════════════════════════════════════════════════════════════════════
# CLIENT COMPANIES
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/clients/<int:client_id>/companies")
@require_auth
def get_client_companies(client_id):
    db = get_db()
    c = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404
    companies = db.execute(
        "SELECT id, name, tax_id, opening_balance FROM client_companies WHERE client_id=? ORDER BY name",
        (client_id,),
    ).fetchall()
    if not companies:
        return jsonify({"result": f"No companies on record for {c['name']}."})
    lines = [f"Companies for {c['name']}:"]
    for co in companies:
        co_ob = _f(co["opening_balance"])
        co_inv_bal = sum(
            _f(r["total"]) - _f(r["amount_paid"])
            for r in db.execute(
                "SELECT total, amount_paid FROM invoices WHERE client_id=? AND company_id=? AND status!='cancelled'",
                (client_id, co["id"]),
            ).fetchall()
        )
        co_paid = _f(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND company_id=?",
            (client_id, co["id"]),
        ).fetchone()[0])
        co_balance = (co_paid + max(0.0, -co_ob)) - (co_inv_bal + max(0.0, co_ob))
        bal_label = (f"{_inr(abs(co_balance))} owes us" if co_balance < 0
                     else (f"{_inr(co_balance)} credit" if co_balance > 0 else "settled"))
        tax_str = f" | GST: {co['tax_id']}" if co["tax_id"] else ""
        lines.append(f"  ID {co['id']}: {co['name']}{tax_str} — {bal_label}")
    return jsonify({"result": "\n".join(lines)})


@bp.route("/clients/<int:client_id>/companies", methods=["POST"])
@require_auth
def create_company(client_id):
    db = get_db()
    if not db.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone():
        return jsonify({"error": f"Client ID {client_id} not found."}), 404
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Company name is required."}), 400
    ob_amt = _f(data.get("opening_balance_amt", 0))
    ob_type = data.get("opening_balance_type", "debt")
    ob = abs(ob_amt) if ob_type != "credit" else -abs(ob_amt)
    cur = db.execute(
        "INSERT INTO client_companies (client_id, name, tax_id, opening_balance) VALUES (?,?,?,?)",
        (client_id, name, data.get("tax_id") or None, ob),
    )
    db.commit()
    msg = f"✓ Company '{name}' added to client ID {client_id} (company ID: {cur.lastrowid})."
    if ob != 0:
        msg += f" Opening balance: {_inr(abs(ob))} ({'debt' if ob > 0 else 'credit'})."
    return jsonify({"result": msg})


@bp.route("/clients/<int:client_id>/companies/<int:company_id>", methods=["PUT"])
@require_auth
def update_company(client_id, company_id):
    db = get_db()
    co = db.execute(
        "SELECT * FROM client_companies WHERE id=? AND client_id=?", (company_id, client_id)
    ).fetchone()
    if not co:
        return jsonify({"error": f"Company ID {company_id} not found for client {client_id}."}), 404
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Company name is required."}), 400
    ob_amt = data.get("opening_balance_amt")
    ob_type = data.get("opening_balance_type")
    old_ob = _f(co["opening_balance"])
    if ob_amt is not None:
        typ = ob_type or ("credit" if old_ob < 0 else "debt")
        ob = abs(float(ob_amt)) if typ != "credit" else -abs(float(ob_amt))
    else:
        ob = old_ob
    db.execute(
        "UPDATE client_companies SET name=?, tax_id=?, opening_balance=? WHERE id=?",
        (name, data.get("tax_id") or None, ob, company_id),
    )
    db.commit()
    return jsonify({"result": f"✓ Company ID {company_id} ('{name}') updated."})


@bp.route("/clients/<int:client_id>/companies/<int:company_id>", methods=["DELETE"])
@require_auth
def delete_company(client_id, company_id):
    db = get_db()
    co = db.execute(
        "SELECT name FROM client_companies WHERE id=? AND client_id=?", (company_id, client_id)
    ).fetchone()
    if not co:
        return jsonify({"error": f"Company ID {company_id} not found for client {client_id}."}), 404
    db.execute("DELETE FROM client_companies WHERE id=?", (company_id,))
    db.commit()
    return jsonify({"result": f"✓ Company '{co['name']}' (ID: {company_id}) deleted."})


# ═════════════════════════════════════════════════════════════════════════════
# INVOICES — READ
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/invoices/recent")
@require_auth
def get_recent_invoices():
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
    except (ValueError, TypeError):
        limit = 10
    db = get_db()
    rows = db.execute(
        """SELECT i.invoice_number, i.issue_date, i.total, i.amount_paid, i.status, c.name
           FROM invoices i JOIN clients c ON c.id=i.client_id
           ORDER BY i.issue_date DESC, i.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return jsonify({"result": "No invoices found."})
    text = "\n".join(
        f"{r['invoice_number']} | {r['name']} | {r['issue_date']} | {_inr(r['total'])} | {r['status']}"
        for r in rows
    )
    return jsonify({"result": text})


@bp.route("/invoices/overdue")
@require_auth
def get_overdue_invoices():
    today = date.today().isoformat()
    db = get_db()
    rows = db.execute(
        """SELECT i.id, i.invoice_number, i.issue_date, i.due_date,
                  i.total, i.amount_paid, c.name AS client_name
           FROM invoices i JOIN clients c ON c.id=i.client_id
           WHERE i.due_date < ? AND i.status NOT IN ('paid','cancelled')
           ORDER BY i.due_date""",
        (today,),
    ).fetchall()
    if not rows:
        return jsonify({"result": "No overdue invoices — all up to date!"})
    lines = []
    for r in rows:
        days = (date.today() - date.fromisoformat(r["due_date"])).days
        remaining = _f(r["total"]) - _f(r["amount_paid"])
        lines.append(
            f"{r['invoice_number']} | {r['client_name']} | "
            f"due {r['due_date']} ({days}d ago) | {_inr(remaining)} remaining"
        )
    return jsonify({"result": f"{len(lines)} overdue invoice(s):\n" + "\n".join(lines)})


@bp.route("/invoices/<invoice_number>")
@require_auth
def get_invoice_details(invoice_number):
    db = get_db()
    inv = db.execute(
        "SELECT i.*, c.name AS client_name FROM invoices i "
        "JOIN clients c ON c.id=i.client_id WHERE i.invoice_number=?",
        (invoice_number.upper(),),
    ).fetchone()
    if not inv:
        return jsonify({"error": f"Invoice {invoice_number} not found."}), 404
    items = db.execute(
        "SELECT description, quantity, unit_price, tax_rate, line_total "
        "FROM invoice_items WHERE invoice_id=?",
        (inv["id"],),
    ).fetchall()
    pmts = db.execute(
        "SELECT pa.amount, p.payment_date, p.method, p.reference "
        "FROM payment_allocations pa JOIN payments p ON p.id=pa.payment_id "
        "WHERE pa.invoice_id=? ORDER BY p.payment_date",
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
        lines.append(
            f"  {it['description']} × {_f(it['quantity']):.2g} @ {_inr(it['unit_price'])}"
            + (f" ({_f(it['tax_rate']):.0f}% tax)" if _f(it["tax_rate"]) else "")
            + f" = {_inr(it['line_total'])}"
        )
    if pmts:
        lines.append("\nPayments received:")
        for p in pmts:
            lines.append(
                f"  {p['payment_date']} — {_inr(p['amount'])} via {p['method']}"
                + (f" (ref: {p['reference']})" if p["reference"] else "")
            )
    if inv["notes"]:
        lines.append(f"\nNotes: {inv['notes']}")
    return jsonify({"result": "\n".join(lines)})


# ═════════════════════════════════════════════════════════════════════════════
# INVOICES — WRITE
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/invoices", methods=["POST"])
@require_auth
def create_invoice():
    data = _jb()
    client_id = data.get("client_id")
    items = data.get("items", [])
    if not client_id:
        return jsonify({"error": "client_id is required."}), 400
    if not items:
        return jsonify({"error": "At least one line item is required."}), 400
    issue_date = data.get("issue_date") or date.today().isoformat()
    due_date = data.get("due_date") or ""
    try:
        datetime.strptime(issue_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "issue_date must be YYYY-MM-DD."}), 400
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "due_date must be YYYY-MM-DD."}), 400
    discount_amount = abs(_f(data.get("discount_amount", 0)))
    status = data.get("status", "issued")
    notes = data.get("notes") or ""

    db = get_db()
    client = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404

    subtotal = sum(_f(it.get("unit_price")) * _f(it.get("quantity", 1)) for it in items)
    tax_total = sum(
        _f(it.get("unit_price")) * _f(it.get("quantity", 1)) * _f(it.get("tax_rate", 0)) / 100
        for it in items
    )
    total = subtotal + tax_total - discount_amount

    company_id = data.get("company_id") or None
    is_draft = status == "draft"
    invoice_number = _next_invoice_number(db)
    cur = db.execute(
        """INSERT INTO invoices (invoice_number, client_id, company_id, status, issue_date, due_date,
           notes, subtotal, tax_total, discount_amount, total)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (invoice_number, client_id, company_id, status, issue_date,
         due_date or None, notes or None,
         subtotal, tax_total, discount_amount, total),
    )
    invoice_id = cur.lastrowid

    for it in items:
        pid = int(it["product_id"]) if it.get("product_id") else None
        spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
        qty = _f(it.get("quantity", 1))
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
        # Draft invoices don't deduct stock
        if not is_draft and (pid or spid):
            tbl = "sub_products" if spid else "products"
            pk = spid if spid else pid
            db.execute(f"UPDATE {tbl} SET stock_qty=stock_qty-? WHERE id=?", (qty, pk))
            db.execute(
                "INSERT INTO stock_movements "
                "(product_id, sub_product_id, movement_type, quantity, notes, invoice_id) "
                "VALUES (?,?,?,?,?,?)",
                (pid, spid, "sale", qty, f"Invoice {invoice_number}", invoice_id),
            )
    db.commit()
    if not is_draft:
        _apply_client_credit(db, client_id, invoice_id)
        db.commit()

    return jsonify({"result": (
        f"✓ Invoice {invoice_number} (ID: {invoice_id}) created for {client['name']}.\n"
        f"  Subtotal: {_inr(subtotal)} | Tax: {_inr(tax_total)} | Discount: {_inr(discount_amount)} | Total: {_inr(total)}"
    )})


@bp.route("/invoices/<int:invoice_id>/status", methods=["PUT"])
@require_auth
def update_invoice_status(invoice_id):
    data = _jb()
    status = data.get("status", "")
    valid = {"draft", "issued", "sent", "partial", "paid", "cancelled"}
    if status not in valid:
        return jsonify({"error": f"Invalid status. Must be one of: {', '.join(sorted(valid))}"}), 400
    db = get_db()
    inv = db.execute(
        "SELECT invoice_number, status, client_id, company_id, issue_date FROM invoices WHERE id=?",
        (invoice_id,),
    ).fetchone()
    if not inv:
        return jsonify({"error": f"Invoice ID {invoice_id} not found."}), 404
    prev = inv["status"]

    # Draft → Issued: check warehouse stock first
    if prev == "draft" and status == "issued":
        items = db.execute(
            """SELECT ii.description, ii.quantity, ii.product_id, ii.sub_product_id,
                      p.name AS product_name, sp.name AS sub_name
               FROM invoice_items ii
               LEFT JOIN products p ON ii.product_id = p.id
               LEFT JOIN sub_products sp ON ii.sub_product_id = sp.id
               WHERE ii.invoice_id = ?""",
            (invoice_id,),
        ).fetchall()
        short = []
        for it in items:
            pid = it["product_id"]; spid = it["sub_product_id"]
            if not pid and not spid:
                continue
            tbl = "sub_products" if spid else "products"
            pk = spid if spid else pid
            row = db.execute(f"SELECT stock_qty FROM {tbl} WHERE id=?", (pk,)).fetchone()
            available = _f(row["stock_qty"] or 0) if row else 0.0
            required = _f(it["quantity"])
            name = it["sub_name"] or it["product_name"] or it["description"]
            if available < required:
                short.append(f"  '{name}': need {required:.0f}, have {available:.0f}")
        if short:
            return jsonify({"error": "Cannot issue — insufficient warehouse stock:\n" + "\n".join(short)}), 400
        # Apply stock deductions
        for it in items:
            pid = it["product_id"]; spid = it["sub_product_id"]
            if not pid and not spid:
                continue
            tbl = "sub_products" if spid else "products"
            pk = spid if spid else pid
            qty = _f(it["quantity"])
            db.execute(f"UPDATE {tbl} SET stock_qty=stock_qty-? WHERE id=?", (qty, pk))
            db.execute(
                "INSERT INTO stock_movements "
                "(product_id, sub_product_id, movement_type, quantity, notes, invoice_id) "
                "VALUES (?,?,?,?,?,?)",
                (it["product_id"], it["sub_product_id"], "sale", qty,
                 f"Invoice {inv['invoice_number']}", invoice_id),
            )
        db.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
        db.commit()
        _apply_client_credit(db, inv["client_id"], invoice_id)
        db.commit()
        return jsonify({"result": f"✓ Invoice {inv['invoice_number']} issued. Stock deducted and ledger updated."})

    db.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
    # Restore stock when cancelling a previously-issued invoice (draft never held stock)
    if status == "cancelled" and prev not in ("cancelled", "draft"):
        rows = db.execute(
            "SELECT product_id, sub_product_id, quantity FROM invoice_items WHERE invoice_id=?",
            (invoice_id,),
        ).fetchall()
        for r in rows:
            if r["product_id"] or r["sub_product_id"]:
                tbl = "sub_products" if r["sub_product_id"] else "products"
                pk = r["sub_product_id"] if r["sub_product_id"] else r["product_id"]
                db.execute(f"UPDATE {tbl} SET stock_qty=stock_qty+? WHERE id=?", (r["quantity"], pk))
                db.execute(
                    "INSERT INTO stock_movements "
                    "(product_id, sub_product_id, movement_type, quantity, notes, invoice_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (r["product_id"], r["sub_product_id"], "sale_cancelled", r["quantity"],
                     f"Cancelled: {inv['invoice_number']}", invoice_id),
                )
    db.commit()
    return jsonify({"result": f"✓ Invoice {inv['invoice_number']} status changed: {prev} → {status}."})


@bp.route("/invoices/<int:invoice_id>", methods=["DELETE"])
@require_auth
def delete_invoice(invoice_id):
    db = get_db()
    inv = db.execute(
        "SELECT i.invoice_number, c.name AS client_name "
        "FROM invoices i JOIN clients c ON c.id=i.client_id WHERE i.id=?",
        (invoice_id,),
    ).fetchone()
    if not inv:
        return jsonify({"error": f"Invoice ID {invoice_id} not found."}), 404
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    db.execute("DELETE FROM payment_allocations WHERE invoice_id=?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    db.commit()
    return jsonify({"result": f"✓ Invoice {inv['invoice_number']} (for {inv['client_name']}) permanently deleted."})


# ═════════════════════════════════════════════════════════════════════════════
# PAYMENTS
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/payments/recent")
@require_auth
def get_recent_payments():
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
    except (ValueError, TypeError):
        limit = 10
    db = get_db()
    rows = db.execute(
        """SELECT p.id, p.amount, p.payment_date, p.method, p.reference,
                  c.name AS client_name,
                  (SELECT GROUP_CONCAT(i.invoice_number, ', ')
                     FROM payment_allocations pa JOIN invoices i ON i.id=pa.invoice_id
                    WHERE pa.payment_id=p.id) AS invoice_number
           FROM payments p
           JOIN clients c ON c.id=p.client_id
           ORDER BY p.payment_date DESC, p.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return jsonify({"result": "No payments found."})
    text = "\n".join(
        f"ID {r['id']} | {r['payment_date']} | {r['client_name']} | {_inr(r['amount'])} | "
        f"{r['method']}" + (f" | {r['invoice_number']}" if r["invoice_number"] else " | unallocated") +
        (f" | ref: {r['reference']}" if r["reference"] else "")
        for r in rows
    )
    return jsonify({"result": text})


@bp.route("/payments", methods=["POST"])
@require_auth
def record_payment():
    data = _jb()
    client_id = data.get("client_id")
    amount = _f(data.get("amount"))
    payment_date = data.get("payment_date", "")
    method = data.get("method", "")
    invoice_id = data.get("invoice_id")
    company_id = data.get("company_id") or None
    reference = data.get("reference") or ""
    notes = data.get("notes") or ""

    valid_methods = {"cash", "bank_transfer", "cheque", "upi", "other"}
    if method not in valid_methods:
        return jsonify({"error": f"Invalid method. Must be one of: {', '.join(sorted(valid_methods))}"}), 400
    try:
        datetime.strptime(payment_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date — use YYYY-MM-DD."}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive."}), 400

    db = get_db()
    client = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404

    from ..services import payment_service
    payment_service.create_payment({
        "client_id":    client_id,
        "company_id":   company_id,
        "invoice_id":   invoice_id,
        "amount":       amount,
        "payment_date": payment_date,
        "method":       method,
        "reference":    reference or None,
        "notes":        notes or "Recorded via Claude",
    })

    # Build a human-readable allocation summary from what was just stored.
    last_payment_id = db.execute(
        "SELECT id FROM payments WHERE client_id=? ORDER BY id DESC LIMIT 1",
        (client_id,),
    ).fetchone()["id"]
    alloc_rows = db.execute(
        """SELECT pa.amount, i.invoice_number
           FROM payment_allocations pa JOIN invoices i ON i.id=pa.invoice_id
           WHERE pa.payment_id=?""",
        (last_payment_id,),
    ).fetchall()
    allocated_sum = sum(_f(r["amount"]) for r in alloc_rows)
    leftover = amount - allocated_sum

    lines = [f"✓ {_inr(amount)} payment recorded for {client['name']} on {payment_date}.",
             "Allocation:"]
    for r in alloc_rows:
        lines.append(f"  {_inr(_f(r['amount']))} → {r['invoice_number']}")
    if leftover > 0.001:
        lines.append(f"  {_inr(leftover)} → opening balance / credit")
    return jsonify({"result": "\n".join(lines)})


@bp.route("/payments/<int:payment_id>", methods=["DELETE"])
@require_auth
def delete_payment(payment_id):
    db = get_db()
    p = db.execute(
        "SELECT p.amount, p.payment_date, c.name AS client_name "
        "FROM payments p JOIN clients c ON c.id=p.client_id WHERE p.id=?",
        (payment_id,),
    ).fetchone()
    if not p:
        return jsonify({"error": f"Payment ID {payment_id} not found."}), 404
    from ..services import payment_service
    payment_service.delete_payment(payment_id)
    return jsonify({"result": (
        f"✓ Payment ID {payment_id} ({_inr(p['amount'])} from {p['client_name']} "
        f"on {p['payment_date']}) permanently deleted."
    )})


@bp.route("/payments/ledger-entry", methods=["POST"])
@require_auth
def add_ledger_entry():
    data = _jb()
    client_id = data.get("client_id")
    entry_date = data.get("entry_date", "")
    description = data.get("description", "")
    debit = _f(data.get("debit", 0))
    credit = _f(data.get("credit", 0))
    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date — use YYYY-MM-DD."}), 400
    if debit == 0 and credit == 0:
        return jsonify({"error": "Enter a debit or credit amount."}), 400
    db = get_db()
    c = db.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404
    db.execute(
        "INSERT INTO ledger_entries (client_id, entry_date, description, debit, credit) VALUES (?,?,?,?,?)",
        (client_id, entry_date, description, debit, credit),
    )
    db.commit()
    direction = f"debit {_inr(debit)}" if debit else f"credit {_inr(credit)}"
    return jsonify({"result": f"✓ Manual ledger entry added for {c['name']}: {direction} on {entry_date} — '{description}'"})


@bp.route("/clients/reconcile", methods=["POST"])
@require_auth
def reconcile_clients():
    """Apply unallocated payments to the oldest unpaid invoices.

    Body: {"client_id": <int>} reconciles a single client; {} reconciles ALL clients.
    Rewrites payment allocations + invoice paid-status only — never creates, edits,
    or deletes a payment or invoice. Idempotent.
    """
    data = _jb()
    db = get_db()
    from ..services import payment_service

    cid = data.get("client_id")
    if cid is not None:
        client = db.execute("SELECT name FROM clients WHERE id=?", (cid,)).fetchone()
        if not client:
            return jsonify({"error": f"Client ID {cid} not found."}), 404
        before = {r["id"] for r in db.execute(
            "SELECT id FROM invoices WHERE client_id=? AND status='paid'", (cid,)).fetchall()}
        payment_service.recalculate_client_balance(int(cid))
        after = {r["id"] for r in db.execute(
            "SELECT id FROM invoices WHERE client_id=? AND status='paid'", (cid,)).fetchall()}
        return jsonify({"result": (
            f"✓ Reconciled {client['name']}. {len(after - before)} invoice(s) newly marked paid; "
            f"{len(after)} of their invoice(s) now fully paid."
        )})

    summary = payment_service.reconcile_all_clients()
    return jsonify({"result": (
        f"✓ Reconciled {summary['clients_processed']} client(s). "
        f"{summary['invoices_newly_paid']} invoice(s) newly marked paid; "
        f"{summary['total_paid_invoices']} invoice(s) now fully paid in total."
    )})


# ═════════════════════════════════════════════════════════════════════════════
# STATS
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/stats")
@require_auth
def get_business_stats():
    today = date.today().isoformat()
    db = get_db()
    total_revenue = _f(db.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0])
    total_invoiced_all = _f(db.execute(
        "SELECT COALESCE(SUM(total),0) FROM invoices WHERE status!='cancelled'"
    ).fetchone()[0])
    outstanding = _f(db.execute(
        "SELECT COALESCE(SUM(total-amount_paid),0) FROM invoices WHERE status NOT IN ('paid','cancelled')"
    ).fetchone()[0])
    client_count = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    overdue_count = db.execute(
        "SELECT COUNT(*) FROM invoices WHERE due_date<? AND status NOT IN ('paid','cancelled')", (today,)
    ).fetchone()[0]
    overdue_amt = _f(db.execute(
        "SELECT COALESCE(SUM(total-amount_paid),0) FROM invoices WHERE due_date<? AND status NOT IN ('paid','cancelled')",
        (today,),
    ).fetchone()[0])
    recent_pmts = _f(db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE payment_date>=date('now','-30 days')"
    ).fetchone()[0])
    recent_pmt_count = db.execute(
        "SELECT COUNT(*) FROM payments WHERE payment_date>=date('now','-30 days')"
    ).fetchone()[0]
    recent_pmt_clients = db.execute(
        "SELECT COUNT(DISTINCT client_id) FROM payments WHERE payment_date>=date('now','-30 days')"
    ).fetchone()[0]
    recent_inv = _f(db.execute(
        "SELECT COALESCE(SUM(total),0) FROM invoices WHERE issue_date>=date('now','-30 days') AND status!='cancelled'"
    ).fetchone()[0])
    recent_inv_count = db.execute(
        "SELECT COUNT(*) FROM invoices WHERE issue_date>=date('now','-30 days') AND status!='cancelled'"
    ).fetchone()[0]
    recent_tax = _f(db.execute(
        "SELECT COALESCE(SUM(tax_total),0) FROM invoices WHERE issue_date>=date('now','-30 days') AND status!='cancelled'"
    ).fetchone()[0])
    inv_count = db.execute("SELECT COUNT(*) FROM invoices WHERE status!='cancelled'").fetchone()[0]
    product_count = db.execute("SELECT COUNT(*) FROM products WHERE is_active=1").fetchone()[0]
    supplier_count = db.execute("SELECT COUNT(*) FROM suppliers WHERE is_active=1").fetchone()[0]
    in_transit_count = db.execute(
        "SELECT COUNT(*) FROM dispatches WHERE status IN ('in_transit','partially_received')"
    ).fetchone()[0]
    text = "\n".join([
        f"─── All-time ───────────────────────",
        f"Total sales (invoiced)  : {_inr(total_invoiced_all)}",
        f"Total revenue collected : {_inr(total_revenue)}",
        f"Outstanding (unpaid)    : {_inr(outstanding)}",
        f"Overdue                 : {overdue_count} invoice(s) totalling {_inr(overdue_amt)}",
        f"Clients                 : {client_count}",
        f"Invoices (active)       : {inv_count}",
        f"Active products         : {product_count}",
        f"Active suppliers        : {supplier_count}",
        f"In-transit dispatches   : {in_transit_count}",
        f"─── Last 30 days ───────────────────",
        f"Sales (invoiced)        : {_inr(recent_inv)} across {recent_inv_count} invoice(s) · GST {_inr(recent_tax)}",
        f"Revenue collected       : {_inr(recent_pmts)} · {recent_pmt_count} payment(s) from {recent_pmt_clients} client(s)",
    ])
    return jsonify({"result": text})


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORIES
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/categories")
@require_auth
def list_categories():
    db = get_db()
    rows = db.execute("SELECT id, name, description FROM categories ORDER BY name").fetchall()
    if not rows:
        return jsonify({"result": "No categories found."})
    text = "\n".join(
        f"ID {r['id']}: {r['name']}" + (f" — {r['description']}" if r["description"] else "")
        for r in rows
    )
    return jsonify({"result": text})


@bp.route("/categories", methods=["POST"])
@require_auth
def create_category():
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Category name is required."}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO categories (name, description) VALUES (?,?)",
        (name, data.get("description") or None),
    )
    db.commit()
    return jsonify({"result": f"✓ Category '{name}' created (ID: {cur.lastrowid})."})


@bp.route("/categories/<int:cat_id>", methods=["PUT"])
@require_auth
def update_category(cat_id):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Category name is required."}), 400
    db = get_db()
    cat = db.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not cat:
        return jsonify({"error": f"Category ID {cat_id} not found."}), 404
    db.execute("UPDATE categories SET name=?, description=? WHERE id=?",
               (name, data.get("description") or None, cat_id))
    db.commit()
    return jsonify({"result": f"✓ Category ID {cat_id} updated to '{name}'."})


@bp.route("/categories/<int:cat_id>", methods=["DELETE"])
@require_auth
def delete_category(cat_id):
    db = get_db()
    cat = db.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not cat:
        return jsonify({"error": f"Category ID {cat_id} not found."}), 404
    db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    db.commit()
    return jsonify({"result": f"✓ Category '{cat['name']}' (ID: {cat_id}) permanently deleted."})


# ═════════════════════════════════════════════════════════════════════════════
# PRODUCTS
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/products/search")
@require_auth
def search_products():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    q = f"%{query.lower()}%"
    db = get_db()
    products = db.execute(
        "SELECT id, name, sku, unit_price, stock_qty, pcs_per_carton FROM products "
        "WHERE is_active=1 AND (LOWER(name) LIKE ? OR LOWER(COALESCE(sku,'')) LIKE ?) "
        "ORDER BY name LIMIT 10",
        (q, q),
    ).fetchall()
    subs = db.execute(
        """SELECT s.id, s.name, s.sku, s.unit_price, s.stock_qty,
                  p.name AS parent, p.id AS parent_id, p.pcs_per_carton
           FROM sub_products s JOIN products p ON p.id=s.product_id
           WHERE s.is_active=1 AND (LOWER(s.name) LIKE ? OR LOWER(COALESCE(s.sku,'')) LIKE ?)
           ORDER BY p.name, s.name LIMIT 10""",
        (q, q),
    ).fetchall()
    lines = []
    for p in products:
        lines.append(
            f"Product ID {p['id']}: {p['name']}" +
            (f" [{p['sku']}]" if p["sku"] else "") +
            f" | {_inr(p['unit_price'])} | stock: {_f(p['stock_qty']):.0f}" +
            (f" | pcs/ctn: {p['pcs_per_carton']}" if p["pcs_per_carton"] else "")
        )
    for s in subs:
        lines.append(
            f"Sub-product ID {s['id']}: {s['parent']} (ID:{s['parent_id']}) — {s['name']}" +
            (f" [{s['sku']}]" if s["sku"] else "") +
            f" | {_inr(s['unit_price'])} | stock: {_f(s['stock_qty']):.0f}" +
            (f" | pcs/ctn: {s['pcs_per_carton']}" if s["pcs_per_carton"] else "")
        )
    return jsonify({"result": "\n".join(lines) if lines else "No products found matching that query."})


@bp.route("/products/stock")
@require_auth
def get_stock_summary():
    db = get_db()
    products = db.execute(
        "SELECT id, name, sku, stock_qty, production_qty, in_transit_qty, min_quantity, "
        "has_eco_range, eco_parent_id, pcs_per_carton "
        "FROM products WHERE is_active=1 ORDER BY name"
    ).fetchall()
    subs = db.execute(
        """SELECT p.id AS parent_id, p.name AS parent, p.eco_parent_id AS parent_eco_parent,
                  p.pcs_per_carton,
                  s.id, s.name, s.sku, s.eco_parent_sub_id,
                  s.stock_qty, s.production_qty, s.in_transit_qty, s.min_quantity
           FROM sub_products s JOIN products p ON p.id=s.product_id
           WHERE s.is_active=1 ORDER BY p.name, s.name"""
    ).fetchall()

    # Aggregate sub-product totals per parent
    sub_agg = {}
    for s in subs:
        pid = s["parent_id"]
        if pid not in sub_agg:
            sub_agg[pid] = {"stock": 0.0, "prod": 0.0, "transit": 0.0, "min": 0.0}
        sub_agg[pid]["stock"]   += _f(s["stock_qty"])
        sub_agg[pid]["prod"]    += _f(s["production_qty"])
        sub_agg[pid]["transit"] += _f(s["in_transit_qty"])
        sub_agg[pid]["min"]     += _f(s["min_quantity"])

    # Build lookup: main product id → eco product row
    eco_by_main = {p["eco_parent_id"]: p for p in products if p["eco_parent_id"]}

    def _row(name, sku, stock, prod, transit, min_qty, pcs_per_carton=0):
        alert = " ⚠ LOW" if min_qty and _f(stock) < _f(min_qty) else ""
        pcs = f" | pcs/ctn: {pcs_per_carton}" if pcs_per_carton else ""
        return (f"{name}" + (f" [{sku}]" if sku else "") +
                f" | wh:{_f(stock):.0f} prod:{_f(prod):.0f} transit:{_f(transit):.0f}{pcs}{alert}")

    lines = ["=== Products ==="]
    for p in products:
        pid = p["id"]
        # Skip eco products — they are shown merged with their main product
        if p["eco_parent_id"]:
            continue

        eco = eco_by_main.get(pid)

        if pid in sub_agg:
            # Main product has sub-products
            agg = sub_agg[pid]
            if eco and eco["id"] in sub_agg:
                # Eco product also has sub-products — merge sub totals
                eco_agg = sub_agg[eco["id"]]
                combined_stock   = agg["stock"]   + eco_agg["stock"]
                combined_prod    = agg["prod"]     + eco_agg["prod"]
                combined_transit = agg["transit"]  + eco_agg["transit"]
                alert = " ⚠ LOW" if agg["min"] and combined_stock < agg["min"] else ""
                lines.append(
                    f"{p['name']} + Eco" + (f" [{p['sku']}]" if p["sku"] else "") +
                    f" | wh:{combined_stock:.0f}({agg['stock']:.0f}+{eco_agg['stock']:.0f})"
                    f" prod:{combined_prod:.0f} transit:{combined_transit:.0f}{alert}"
                )
            else:
                lines.append(_row(p["name"], p["sku"], agg["stock"], agg["prod"], agg["transit"], agg["min"], p["pcs_per_carton"]))
        else:
            if eco:
                # Standalone eco-paired product — merge stock
                combined_stock   = _f(p["stock_qty"])   + _f(eco["stock_qty"])
                combined_prod    = _f(p["production_qty"]) + _f(eco["production_qty"])
                combined_transit = _f(p["in_transit_qty"]) + _f(eco["in_transit_qty"])
                min_qty = _f(p["min_quantity"])
                alert = " ⚠ LOW" if min_qty and combined_stock < min_qty else ""
                lines.append(
                    f"{p['name']} + Eco" + (f" [{p['sku']}]" if p["sku"] else "") +
                    f" | wh:{combined_stock:.0f}({_f(p['stock_qty']):.0f}+{_f(eco['stock_qty']):.0f})"
                    f" prod:{combined_prod:.0f} transit:{combined_transit:.0f}{alert}"
                )
            else:
                lines.append(_row(p["name"], p["sku"], p["stock_qty"], p["production_qty"], p["in_transit_qty"], p["min_quantity"], p["pcs_per_carton"]))

    if subs:
        lines.append("\n=== Sub-products ===")
        # Build lookup: main sub id → eco sub row (for pairing)
        eco_subs_by_main = {s["eco_parent_sub_id"]: s for s in subs if s["eco_parent_sub_id"]}
        for s in subs:
            # Skip eco sub-products (shown merged with main sub)
            if s["eco_parent_sub_id"]:
                continue
            eco_s = eco_subs_by_main.get(s["id"])
            if eco_s:
                combined_stock   = _f(s["stock_qty"])     + _f(eco_s["stock_qty"])
                combined_prod    = _f(s["production_qty"]) + _f(eco_s["production_qty"])
                combined_transit = _f(s["in_transit_qty"]) + _f(eco_s["in_transit_qty"])
                min_qty = _f(s["min_quantity"])
                alert = " ⚠ LOW" if min_qty and combined_stock < min_qty else ""
                lines.append(
                    f"{s['parent']} — {s['name']} + Eco" + (f" [{s['sku']}]" if s["sku"] else "") +
                    f" | wh:{combined_stock:.0f}({_f(s['stock_qty']):.0f}+{_f(eco_s['stock_qty']):.0f})"
                    f" prod:{combined_prod:.0f} transit:{combined_transit:.0f}{alert}"
                )
            else:
                lines.append(_row(f"{s['parent']} — {s['name']}", s["sku"], s["stock_qty"], s["production_qty"], s["in_transit_qty"], s["min_quantity"], s["pcs_per_carton"]))
    return jsonify({"result": "\n".join(lines)})


@bp.route("/products/low-stock")
@require_auth
def get_low_stock_alerts():
    db = get_db()

    # ── Block A: Standalone products (no sub-products, no eco linkage) ──────
    low_p = db.execute(
        """SELECT name, sku, stock_qty, min_quantity FROM products
           WHERE is_active=1 AND min_quantity>0 AND stock_qty<min_quantity
             AND has_eco_range=0 AND eco_parent_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM sub_products sp WHERE sp.product_id=products.id AND sp.is_active=1)
           ORDER BY (stock_qty-min_quantity)"""
    ).fetchall()

    # ── Block B: Eco-paired products without sub-products ───────────────────
    # Check combined (main + eco) stock vs main's minimum
    eco_paired_low = db.execute(
        """SELECT p.name AS main_name, p.sku AS main_sku,
                  p.stock_qty AS main_stock, p.min_quantity AS min_qty,
                  eco.name AS eco_name, eco.sku AS eco_sku, eco.stock_qty AS eco_stock,
                  (p.stock_qty + eco.stock_qty) AS combined
           FROM products p
           JOIN products eco ON eco.eco_parent_id = p.id AND eco.is_active=1
           WHERE p.is_active=1 AND p.has_eco_range=1 AND p.min_quantity > 0
             AND (p.stock_qty + eco.stock_qty) < p.min_quantity
             AND NOT EXISTS (SELECT 1 FROM sub_products sp WHERE sp.product_id=p.id AND sp.is_active=1)
           ORDER BY (p.stock_qty + eco.stock_qty - p.min_quantity)"""
    ).fetchall()

    # ── Block C: Products WITH sub-products, no eco range ───────────────────
    low_p_agg = db.execute(
        """SELECT p.name, p.sku,
                  SUM(s.stock_qty)    AS total_stock,
                  SUM(s.min_quantity) AS total_min
           FROM products p
           JOIN sub_products s ON s.product_id=p.id AND s.is_active=1
           WHERE p.is_active=1 AND p.has_eco_range=0 AND p.eco_parent_id IS NULL
           GROUP BY p.id
           HAVING total_min > 0 AND total_stock < total_min
           ORDER BY (total_stock - total_min)"""
    ).fetchall()

    # ── Block D: Eco-paired products WITH sub-products (per-variant pair) ───
    # For each main_sub + eco_sub pair, compare combined stock vs main_sub.min_quantity
    eco_sub_pairs_low = db.execute(
        """SELECT ms.name AS main_sub_name, ms.sku AS main_sub_sku,
                  ms.stock_qty AS main_stock, ms.min_quantity AS eff_min,
                  es.name AS eco_sub_name, es.sku AS eco_sub_sku, es.stock_qty AS eco_stock,
                  p.name AS main_parent_name, ep.name AS eco_parent_name,
                  (ms.stock_qty + es.stock_qty) AS combined
           FROM sub_products ms
           JOIN sub_products es ON es.eco_parent_sub_id = ms.id AND es.is_active=1
           JOIN products p  ON p.id  = ms.product_id
           JOIN products ep ON ep.id = es.product_id
           WHERE ms.is_active=1 AND ms.min_quantity > 0
             AND (ms.stock_qty + es.stock_qty) < ms.min_quantity
           ORDER BY (ms.stock_qty + es.stock_qty - ms.min_quantity)"""
    ).fetchall()

    # ── Block E: Individual sub-products with no eco counterpart ────────────
    # Exclude: eco sub-products (eco_parent_sub_id IS NOT NULL)
    # Exclude: main sub-products that have an eco pair (handled in Block D)
    low_s = db.execute(
        """SELECT p.name AS parent, s.name, s.sku, s.stock_qty, s.min_quantity AS eff_min
           FROM sub_products s JOIN products p ON p.id=s.product_id
           WHERE s.is_active=1 AND s.min_quantity>0 AND s.stock_qty < s.min_quantity
             AND s.eco_parent_sub_id IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM sub_products es
                 WHERE es.eco_parent_sub_id = s.id AND es.is_active=1
             )
           ORDER BY s.stock_qty"""
    ).fetchall()

    if not low_p and not eco_paired_low and not low_p_agg and not eco_sub_pairs_low and not low_s:
        return jsonify({"result": "No low stock alerts — all products above minimum levels."})

    lines = []

    for p in low_p:
        shortage = _f(p["min_quantity"]) - _f(p["stock_qty"])
        lines.append(
            f"{p['name']}" + (f" [{p['sku']}]" if p["sku"] else "") +
            f" | stock: {_f(p['stock_qty']):.0f} / min: {_f(p['min_quantity']):.0f} | short by {shortage:.0f}"
        )

    for row in eco_paired_low:
        shortage = _f(row["min_qty"]) - _f(row["combined"])
        combined_str = f"{_f(row['combined']):.0f} ({_f(row['main_stock']):.0f} main + {_f(row['eco_stock']):.0f} eco)"
        lines.append(
            f"{row['main_name']}" + (f" [{row['main_sku']}]" if row["main_sku"] else "") +
            f" | combined: {combined_str} / min: {_f(row['min_qty']):.0f} | short by {shortage:.0f} [eco-paired]"
        )
        lines.append(
            f"{row['eco_name']}" + (f" [{row['eco_sku']}]" if row["eco_sku"] else "") +
            f" | combined: {combined_str} / min: {_f(row['min_qty']):.0f} | short by {shortage:.0f} [eco-paired]"
        )

    for p in low_p_agg:
        shortage = _f(p["total_min"]) - _f(p["total_stock"])
        lines.append(
            f"{p['name']}" + (f" [{p['sku']}]" if p["sku"] else "") +
            f" | stock: {_f(p['total_stock']):.0f} / min: {_f(p['total_min']):.0f} | short by {shortage:.0f} [aggregate]"
        )

    for row in eco_sub_pairs_low:
        shortage = _f(row["eff_min"]) - _f(row["combined"])
        combined_str = f"{_f(row['combined']):.0f} ({_f(row['main_stock']):.0f} main + {_f(row['eco_stock']):.0f} eco)"
        lines.append(
            f"{row['main_parent_name']} — {row['main_sub_name']}" + (f" [{row['main_sub_sku']}]" if row["main_sub_sku"] else "") +
            f" | combined: {combined_str} / min: {_f(row['eff_min']):.0f} | short by {shortage:.0f} [eco-paired]"
        )
        lines.append(
            f"{row['eco_parent_name']} — {row['eco_sub_name']}" + (f" [{row['eco_sub_sku']}]" if row["eco_sub_sku"] else "") +
            f" | combined: {combined_str} / min: {_f(row['eff_min']):.0f} | short by {shortage:.0f} [eco-paired]"
        )

    for s in low_s:
        shortage = _f(s["eff_min"]) - _f(s["stock_qty"])
        lines.append(
            f"{s['parent']} — {s['name']}" + (f" [{s['sku']}]" if s["sku"] else "") +
            f" | stock: {_f(s['stock_qty']):.0f} / min: {_f(s['eff_min']):.0f} | short by {shortage:.0f}"
        )

    return jsonify({"result": f"{len(lines)} low-stock alert(s):\n" + "\n".join(lines)})


@bp.route("/products", methods=["POST"])
@require_auth
def create_product():
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Product name is required."}), 400
    unit_price = _f(data.get("unit_price", 0))
    tax_rate = _f(data.get("tax_rate", 18.0))
    sku = data.get("sku") or None
    description = data.get("description") or None
    min_quantity = _f(data.get("min_quantity", 0))
    opening_stock = _f(data.get("opening_stock", 0))
    category_id = data.get("category_id") or None
    db = get_db()
    if category_id:
        cat = db.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if not cat:
            return jsonify({"error": f"Category ID {category_id} not found."}), 400
    pcs_per_carton = int(data.get("pcs_per_carton") or 0)
    cur = db.execute(
        """INSERT INTO products (category_id, name, sku, description, unit_price, tax_rate,
           track_inventory, stock_qty, min_quantity, pcs_per_carton, is_active)
           VALUES (?,?,?,?,?,?,1,?,?,?,1)""",
        (category_id, name, sku, description, unit_price, tax_rate, opening_stock, min_quantity, pcs_per_carton),
    )
    product_id = cur.lastrowid
    if opening_stock > 0:
        db.execute(
            "INSERT INTO stock_movements (product_id, movement_type, quantity, notes) VALUES (?,?,?,?)",
            (product_id, "opening", opening_stock, "Opening stock"),
        )
    db.commit()
    return jsonify({"result": f"✓ Product '{name}' created (ID: {product_id}) | Price: {_inr(unit_price)} | Tax: {tax_rate}% | Stock: {opening_stock:.0f}."})


@bp.route("/products/<int:product_id>", methods=["PUT"])
@require_auth
def update_product(product_id):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Product name is required."}), 400
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        return jsonify({"error": f"Product ID {product_id} not found."}), 404
    unit_price = data.get("unit_price")
    tax_rate = data.get("tax_rate")
    min_quantity = data.get("min_quantity")
    category_id = data.get("category_id")
    is_active = data.get("is_active", True)
    new_price = _f(unit_price) if unit_price is not None else _f(p["unit_price"])
    new_tax = _f(tax_rate) if tax_rate is not None else _f(p["tax_rate"])
    new_min = _f(min_quantity) if min_quantity is not None else _f(p["min_quantity"])
    new_cat = category_id if category_id is not None else p["category_id"]
    pcs_per_carton = data.get("pcs_per_carton")
    new_pcs = int(pcs_per_carton) if pcs_per_carton is not None else int(p["pcs_per_carton"] or 0)
    db.execute(
        """UPDATE products SET category_id=?, name=?, sku=?, description=?,
           unit_price=?, tax_rate=?, track_inventory=1, min_quantity=?, pcs_per_carton=?, is_active=? WHERE id=?""",
        (new_cat, name, data.get("sku") or None, data.get("description") or None,
         new_price, new_tax, new_min, new_pcs, 1 if is_active else 0, product_id),
    )
    db.execute("UPDATE sub_products SET tax_rate=? WHERE product_id=?", (new_tax, product_id))
    # Cascade min_quantity change to eco product (and its subs inherit via min=0)
    if min_quantity is not None and new_min != _f(p["min_quantity"]):
        eco = db.execute(
            "SELECT id FROM products WHERE eco_parent_id=?", (product_id,)
        ).fetchone()
        if eco:
            db.execute("UPDATE products SET min_quantity=? WHERE id=?", (new_min, eco["id"]))
    db.commit()
    return jsonify({"result": f"✓ Product ID {product_id} ('{name}') updated."})


@bp.route("/products/<int:product_id>", methods=["DELETE"])
@require_auth
def delete_product(product_id):
    db = get_db()
    p = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        return jsonify({"error": f"Product ID {product_id} not found."}), 404
    db.execute(
        "DELETE FROM stock_movements WHERE sub_product_id IN "
        "(SELECT id FROM sub_products WHERE product_id=?)", (product_id,)
    )
    db.execute("DELETE FROM stock_movements WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM sub_products WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM products WHERE id=?", (product_id,))
    db.commit()
    return jsonify({"result": f"✓ Product '{p['name']}' (ID: {product_id}) and all sub-products permanently deleted."})


@bp.route("/products/<int:product_id>/create-eco-range", methods=["POST"])
@require_auth
def create_eco_range(product_id):
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        return jsonify({"error": f"Product ID {product_id} not found."}), 404
    if p["has_eco_range"]:
        return jsonify({"error": f"Product '{p['name']}' already has an eco range."}), 400
    if p["eco_parent_id"]:
        return jsonify({"error": f"Product '{p['name']}' is itself an eco product and cannot have its own eco range."}), 400

    data = _jb()
    unit_price = _f(data.get("unit_price", 0))
    provided_sku = (data.get("sku") or "").strip() or None
    eco_sku = provided_sku or (("E-" + p["sku"]) if p["sku"] else None)

    # Create eco product (zero stock)
    eco_cur = db.execute(
        """INSERT INTO products (category_id, name, sku, description, unit_price, tax_rate,
           track_inventory, stock_qty, min_quantity, pcs_per_carton, is_active, eco_parent_id)
           VALUES (?,?,?,?,?,?,1,0,?,?,1,?)""",
        (p["category_id"], "Eco " + p["name"], eco_sku, p["description"],
         unit_price, p["tax_rate"], p["min_quantity"], p["pcs_per_carton"] or 0, product_id),
    )
    eco_id = eco_cur.lastrowid

    # Mark main product as having an eco range
    db.execute("UPDATE products SET has_eco_range=1 WHERE id=?", (product_id,))

    # Mirror sub-products
    main_subs = db.execute(
        "SELECT * FROM sub_products WHERE product_id=? AND is_active=1 ORDER BY id",
        (product_id,)
    ).fetchall()
    eco_sub_count = 0
    for ms in main_subs:
        eco_sub_sku = (("E-" + ms["sku"]) if ms["sku"] else None)
        db.execute(
            """INSERT INTO sub_products (product_id, name, sku, description, unit_price,
               use_parent_price, tax_rate, track_inventory, stock_qty, min_quantity,
               production_qty, in_transit_qty, is_active, eco_parent_sub_id)
               VALUES (?,?,?,?,0,0,?,1,0,?,0,0,1,?)""",
            (eco_id, "Eco " + ms["name"], eco_sub_sku, ms["description"],
             ms["tax_rate"], ms["min_quantity"] or 0, ms["id"]),
        )
        eco_sub_count += 1

    db.commit()
    msg = f"✓ Eco range created for '{p['name']}' → 'Eco {p['name']}' (ID: {eco_id})"
    if eco_sub_count:
        msg += f" with {eco_sub_count} eco sub-product(s). Set pricing via update_sub_product."
    msg += " Add stock via adjust_stock."
    return jsonify({"result": msg})


@bp.route("/products/<int:product_id>/adjust-stock", methods=["POST"])
@require_auth
def adjust_stock(product_id):
    data = _jb()
    bucket = data.get("bucket", "")
    direction = data.get("direction", "")
    quantity = _f(data.get("quantity", 0))
    notes = data.get("notes") or ""
    sub_product_id = data.get("sub_product_id") or None

    if bucket not in ("warehouse", "production", "dispatch"):
        return jsonify({"error": "bucket must be: warehouse | production | dispatch"}), 400
    if direction not in ("increase", "decrease"):
        return jsonify({"error": "direction must be: increase | decrease"}), 400
    if quantity <= 0:
        return jsonify({"error": "quantity must be positive."}), 400

    field_map = {"warehouse": "stock_qty", "production": "production_qty", "dispatch": "in_transit_qty"}
    field = field_map[bucket]
    delta = quantity if direction == "increase" else -quantity
    movement = f"{bucket}_{'add' if direction == 'increase' else 'deduct'}"

    db = get_db()
    if sub_product_id:
        row = db.execute("SELECT name FROM sub_products WHERE id=?", (sub_product_id,)).fetchone()
        if not row:
            return jsonify({"error": f"Sub-product ID {sub_product_id} not found."}), 404
        name = row["name"]
        db.execute(f"UPDATE sub_products SET {field}={field}+? WHERE id=?", (delta, sub_product_id))
    else:
        row = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return jsonify({"error": f"Product ID {product_id} not found."}), 404
        name = row["name"]
        db.execute(f"UPDATE products SET {field}={field}+? WHERE id=?", (delta, product_id))
    db.execute(
        "INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes) VALUES (?,?,?,?,?)",
        (product_id, sub_product_id or None, movement, quantity, notes or "Adjusted via Claude"),
    )
    db.commit()
    return jsonify({"result": f"✓ {name} {bucket} stock {direction}d by {quantity:.0f}. Notes: {notes or '—'}"})


@bp.route("/products/<int:product_id>/sub-products", methods=["POST"])
@require_auth
def create_sub_product(product_id):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Sub-product name is required."}), 400
    db = get_db()
    parent = db.execute("SELECT name, tax_rate, unit_price FROM products WHERE id=?", (product_id,)).fetchone()
    if not parent:
        return jsonify({"error": f"Product ID {product_id} not found."}), 404
    parent_tax = _f(parent["tax_rate"])
    use_parent_price = data.get("use_parent_price", True)
    use_pp = 1 if use_parent_price else 0
    unit_price = _f(data.get("unit_price", 0))
    min_quantity = _f(data.get("min_quantity", 0))
    opening_stock = _f(data.get("opening_stock", 0))
    cur = db.execute(
        """INSERT INTO sub_products
           (product_id, name, sku, description, use_parent_price, unit_price,
            tax_rate, track_inventory, stock_qty, min_quantity, is_active)
           VALUES (?,?,?,?,?,?,?,1,?,?,1)""",
        (product_id, name, data.get("sku") or None, data.get("description") or None,
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
    return jsonify({"result": (
        f"✓ Sub-product '{name}' created under '{parent['name']}' (sub-ID: {sub_id}). "
        f"Price: {_inr(effective_price)} | Stock: {opening_stock:.0f}."
    )})


@bp.route("/sub-products/<int:sub_id>", methods=["PUT"])
@require_auth
def update_sub_product(sub_id):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Sub-product name is required."}), 400
    db = get_db()
    s = db.execute(
        "SELECT s.*, p.name AS parent_name, p.tax_rate AS parent_tax "
        "FROM sub_products s JOIN products p ON p.id=s.product_id WHERE s.id=?",
        (sub_id,),
    ).fetchone()
    if not s:
        return jsonify({"error": f"Sub-product ID {sub_id} not found."}), 404
    use_parent_price = data.get("use_parent_price")
    unit_price = data.get("unit_price")
    min_quantity = data.get("min_quantity")
    is_active = data.get("is_active", True)
    use_pp = (1 if use_parent_price else 0) if use_parent_price is not None else s["use_parent_price"]
    new_price = _f(unit_price) if unit_price is not None else _f(s["unit_price"])
    new_min = _f(min_quantity) if min_quantity is not None else _f(s["min_quantity"])
    db.execute(
        """UPDATE sub_products SET name=?, sku=?, description=?, use_parent_price=?,
           unit_price=?, tax_rate=?, min_quantity=?, is_active=? WHERE id=?""",
        (name, data.get("sku") or None, data.get("description") or None, use_pp,
         new_price, _f(s["parent_tax"]), new_min,
         1 if is_active else 0, sub_id),
    )
    db.commit()
    return jsonify({"result": f"✓ Sub-product '{name}' (ID: {sub_id}) under '{s['parent_name']}' updated."})


@bp.route("/sub-products/<int:sub_id>", methods=["DELETE"])
@require_auth
def delete_sub_product(sub_id):
    db = get_db()
    s = db.execute(
        "SELECT s.name, p.name AS parent FROM sub_products s JOIN products p ON p.id=s.product_id WHERE s.id=?",
        (sub_id,),
    ).fetchone()
    if not s:
        return jsonify({"error": f"Sub-product ID {sub_id} not found."}), 404
    display_name = f"{s['parent']} — {s['name']}"
    db.execute(
        "UPDATE purchase_order_items SET product_name=?, sub_product_id=NULL WHERE sub_product_id=?",
        (display_name, sub_id),
    )
    db.execute(
        "UPDATE dispatch_items SET product_name=?, sub_product_id=NULL WHERE sub_product_id=?",
        (display_name, sub_id),
    )
    db.execute("UPDATE invoice_items SET sub_product_id=NULL WHERE sub_product_id=?", (sub_id,))
    db.execute("DELETE FROM stock_tally_items WHERE sub_id=?", (sub_id,))
    db.execute("DELETE FROM stock_movements WHERE sub_product_id=?", (sub_id,))
    db.execute("DELETE FROM sub_products WHERE id=?", (sub_id,))
    db.commit()
    return jsonify({"result": f"✓ Sub-product '{display_name}' (ID: {sub_id}) permanently deleted."})


# ═════════════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/suppliers")
@require_auth
def get_suppliers():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, company, email, phone, address FROM suppliers WHERE is_active=1 ORDER BY name"
    ).fetchall()
    if not rows:
        return jsonify({"result": "No suppliers found."})
    text = "\n".join(
        f"ID {r['id']}: {r['name']}" +
        (f" ({r['company']})" if r["company"] else "") +
        (f" | {r['email']}" if r["email"] else "") +
        (f" | {r['phone']}" if r["phone"] else "") +
        (f" | {r['address']}" if r["address"] else "")
        for r in rows
    )
    return jsonify({"result": text})


@bp.route("/suppliers", methods=["POST"])
@require_auth
def create_supplier():
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Supplier name is required."}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO suppliers (name, company, email, phone, address, notes) VALUES (?,?,?,?,?,?)",
        (name, data.get("company") or None, data.get("email") or None,
         data.get("phone") or None, data.get("address") or None, data.get("notes") or None),
    )
    db.commit()
    return jsonify({"result": f"✓ Supplier '{name}' created (ID: {cur.lastrowid})."})


@bp.route("/suppliers/<int:supplier_id>", methods=["PUT"])
@require_auth
def update_supplier(supplier_id):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Supplier name is required."}), 400
    db = get_db()
    s = db.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not s:
        return jsonify({"error": f"Supplier ID {supplier_id} not found."}), 404
    db.execute(
        "UPDATE suppliers SET name=?, company=?, email=?, phone=?, address=?, notes=? WHERE id=?",
        (name, data.get("company") or None, data.get("email") or None,
         data.get("phone") or None, data.get("address") or None,
         data.get("notes") or None, supplier_id),
    )
    db.commit()
    return jsonify({"result": f"✓ Supplier ID {supplier_id} ('{name}') updated."})


@bp.route("/suppliers/<int:supplier_id>", methods=["DELETE"])
@require_auth
def delete_supplier(supplier_id):
    db = get_db()
    s = db.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not s:
        return jsonify({"error": f"Supplier ID {supplier_id} not found."}), 404
    db.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
    db.commit()
    return jsonify({"result": f"✓ Supplier '{s['name']}' (ID: {supplier_id}) permanently deleted."})


# ═════════════════════════════════════════════════════════════════════════════
# PRODUCTION ORDERS (read uses /purchase-orders path, writes use /production-orders)
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/purchase-orders")
@require_auth
def get_purchase_orders():
    status = request.args.get("status", "open")
    db = get_db()
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
        return jsonify({"result": f"No {status} production orders found."})
    lines = []
    for r in rows:
        lines.append(
            f"PO-{r['id']:04d} | {r['name']} | {r['status']} | "
            f"due: {r['expected_completion'] or '—'}" +
            (f" | supplier: {r['supplier']}" if r["supplier"] else "")
        )
    return jsonify({"result": "\n".join(lines)})


@bp.route("/purchase-orders/<int:po_id>")
@require_auth
def get_purchase_order_details(po_id):
    db = get_db()
    po = db.execute(
        "SELECT po.*, s.name AS supplier FROM purchase_orders po "
        "LEFT JOIN suppliers s ON s.id=po.supplier_id WHERE po.id=?",
        (po_id,),
    ).fetchone()
    if not po:
        return jsonify({"error": f"Production order ID {po_id} not found."}), 404
    items = db.execute(
        """SELECT CASE WHEN poi.sub_product_id IS NOT NULL
                      THEN par.name || ' — ' || sub.name
                      WHEN poi.product_name IS NOT NULL
                      THEN poi.product_name
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
        remaining = _f(it["quantity"]) - dispatched
        lines.append(
            f"  {it['display_name']} | ordered: {_f(it['quantity']):.0f} | "
            f"dispatched: {dispatched:.0f} | remaining: {remaining:.0f}"
            + (f" | price: {_inr(it['price'])}" if it["price"] else "")
        )
    return jsonify({"result": "\n".join(lines)})


@bp.route("/production-orders", methods=["POST"])
@require_auth
def create_production_order():
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Order name is required."}), 400
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "At least one item is required."}), 400
    expected_completion = data.get("expected_completion") or ""
    if expected_completion:
        try:
            datetime.strptime(expected_completion, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "expected_completion must be YYYY-MM-DD."}), 400
    supplier_id = data.get("supplier_id") or None
    notes = data.get("notes") or None
    db = get_db()
    if supplier_id:
        s = db.execute("SELECT id FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not s:
            return jsonify({"error": f"Supplier ID {supplier_id} not found."}), 404
    cur = db.execute(
        "INSERT INTO purchase_orders (name, supplier_id, expected_completion, status, notes) VALUES (?,?,?,?,?)",
        (name, supplier_id, expected_completion or None, "open", notes),
    )
    po_id = cur.lastrowid
    for it in items:
        pid = int(it["product_id"]) if it.get("product_id") else None
        spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
        qty = _f(it.get("quantity"))
        price = _f(it.get("price")) or None
        if qty <= 0:
            continue
        db.execute(
            "INSERT INTO purchase_order_items (po_id, product_id, sub_product_id, quantity, price) VALUES (?,?,?,?,?)",
            (po_id, pid, spid, qty, price),
        )
        tbl = "sub_products" if spid else "products"
        pk = spid if spid else pid
        if pk:
            db.execute(f"UPDATE {tbl} SET production_qty=production_qty+? WHERE id=?", (qty, pk))
    db.commit()
    return jsonify({"result": f"✓ Production order '{name}' created (PO-{po_id:04d}) with {len(items)} item(s). Status: open."})


@bp.route("/production-orders/<int:po_id>", methods=["PUT"])
@require_auth
def update_production_order(po_id):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Order name is required."}), 400
    db = get_db()
    po = db.execute("SELECT name FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po:
        return jsonify({"error": f"Production order ID {po_id} not found."}), 404
    supplier_id = data.get("supplier_id") or None
    expected_completion = data.get("expected_completion") or None
    notes = data.get("notes") or None
    db.execute(
        "UPDATE purchase_orders SET name=?, supplier_id=?, expected_completion=?, notes=? WHERE id=?",
        (name, supplier_id, expected_completion, notes, po_id),
    )
    db.commit()
    return jsonify({"result": f"✓ Production order PO-{po_id:04d} updated."})


@bp.route("/production-orders/<int:po_id>/close", methods=["PUT"])
@require_auth
def close_production_order(po_id):
    db = get_db()
    po = db.execute("SELECT name, status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po:
        return jsonify({"error": f"Production order ID {po_id} not found."}), 404
    if po["status"] == "closed":
        return jsonify({"result": f"PO-{po_id:04d} is already closed."})
    db.execute("UPDATE purchase_orders SET status='closed' WHERE id=?", (po_id,))
    db.commit()
    return jsonify({"result": f"✓ Production order PO-{po_id:04d} ('{po['name']}') closed."})


@bp.route("/production-orders/<int:po_id>", methods=["DELETE"])
@require_auth
def delete_production_order(po_id):
    db = get_db()
    po = db.execute("SELECT name FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po:
        return jsonify({"error": f"Production order ID {po_id} not found."}), 404
    items = db.execute(
        "SELECT product_id, sub_product_id, quantity, qty_dispatched FROM purchase_order_items WHERE po_id=?",
        (po_id,),
    ).fetchall()
    for it in items:
        undispatched = _f(it["quantity"]) - _f(it["qty_dispatched"])
        if undispatched > 0:
            tbl = "sub_products" if it["sub_product_id"] else "products"
            pk = it["sub_product_id"] if it["sub_product_id"] else it["product_id"]
            if pk:
                db.execute(f"UPDATE {tbl} SET production_qty=production_qty-? WHERE id=?",
                           (undispatched, pk))
    db.execute("DELETE FROM purchase_orders WHERE id=?", (po_id,))
    db.commit()
    return jsonify({"result": f"✓ Production order PO-{po_id:04d} ('{po['name']}') permanently deleted and quantities reversed."})


# ═════════════════════════════════════════════════════════════════════════════
# DISPATCHES
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/dispatches")
@require_auth
def get_dispatches():
    status = request.args.get("status", "in_transit")
    db = get_db()
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
        return jsonify({"result": f"No dispatches with status '{status}'."})
    lines = []
    for r in rows:
        lines.append(
            f"DISP-{r['id']:04d} | {r['name']} | {r['status']} | "
            f"dispatched: {r['dispatch_date'] or '—'} | arrival: {r['expected_arrival'] or '—'}"
            + (f" | {r['supplier']}" if r["supplier"] else "")
        )
    return jsonify({"result": "\n".join(lines)})


@bp.route("/dispatches/<int:dispatch_id>")
@require_auth
def get_dispatch_details(dispatch_id):
    db = get_db()
    d = db.execute(
        "SELECT d.*, s.name AS supplier FROM dispatches d "
        "LEFT JOIN suppliers s ON s.id=d.supplier_id WHERE d.id=?",
        (dispatch_id,),
    ).fetchone()
    if not d:
        return jsonify({"error": f"Dispatch ID {dispatch_id} not found."}), 404
    items = db.execute(
        """SELECT di.id, di.quantity, di.qty_received,
                  CASE WHEN di.sub_product_id IS NOT NULL
                       THEN par.name || ' — ' || sub.name
                       WHEN di.product_name IS NOT NULL
                       THEN di.product_name
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
        pending = _f(it["quantity"]) - received
        lines.append(
            f"  di-{it['id']} | {it['display_name']} | "
            f"dispatched: {_f(it['quantity']):.0f} | received: {received:.0f} | pending: {pending:.0f}"
        )
    return jsonify({"result": "\n".join(lines)})


@bp.route("/dispatches", methods=["POST"])
@require_auth
def create_dispatch():
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Dispatch name is required."}), 400
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "At least one item is required."}), 400
    dispatch_date = data.get("dispatch_date") or ""
    expected_arrival = data.get("expected_arrival") or ""
    for d in [dispatch_date, expected_arrival]:
        if d:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": f"Dates must be YYYY-MM-DD, got '{d}'."}), 400
    supplier_id = data.get("supplier_id") or None
    notes = data.get("notes") or None

    db = get_db()
    # Validate production stock
    errors = []
    for it in items:
        pid = int(it["product_id"]) if it.get("product_id") else None
        spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
        qty = _f(it.get("quantity"))
        if spid:
            row = db.execute("SELECT name, production_qty FROM sub_products WHERE id=?", (spid,)).fetchone()
        else:
            row = db.execute("SELECT name, production_qty FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            errors.append("Product/sub-product not found.")
            continue
        available = _f(row["production_qty"])
        if qty > available:
            errors.append(f"{row['name']}: dispatch qty {qty:.0f} exceeds production qty {available:.0f}.")
    if errors:
        return jsonify({"error": "Error — insufficient production stock:\n" + "\n".join(errors)}), 400

    cur = db.execute(
        "INSERT INTO dispatches (name, supplier_id, dispatch_date, expected_arrival, notes) VALUES (?,?,?,?,?)",
        (name, supplier_id, dispatch_date or None, expected_arrival or None, notes),
    )
    dispatch_id = cur.lastrowid

    warnings = []
    for it in items:
        pid = int(it["product_id"]) if it.get("product_id") else None
        spid = int(it["sub_product_id"]) if it.get("sub_product_id") else None
        qty = _f(it.get("quantity"))
        price = _f(it.get("price")) or None

        db.execute(
            "INSERT INTO dispatch_items (dispatch_id, product_id, sub_product_id, quantity, price) VALUES (?,?,?,?,?)",
            (dispatch_id, pid, spid, qty, price),
        )

        leftover = _deduct_production_fifo(db, pid, spid, qty)
        if leftover > 0.001:
            warnings.append(f"{qty - leftover:.0f}/{qty:.0f} units matched to open POs; remainder taken from production.")

        _update_qty(db, pid, spid, "production_qty", -qty)
        _update_qty(db, pid, spid, "in_transit_qty", +qty)

        db.execute(
            "INSERT INTO stock_movements "
            "(product_id, sub_product_id, movement_type, quantity, notes, dispatch_id, expected_arrival) "
            "VALUES (?,?,'transit_dispatch',?,?,?,?)",
            (pid, spid, qty, notes or None, dispatch_id, expected_arrival or None),
        )

    db.commit()
    result = f"✓ Dispatch '{name}' created (DISP-{dispatch_id:04d}) with {len(items)} item(s)."
    if warnings:
        result += "\nWarnings:\n" + "\n".join(f"  ⚠ {w}" for w in warnings)
    return jsonify({"result": result})


@bp.route("/dispatches/<int:dispatch_id>/receive", methods=["POST"])
@require_auth
def receive_dispatch_items(dispatch_id):
    data = _jb()
    received_items = data.get("received_items", [])
    if not received_items:
        return jsonify({"error": "Provide at least one item with dispatch_item_id and quantity."}), 400
    db = get_db()
    d = db.execute("SELECT name, status FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if not d:
        return jsonify({"error": f"Dispatch ID {dispatch_id} not found."}), 404
    if d["status"] == "received":
        return jsonify({"result": f"Dispatch DISP-{dispatch_id:04d} is already fully received."})

    lines = []
    for it in received_items:
        di_id = int(it["dispatch_item_id"])
        qty = _f(it.get("quantity"))
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
        _update_qty(db, row["product_id"], row["sub_product_id"], "stock_qty", +qty)
        db.execute(
            "INSERT INTO stock_movements "
            "(product_id, sub_product_id, movement_type, quantity, notes, dispatch_id) "
            "VALUES (?,?,'transit_arrival',?,?,?)",
            (row["product_id"], row["sub_product_id"], qty,
             f"Received from DISP-{dispatch_id:04d}", dispatch_id),
        )
        lines.append(f"  ✓ Item {di_id}: {qty:.0f} units received → warehouse stock")

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
    return jsonify({"result": result})


@bp.route("/dispatches/<int:dispatch_id>", methods=["DELETE"])
@require_auth
def delete_dispatch(dispatch_id):
    db = get_db()
    d = db.execute("SELECT name FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if not d:
        return jsonify({"error": f"Dispatch ID {dispatch_id} not found."}), 404
    items = db.execute("SELECT * FROM dispatch_items WHERE dispatch_id=?", (dispatch_id,)).fetchall()
    for it in items:
        unreceived = _f(it["quantity"]) - _f(it["qty_received"])
        if unreceived > 0:
            _update_qty(db, it["product_id"], it["sub_product_id"], "in_transit_qty", -unreceived)
            _update_qty(db, it["product_id"], it["sub_product_id"], "production_qty", +unreceived)
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
    return jsonify({"result": f"✓ Dispatch DISP-{dispatch_id:04d} ('{d['name']}') permanently deleted and stock movements reversed."})


# ─── Stock Tallies ────────────────────────────────────────────────────────────

@bp.route("/tallies", methods=["GET"])
@require_auth
def list_tallies_api():
    db = get_db()
    rows = db.execute("SELECT * FROM stock_tallies ORDER BY created_at DESC").fetchall()
    out = []
    for t in rows:
        total = db.execute(
            "SELECT COUNT(*) FROM stock_tally_items WHERE tally_id=?", (t["id"],)
        ).fetchone()[0]
        pending = db.execute(
            "SELECT COUNT(*) FROM stock_tally_items WHERE tally_id=? AND physical_qty IS NULL",
            (t["id"],),
        ).fetchone()[0]
        out.append({
            "id": t["id"],
            "name": t["name"],
            "status": t["status"],
            "notes": t["notes"],
            "created_at": t["created_at"],
            "applied_at": t["applied_at"],
            "total_items": total,
            "pending_items": pending,
        })
    return jsonify(out)


@bp.route("/tallies/<int:tally_id>", methods=["GET"])
@require_auth
def get_tally_api(tally_id):
    from ..services.tally_service import get_tally
    tally, categories = get_tally(tally_id)
    if not tally:
        return jsonify({"error": "Tally not found"}), 404

    cat_list = []
    for cat in categories:
        groups_out = []
        for g in cat["groups"]:
            items_out = []
            for row in g["rows"]:
                diff = None
                if row["physical_qty"] is not None:
                    diff = (row["physical_qty"] or 0) - (row["digital_qty"] or 0)
                items_out.append({
                    "item_id": row["id"],
                    "sub_id": row["sub_id"],
                    "sub_name": row["sub_name"],
                    "sub_sku": row["sub_sku"],
                    "digital_qty": row["digital_qty"],
                    "physical_qty": row["physical_qty"],
                    "diff": diff,
                    "refreshed_at": row["refreshed_at"],
                })
            groups_out.append({
                "product_id": g["product_id"],
                "product_name": g["product_name"],
                "product_sku": g["product_sku"],
                "pcs_per_carton": g["pcs_per_carton"],
                "items": items_out,
            })
        cat_list.append({"category_name": cat["category_name"], "groups": groups_out})

    return jsonify({
        "id": tally["id"],
        "name": tally["name"],
        "status": tally["status"],
        "notes": tally["notes"],
        "created_at": tally["created_at"],
        "applied_at": tally["applied_at"],
        "categories": cat_list,
    })


@bp.route("/tallies", methods=["POST"])
@require_auth
def create_tally_api():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    notes = (data.get("notes") or "").strip() or None
    from ..services.tally_service import create_tally
    tally_id = create_tally(name, notes)
    return jsonify({"tally_id": tally_id, "result": f"Tally '{name}' created with id {tally_id}."})


@bp.route("/tallies/<int:tally_id>/items/<int:item_id>", methods=["PUT"])
@require_auth
def update_tally_item_api(tally_id, item_id):
    db = get_db()
    tally = db.execute("SELECT status FROM stock_tallies WHERE id=?", (tally_id,)).fetchone()
    if not tally:
        return jsonify({"error": "Tally not found"}), 404
    if tally["status"] != "draft":
        return jsonify({"error": "Tally is not a draft — cannot update items"}), 400
    data = request.get_json(silent=True) or {}
    if "physical_qty" not in data:
        return jsonify({"error": "physical_qty is required"}), 400
    val = data["physical_qty"]
    if val is not None:
        try:
            val = float(val)
        except (TypeError, ValueError):
            return jsonify({"error": "physical_qty must be a number or null"}), 400
    db.execute(
        "UPDATE stock_tally_items SET physical_qty=? WHERE id=? AND tally_id=?",
        (val, item_id, tally_id),
    )
    db.commit()
    return jsonify({"result": f"Item {item_id} updated — physical_qty set to {val}."})


@bp.route("/tallies/<int:tally_id>/apply", methods=["POST"])
@require_auth
def apply_tally_api(tally_id):
    from ..services.tally_service import apply_tally
    ok, err = apply_tally(tally_id)
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"result": f"Tally {tally_id} applied — stock quantities updated to match physical counts."})


# ═════════════════════════════════════════════════════════════════════════════
# PALM PURCHASES — instant warehouse stock-in
# ═════════════════════════════════════════════════════════════════════════════

@bp.route("/palm-purchases")
@require_auth
def api_list_palm_purchases():
    from ..services import palm_purchase_service
    rows = palm_purchase_service.get_all_palm_purchases()
    out = []
    for r in rows:
        out.append({
            "id":            r["id"],
            "name":          r["name"],
            "supplier_id":   r["supplier_id"],
            "supplier_name": r["supplier_name"],
            "purchase_date": r["purchase_date"],
            "total_cost":    r["total_cost"],
            "total_qty":     r["total_qty"],
            "item_count":    r["item_count"],
            "notes":         r["notes"],
            "created_at":    r["created_at"],
        })
    return jsonify({"result": out})


@bp.route("/palm-purchases/<int:pp_id>")
@require_auth
def api_get_palm_purchase(pp_id):
    from ..services import palm_purchase_service
    pp = palm_purchase_service.get_palm_purchase(pp_id)
    if not pp:
        return jsonify({"error": f"Palm purchase {pp_id} not found"}), 404
    items = palm_purchase_service.get_palm_purchase_items(pp_id)
    return jsonify({"result": {
        "id":            pp["id"],
        "name":          pp["name"],
        "supplier_id":   pp["supplier_id"],
        "supplier_name": pp["supplier_name"],
        "purchase_date": pp["purchase_date"],
        "total_cost":    pp["total_cost"],
        "notes":         pp["notes"],
        "created_at":    pp["created_at"],
        "items": [{
            "id":             it["id"],
            "product_id":     it["product_id"],
            "sub_product_id": it["sub_product_id"],
            "display_name":   it["display_name"],
            "sku":            it["sub_sku"] or it["product_sku"],
            "quantity":       it["quantity"],
            "unit_cost":      it["unit_cost"],
            "line_cost":      (it["quantity"] or 0) * (it["unit_cost"] or 0),
            "notes":          it["notes"],
        } for it in items],
    }})


@bp.route("/palm-purchases", methods=["POST"])
@require_auth
def api_create_palm_purchase():
    """Create a palm purchase. Body:
       { "supplier_id": int|null, "purchase_date": "YYYY-MM-DD" (default today),
         "name": str?, "notes": str?,
         "items": [ {"product_id":int, "sub_product_id":int?, "quantity":number, "unit_cost":number?, "notes":str?}, ... ] }
       Increments warehouse stock_qty for each item and records a stock_movement of type 'palm_purchase'.
    """
    from ..services import palm_purchase_service
    data = _jb()
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "items[] is required (list of {product_id, sub_product_id?, quantity, unit_cost?})"}), 400
    pdata = {
        "name":          data.get("name") or "",
        "supplier_id":   data.get("supplier_id") or None,
        "purchase_date": data.get("purchase_date") or str(date.today()),
        "notes":         data.get("notes") or "",
    }
    try:
        pp_id = palm_purchase_service.create_palm_purchase(pdata, items)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    pp = palm_purchase_service.get_palm_purchase(pp_id)
    out = palm_purchase_service.get_palm_purchase_items(pp_id)
    total_qty = sum(float(it["quantity"] or 0) for it in out)
    lines = [
        f"✓ Palm purchase #{pp_id} recorded — warehouse stock increased.",
        f"  Date: {pp['purchase_date']} | Supplier: {pp['supplier_name'] or 'walk-in'} | Items: {len(out)} | Total qty added: {total_qty:.2f}",
    ]
    for it in out:
        lines.append(f"  • {it['display_name']}: +{float(it['quantity']):.2f}"
                     + (f"  @ {_inr(it['unit_cost'])}" if it["unit_cost"] else ""))
    return jsonify({"result": "\n".join(lines)})


@bp.route("/palm-purchases/<int:pp_id>", methods=["DELETE"])
@require_auth
def api_delete_palm_purchase(pp_id):
    from ..services import palm_purchase_service
    pp = palm_purchase_service.get_palm_purchase(pp_id)
    if not pp:
        return jsonify({"error": f"Palm purchase {pp_id} not found"}), 404
    palm_purchase_service.delete_palm_purchase(pp_id)
    return jsonify({"result": f"✓ Palm purchase #{pp_id} deleted — warehouse stock reversed."})


# ═════════════════════════════════════════════════════════════════════════════
# STOCK HISTORY — movement audit trail
# ═════════════════════════════════════════════════════════════════════════════

# Buckets group movement_types so callers can filter by where stock lives.
_BUCKET_MOVEMENT_TYPES = {
    "warehouse": ("opening", "add", "sale", "sale_cancelled",
                  "warehouse_add", "warehouse_deduct",
                  "correction", "palm_purchase", "palm_purchase_reversed",
                  "arrival", "transit_arrival"),
    "production": ("production", "production_add", "production_deduct"),
    "transit":    ("dispatch", "transit_dispatch",
                   "dispatch_add", "dispatch_deduct"),
}


@bp.route("/products/<int:product_id>/stock-history")
@require_auth
def api_product_stock_history(product_id):
    """Return chronological stock movements for a product (and optionally a sub-product).
    Query params:
      sub_product_id (int, optional) — if set, only movements for that sub-product
      bucket (warehouse|production|transit, optional) — filter by stock bucket
      limit (int, default 100, max 500)
    """
    db = get_db()
    sub_product_id = request.args.get("sub_product_id", type=int)
    bucket = (request.args.get("bucket") or "").strip().lower() or None
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    # Existence check
    prod = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
    if not prod:
        return jsonify({"error": f"Product {product_id} not found"}), 404
    sub_name = None
    if sub_product_id:
        srow = db.execute("SELECT name FROM sub_products WHERE id=? AND product_id=?",
                          (sub_product_id, product_id)).fetchone()
        if not srow:
            return jsonify({"error": f"Sub-product {sub_product_id} not found under product {product_id}"}), 404
        sub_name = srow["name"]

    where = ["product_id = ?"]
    params = [product_id]
    if sub_product_id is not None:
        where.append("sub_product_id = ?")
        params.append(sub_product_id)
    else:
        # default: include all movements for the product (with or without sub_product)
        pass

    if bucket:
        if bucket not in _BUCKET_MOVEMENT_TYPES:
            return jsonify({"error": "bucket must be one of: warehouse, production, transit"}), 400
        types = _BUCKET_MOVEMENT_TYPES[bucket]
        placeholders = ",".join("?" * len(types))
        where.append(f"movement_type IN ({placeholders})")
        params.extend(types)

    sql = (
        f"SELECT id, product_id, sub_product_id, movement_type, quantity, "
        f"       notes, invoice_id, dispatch_id, palm_purchase_id, "
        f"       expected_arrival, linked_movement_id, created_at "
        f"FROM stock_movements WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    params.append(limit)
    rows = db.execute(sql, params).fetchall()

    history = []
    for r in rows:
        history.append({
            "id":             r["id"],
            "type":           r["movement_type"],
            "quantity":       r["quantity"],
            "notes":          r["notes"],
            "invoice_id":     r["invoice_id"],
            "dispatch_id":    r["dispatch_id"],
            "palm_purchase_id": r["palm_purchase_id"],
            "linked_id":      r["linked_movement_id"],
            "expected_arrival": r["expected_arrival"],
            "sub_product_id": r["sub_product_id"],
            "created_at":     r["created_at"],
        })

    # Current bucket levels for context
    if sub_product_id:
        cur_row = db.execute(
            "SELECT stock_qty, production_qty, in_transit_qty FROM sub_products WHERE id=?",
            (sub_product_id,)).fetchone()
    else:
        cur_row = db.execute(
            "SELECT stock_qty, production_qty, in_transit_qty FROM products WHERE id=?",
            (product_id,)).fetchone()

    return jsonify({"result": {
        "product_id":     product_id,
        "product_name":   prod["name"],
        "sub_product_id": sub_product_id,
        "sub_product_name": sub_name,
        "bucket_filter":  bucket,
        "current": {
            "warehouse":  (cur_row["stock_qty"] if cur_row else 0) or 0,
            "production": (cur_row["production_qty"] if cur_row else 0) or 0,
            "transit":    (cur_row["in_transit_qty"] if cur_row else 0) or 0,
        },
        "history": history,
        "count":   len(history),
        "limit":   limit,
    }})


# ═════════════════════════════════════════════════════════════════════════════
# BULK ENDPOINTS — fan-out questions in one call (structured JSON, not text)
# ─────────────────────────────────────────────────────────────────────────────
# Convention: every endpoint here returns
#     {"result": {"items": [...], "truncated": bool, "count": int, ...}}
# Filters are query-string. `include=` is a CSV of optional expansions.
# Hard caps are enforced so a missing filter never returns the entire DB.
# ═════════════════════════════════════════════════════════════════════════════

def _csv_ints(raw):
    """Parse 'a,b,c' into [int(a), int(b), int(c)]; empties / non-ints dropped."""
    if not raw:
        return []
    out = []
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            pass
    return out


def _csv_set(raw):
    if not raw:
        return set()
    return {tok.strip() for tok in str(raw).split(",") if tok.strip()}


def _arg_int(name, default, lo=1, hi=500):
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _arg_date(name):
    """Return YYYY-MM-DD string if present and valid, else None."""
    v = request.args.get(name)
    if not v:
        return None
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return v
    except ValueError:
        return None


# ── 1. /api/products/snapshot ─────────────────────────────────────────────────
@bp.route("/products/snapshot")
@require_auth
def products_snapshot():
    """Per-SKU row (product or sub-product) with current stock + optional 30/60/90-day sales velocity
    and last purchase cost. Replaces N× get_product_stock_history + N× search_products loops.

    Query params:
      category_id           — filter to a category
      product_ids           — CSV of parent product ids (matches that product + all its subs)
      sub_product_ids       — CSV of specific sub-product ids
      include               — CSV: 'velocity' (sold_30/60/90 from stock_movements),
                                   'last_purchase' (most recent palm_purchase_items unit_cost)
      include_inactive=0/1  — default 0 (only active SKUs)
      limit                 — default 200, max 500
    """
    db = get_db()
    category_id    = request.args.get("category_id", type=int)
    product_ids    = _csv_ints(request.args.get("product_ids"))
    sub_ids        = _csv_ints(request.args.get("sub_product_ids"))
    include        = _csv_set(request.args.get("include"))
    include_inact  = request.args.get("include_inactive", "0") in ("1", "true", "yes")
    limit          = _arg_int("limit", 200)

    # Build product WHERE
    p_where, p_params = [], []
    if not include_inact:
        p_where.append("p.is_active = 1")
    if category_id is not None:
        p_where.append("p.category_id = ?"); p_params.append(category_id)
    if product_ids:
        ph = ",".join("?" * len(product_ids))
        p_where.append(f"p.id IN ({ph})"); p_params.extend(product_ids)
    p_clause = (" WHERE " + " AND ".join(p_where)) if p_where else ""

    # Fetch parent products
    products = db.execute(
        f"""SELECT p.id, p.name, p.sku, p.category_id, c.name AS category_name,
                   p.unit_price, p.tax_rate, p.stock_qty, p.production_qty, p.in_transit_qty,
                   p.pcs_per_carton, p.min_quantity, p.has_eco_range, p.eco_parent_id,
                   p.is_active
            FROM products p LEFT JOIN categories c ON c.id = p.category_id
            {p_clause}
            ORDER BY p.name""",
        p_params,
    ).fetchall()
    product_ids_in_scope = [p["id"] for p in products]

    # Fetch sub-products under those parents (or by explicit sub_ids)
    s_where, s_params = [], []
    if not include_inact:
        s_where.append("s.is_active = 1")
    if sub_ids:
        ph = ",".join("?" * len(sub_ids))
        s_where.append(f"s.id IN ({ph})"); s_params.extend(sub_ids)
    elif product_ids_in_scope:
        ph = ",".join("?" * len(product_ids_in_scope))
        s_where.append(f"s.product_id IN ({ph})"); s_params.extend(product_ids_in_scope)
    else:
        # No matching products and no explicit subs — nothing to fetch
        s_where.append("1=0")
    s_clause = " WHERE " + " AND ".join(s_where)

    subs = db.execute(
        f"""SELECT s.id, s.product_id, s.name, s.sku, s.unit_price, s.use_parent_price,
                   s.tax_rate, s.stock_qty, s.production_qty, s.in_transit_qty,
                   s.pcs_per_carton, s.min_quantity, s.eco_parent_sub_id, s.is_active,
                   p.name AS parent_name, p.category_id, c.name AS category_name,
                   p.pcs_per_carton AS parent_pcs_per_carton, p.unit_price AS parent_unit_price
            FROM sub_products s
            JOIN products p ON p.id = s.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            {s_clause}
            ORDER BY p.name, s.name""",
        s_params,
    ).fetchall()
    parents_with_subs = {s["product_id"] for s in subs}

    # Build the row list: sub_products represent their own SKUs; products only if no subs.
    rows = []
    for p in products:
        if p["id"] in parents_with_subs:
            continue  # sub-products will represent this product's SKUs
        rows.append({
            "entity_type":   "product",
            "product_id":    p["id"],
            "sub_product_id": None,
            "name":          p["name"],
            "sku":           p["sku"],
            "category_id":   p["category_id"],
            "category_name": p["category_name"],
            "unit_price":    _f(p["unit_price"]),
            "tax_rate":      _f(p["tax_rate"]),
            "stock":         _f(p["stock_qty"]),
            "production":    _f(p["production_qty"]),
            "transit":       _f(p["in_transit_qty"]),
            "pcs_per_carton": p["pcs_per_carton"] or 0,
            "min_quantity":  _f(p["min_quantity"]),
            "has_eco_range": bool(p["has_eco_range"]),
            "eco_parent_id": p["eco_parent_id"],
            "is_active":     bool(p["is_active"]),
        })

    for s in subs:
        eff_price = _f(s["unit_price"]) if not s["use_parent_price"] else _f(s["parent_unit_price"])
        rows.append({
            "entity_type":   "sub_product",
            "product_id":    s["product_id"],
            "sub_product_id": s["id"],
            "name":          f"{s['parent_name']} — {s['name']}",
            "sku":           s["sku"],
            "category_id":   s["category_id"],
            "category_name": s["category_name"],
            "unit_price":    eff_price,
            "tax_rate":      _f(s["tax_rate"]),
            "stock":         _f(s["stock_qty"]),
            "production":    _f(s["production_qty"]),
            "transit":       _f(s["in_transit_qty"]),
            "pcs_per_carton": s["pcs_per_carton"] or s["parent_pcs_per_carton"] or 0,
            "min_quantity":  _f(s["min_quantity"]),
            "has_eco_range": False,
            "eco_parent_sub_id": s["eco_parent_sub_id"],
            "is_active":     bool(s["is_active"]),
        })

    # Optional: 30/60/90-day sales velocity per row (movement_type='sale')
    if "velocity" in include:
        # Build a (product_id, sub_product_id) → totals map
        vel = db.execute(
            """SELECT product_id, sub_product_id,
                      SUM(CASE WHEN created_at >= date('now','-30 days') THEN quantity ELSE 0 END) AS d30,
                      SUM(CASE WHEN created_at >= date('now','-60 days') THEN quantity ELSE 0 END) AS d60,
                      SUM(CASE WHEN created_at >= date('now','-90 days') THEN quantity ELSE 0 END) AS d90
               FROM stock_movements
               WHERE movement_type = 'sale'
               GROUP BY product_id, sub_product_id"""
        ).fetchall()
        velmap = {(v["product_id"], v["sub_product_id"]): v for v in vel}
        for r in rows:
            key = (r["product_id"], r["sub_product_id"])
            v = velmap.get(key)
            r["velocity"] = {
                "sold_30d": _f(v["d30"]) if v else 0.0,
                "sold_60d": _f(v["d60"]) if v else 0.0,
                "sold_90d": _f(v["d90"]) if v else 0.0,
            }

    # Optional: last purchase cost per row from palm_purchase_items
    if "last_purchase" in include:
        lp = db.execute(
            """SELECT ppi.product_id, ppi.sub_product_id, ppi.unit_cost,
                      pp.purchase_date, pp.supplier_id, pp.id AS palm_purchase_id
               FROM palm_purchase_items ppi
               JOIN palm_purchases pp ON pp.id = ppi.palm_purchase_id
               ORDER BY pp.purchase_date DESC, pp.id DESC"""
        ).fetchall()
        seen = {}
        for r in lp:
            key = (r["product_id"], r["sub_product_id"])
            if key not in seen:
                seen[key] = r
        for row in rows:
            key = (row["product_id"], row["sub_product_id"])
            lpr = seen.get(key)
            row["last_purchase"] = {
                "date":             lpr["purchase_date"] if lpr else None,
                "unit_cost":        _f(lpr["unit_cost"]) if lpr else None,
                "supplier_id":      lpr["supplier_id"] if lpr else None,
                "palm_purchase_id": lpr["palm_purchase_id"] if lpr else None,
            } if lpr else None

    truncated = len(rows) > limit
    rows = rows[:limit]
    return jsonify({"result": {"items": rows, "count": len(rows), "truncated": truncated, "limit": limit}})


# ── 2. /api/products/stock-history-bulk ───────────────────────────────────────
@bp.route("/products/stock-history-bulk")
@require_auth
def products_stock_history_bulk():
    """Stock movements for many products/sub-products in one call.

    Query params:
      product_ids, sub_product_ids — CSV
      category_id                  — restrict to a category
      bucket                       — warehouse|production|transit
      date_from, date_to           — YYYY-MM-DD
      limit                        — default 500, max 2000
    """
    db = get_db()
    product_ids   = _csv_ints(request.args.get("product_ids"))
    sub_ids       = _csv_ints(request.args.get("sub_product_ids"))
    category_id   = request.args.get("category_id", type=int)
    bucket        = (request.args.get("bucket") or "").strip().lower() or None
    date_from     = _arg_date("date_from")
    date_to       = _arg_date("date_to")
    limit         = _arg_int("limit", 500, hi=2000)

    # Resolve product scope via category if given
    if category_id is not None and not product_ids:
        product_ids = [r["id"] for r in db.execute(
            "SELECT id FROM products WHERE category_id=?", (category_id,)
        ).fetchall()]

    where, params = [], []
    if product_ids:
        ph = ",".join("?" * len(product_ids))
        where.append(f"sm.product_id IN ({ph})"); params.extend(product_ids)
    if sub_ids:
        ph = ",".join("?" * len(sub_ids))
        where.append(f"sm.sub_product_id IN ({ph})"); params.extend(sub_ids)
    if bucket:
        if bucket not in _BUCKET_MOVEMENT_TYPES:
            return jsonify({"error": "bucket must be one of: warehouse, production, transit"}), 400
        types = _BUCKET_MOVEMENT_TYPES[bucket]
        ph = ",".join("?" * len(types))
        where.append(f"sm.movement_type IN ({ph})"); params.extend(types)
    if date_from:
        where.append("date(sm.created_at) >= date(?)"); params.append(date_from)
    if date_to:
        where.append("date(sm.created_at) <= date(?)"); params.append(date_to)

    if not where:
        return jsonify({"error": "Specify at least one filter: product_ids, sub_product_ids, category_id, bucket, or date range."}), 400

    sql = (
        "SELECT sm.id, sm.product_id, sm.sub_product_id, sm.movement_type, sm.quantity, "
        "       sm.notes, sm.invoice_id, sm.dispatch_id, sm.palm_purchase_id, "
        "       sm.expected_arrival, sm.linked_movement_id, sm.created_at, "
        "       p.name AS product_name, s.name AS sub_product_name "
        "FROM stock_movements sm "
        "LEFT JOIN products p ON p.id = sm.product_id "
        "LEFT JOIN sub_products s ON s.id = sm.sub_product_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY sm.created_at DESC, sm.id DESC LIMIT ?"
    )
    params.append(limit + 1)  # fetch +1 to detect truncation
    rows = db.execute(sql, params).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]

    items = [{
        "id":             r["id"],
        "product_id":     r["product_id"],
        "product_name":   r["product_name"],
        "sub_product_id": r["sub_product_id"],
        "sub_product_name": r["sub_product_name"],
        "type":           r["movement_type"],
        "quantity":       _f(r["quantity"]),
        "notes":          r["notes"],
        "invoice_id":     r["invoice_id"],
        "dispatch_id":    r["dispatch_id"],
        "palm_purchase_id": r["palm_purchase_id"],
        "expected_arrival": r["expected_arrival"],
        "linked_id":      r["linked_movement_id"],
        "created_at":     r["created_at"],
    } for r in rows]
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 3. /api/products/sales-velocity ───────────────────────────────────────────
@bp.route("/products/sales-velocity")
@require_auth
def products_sales_velocity():
    """Sold quantity + revenue per product/sub-product over a date range.

    Query params: category_id, product_ids, sub_product_ids,
                  date_from, date_to (default: last 90 days),
                  group_by = product|sub_product (default sub_product),
                  limit (default 500, max 1000)
    """
    db = get_db()
    category_id = request.args.get("category_id", type=int)
    product_ids = _csv_ints(request.args.get("product_ids"))
    sub_ids     = _csv_ints(request.args.get("sub_product_ids"))
    date_from   = _arg_date("date_from")
    date_to     = _arg_date("date_to")
    group_by    = (request.args.get("group_by") or "sub_product").lower()
    if group_by not in ("product", "sub_product"):
        group_by = "sub_product"
    limit       = _arg_int("limit", 500, hi=1000)

    where = ["i.status NOT IN ('cancelled','draft')"]
    params = []
    if date_from:
        where.append("i.issue_date >= ?"); params.append(date_from)
    if date_to:
        where.append("i.issue_date <= ?"); params.append(date_to)
    if not date_from and not date_to:
        where.append("i.issue_date >= date('now','-90 days')")
    if category_id is not None:
        where.append("p.category_id = ?"); params.append(category_id)
    if product_ids:
        ph = ",".join("?" * len(product_ids))
        where.append(f"ii.product_id IN ({ph})"); params.extend(product_ids)
    if sub_ids:
        ph = ",".join("?" * len(sub_ids))
        where.append(f"ii.sub_product_id IN ({ph})"); params.extend(sub_ids)

    group_cols = "ii.product_id, ii.sub_product_id" if group_by == "sub_product" else "ii.product_id"
    sql = (
        "SELECT ii.product_id, "
        + ("ii.sub_product_id, " if group_by == "sub_product" else "")
        + "p.name AS product_name, "
        + ("s.name AS sub_product_name, " if group_by == "sub_product" else "")
        + "p.category_id, c.name AS category_name, "
        "SUM(ii.quantity) AS qty_sold, "
        "SUM(ii.line_total) AS revenue, "
        "COUNT(DISTINCT i.id) AS invoice_count, "
        "MIN(i.issue_date) AS first_sale, MAX(i.issue_date) AS last_sale "
        "FROM invoice_items ii "
        "JOIN invoices i ON i.id = ii.invoice_id "
        "LEFT JOIN products p ON p.id = ii.product_id "
        + ("LEFT JOIN sub_products s ON s.id = ii.sub_product_id " if group_by == "sub_product" else "")
        + "LEFT JOIN categories c ON c.id = p.category_id "
        f"WHERE {' AND '.join(where)} "
        f"GROUP BY {group_cols} "
        "ORDER BY revenue DESC LIMIT ?"
    )
    params.append(limit + 1)
    rows = db.execute(sql, params).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]

    items = []
    for r in rows:
        d = dict(r)
        d["qty_sold"] = _f(d.get("qty_sold"))
        d["revenue"]  = _f(d.get("revenue"))
        items.append(d)
    return jsonify({"result": {
        "items": items, "count": len(items), "truncated": truncated, "limit": limit,
        "group_by": group_by, "date_from": date_from, "date_to": date_to,
    }})


# ── 4. /api/invoices/bulk ─────────────────────────────────────────────────────
@bp.route("/invoices/bulk")
@require_auth
def invoices_bulk():
    """Many invoices in one call, optionally with line items and payment allocations.

    Query params: client_id, company_id, status (CSV: 'paid,issued,partial,draft,cancelled'),
                  date_from, date_to, include (CSV: 'items,payments'), limit (default 100, max 500)
    """
    db = get_db()
    client_id   = request.args.get("client_id", type=int)
    company_id  = request.args.get("company_id", type=int)
    statuses    = _csv_set(request.args.get("status"))
    date_from   = _arg_date("date_from")
    date_to     = _arg_date("date_to")
    include     = _csv_set(request.args.get("include"))
    limit       = _arg_int("limit", 100, hi=500)

    where, params = [], []
    if client_id is not None:
        where.append("i.client_id = ?"); params.append(client_id)
    if company_id is not None:
        where.append("i.company_id = ?"); params.append(company_id)
    if statuses:
        ph = ",".join("?" * len(statuses))
        where.append(f"i.status IN ({ph})"); params.extend(sorted(statuses))
    if date_from:
        where.append("i.issue_date >= ?"); params.append(date_from)
    if date_to:
        where.append("i.issue_date <= ?"); params.append(date_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        f"""SELECT i.id, i.invoice_number, i.client_id, i.company_id, i.status,
                   i.issue_date, i.due_date, i.subtotal, i.tax_total, i.discount_amount,
                   i.total, i.amount_paid, i.notes, i.created_at,
                   c.name AS client_name, cc.name AS company_name
            FROM invoices i
            JOIN clients c ON c.id = i.client_id
            LEFT JOIN client_companies cc ON cc.id = i.company_id
            {clause}
            ORDER BY i.issue_date DESC, i.id DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    inv_ids = [r["id"] for r in rows]

    items_by_inv = {}
    if "items" in include and inv_ids:
        ph = ",".join("?" * len(inv_ids))
        for r in db.execute(
            f"""SELECT id, invoice_id, product_id, sub_product_id, description, sku,
                       quantity, unit_price, tax_rate, line_total,
                       discount_type, discount_value
                FROM invoice_items WHERE invoice_id IN ({ph})""",
            inv_ids,
        ).fetchall():
            items_by_inv.setdefault(r["invoice_id"], []).append(dict(r))

    payments_by_inv = {}
    if "payments" in include and inv_ids:
        ph = ",".join("?" * len(inv_ids))
        for r in db.execute(
            f"""SELECT pa.invoice_id, pa.payment_id, pa.amount AS allocated,
                       p.payment_date, p.method, p.reference, p.notes, p.amount AS payment_total
                FROM payment_allocations pa
                JOIN payments p ON p.id = pa.payment_id
                WHERE pa.invoice_id IN ({ph})
                ORDER BY p.payment_date DESC""",
            inv_ids,
        ).fetchall():
            payments_by_inv.setdefault(r["invoice_id"], []).append(dict(r))

    items = []
    for r in rows:
        d = dict(r)
        d["remaining"] = _f(d["total"]) - _f(d["amount_paid"])
        if "items" in include:
            d["line_items"] = items_by_inv.get(r["id"], [])
        if "payments" in include:
            d["payments"] = payments_by_inv.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 5. /api/payments/bulk ─────────────────────────────────────────────────────
@bp.route("/payments/bulk")
@require_auth
def payments_bulk():
    """Many payments with optional allocation breakdown.

    Query params: client_id, company_id, method, date_from, date_to,
                  include (CSV: 'allocations'), limit (default 200, max 1000)
    """
    db = get_db()
    client_id   = request.args.get("client_id", type=int)
    company_id  = request.args.get("company_id", type=int)
    method      = request.args.get("method")
    date_from   = _arg_date("date_from")
    date_to     = _arg_date("date_to")
    include     = _csv_set(request.args.get("include"))
    limit       = _arg_int("limit", 200, hi=1000)

    where, params = [], []
    if client_id is not None:
        where.append("p.client_id = ?"); params.append(client_id)
    if company_id is not None:
        where.append("p.company_id = ?"); params.append(company_id)
    if method:
        where.append("p.method = ?"); params.append(method)
    if date_from:
        where.append("p.payment_date >= ?"); params.append(date_from)
    if date_to:
        where.append("p.payment_date <= ?"); params.append(date_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        f"""SELECT p.id, p.client_id, p.company_id, p.amount, p.payment_date,
                   p.method, p.reference, p.notes, p.created_at,
                   c.name AS client_name, cc.name AS company_name,
                   (SELECT COALESCE(SUM(amount),0) FROM payment_allocations
                      WHERE payment_id = p.id) AS allocated
            FROM payments p
            JOIN clients c ON c.id = p.client_id
            LEFT JOIN client_companies cc ON cc.id = p.company_id
            {clause}
            ORDER BY p.payment_date DESC, p.id DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    pmt_ids = [r["id"] for r in rows]

    allocs_by_pmt = {}
    if "allocations" in include and pmt_ids:
        ph = ",".join("?" * len(pmt_ids))
        for r in db.execute(
            f"""SELECT pa.payment_id, pa.invoice_id, pa.amount, i.invoice_number
                FROM payment_allocations pa
                JOIN invoices i ON i.id = pa.invoice_id
                WHERE pa.payment_id IN ({ph})""",
            pmt_ids,
        ).fetchall():
            allocs_by_pmt.setdefault(r["payment_id"], []).append(dict(r))

    items = []
    for r in rows:
        d = dict(r)
        d["amount"]    = _f(d["amount"])
        d["allocated"] = _f(d["allocated"])
        d["unallocated"] = d["amount"] - d["allocated"]
        if "allocations" in include:
            d["allocations"] = allocs_by_pmt.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 6. /api/clients/outstanding ───────────────────────────────────────────────
@bp.route("/clients/outstanding")
@require_auth
def clients_outstanding():
    """Per-client outstanding ledger with aged-bucket breakdown of unpaid invoices.

    Query params: min_amount, min_age_days, company_id, limit (default 200, max 500)
    Aged buckets are based on issue_date relative to today.
    """
    db = get_db()
    min_amount   = _f(request.args.get("min_amount") or 0)
    min_age_days = request.args.get("min_age_days", type=int) or 0
    company_id   = request.args.get("company_id", type=int)
    limit        = _arg_int("limit", 200, hi=500)

    co_filter = "AND i.company_id = ?" if company_id is not None else ""
    co_params = [company_id] if company_id is not None else []

    rows = db.execute(
        f"""SELECT c.id AS client_id, c.name AS client_name, c.opening_balance,
                   SUM(CASE WHEN julianday('now') - julianday(i.issue_date) <= 30 THEN i.total - i.amount_paid ELSE 0 END) AS bucket_0_30,
                   SUM(CASE WHEN julianday('now') - julianday(i.issue_date) BETWEEN 31 AND 60 THEN i.total - i.amount_paid ELSE 0 END) AS bucket_31_60,
                   SUM(CASE WHEN julianday('now') - julianday(i.issue_date) BETWEEN 61 AND 90 THEN i.total - i.amount_paid ELSE 0 END) AS bucket_61_90,
                   SUM(CASE WHEN julianday('now') - julianday(i.issue_date) > 90 THEN i.total - i.amount_paid ELSE 0 END) AS bucket_90_plus,
                   SUM(i.total - i.amount_paid) AS total_outstanding,
                   COUNT(*) AS unpaid_invoices,
                   MIN(i.issue_date) AS oldest_unpaid,
                   MAX(CAST(julianday('now') - julianday(i.issue_date) AS INTEGER)) AS oldest_age_days
            FROM invoices i
            JOIN clients c ON c.id = i.client_id
            WHERE i.status NOT IN ('paid','cancelled','draft')
              AND (i.total - i.amount_paid) > 0
              {co_filter}
            GROUP BY c.id
            HAVING SUM(i.total - i.amount_paid) >= ?
               AND MAX(CAST(julianday('now') - julianday(i.issue_date) AS INTEGER)) >= ?
            ORDER BY total_outstanding DESC
            LIMIT ?""",
        co_params + [min_amount, min_age_days, limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]

    items = []
    for r in rows:
        d = dict(r)
        for k in ("bucket_0_30", "bucket_31_60", "bucket_61_90", "bucket_90_plus",
                  "total_outstanding", "opening_balance"):
            d[k] = _f(d.get(k))
        items.append(d)
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 7. /api/clients/bulk ──────────────────────────────────────────────────────
@bp.route("/clients/bulk")
@require_auth
def clients_bulk():
    """Structured list of clients with optional company-level balances and recent invoices.

    Query params: include (CSV: 'companies,balance,recent_invoices'),
                  q (substring search on name/company), limit (default 200, max 500)
    """
    db = get_db()
    include = _csv_set(request.args.get("include"))
    q       = (request.args.get("q") or "").strip().lower()
    limit   = _arg_int("limit", 200, hi=500)

    where, params = [], []
    if q:
        where.append(
            "(LOWER(c.name) LIKE ? OR c.id IN "
            "(SELECT client_id FROM client_companies WHERE LOWER(name) LIKE ?))"
        )
        like = f"%{q}%"
        params.extend([like, like])
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        f"""SELECT c.id, c.name, c.company, c.email, c.phone, c.city, c.country,
                   c.tax_id, c.opening_balance, c.payment_terms, c.created_at
            FROM clients c
            {clause}
            ORDER BY c.name LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    client_ids = [r["id"] for r in rows]

    bal_map = {}
    if "balance" in include and client_ids:
        ph = ",".join("?" * len(client_ids))
        inv_rows = db.execute(
            f"""SELECT client_id,
                       COALESCE(SUM(total),0)                                    AS total_invoiced,
                       COALESCE(SUM(CASE WHEN status NOT IN ('paid','cancelled')
                                         THEN total - amount_paid ELSE 0 END),0) AS outstanding
                FROM invoices WHERE client_id IN ({ph}) AND status != 'cancelled'
                GROUP BY client_id""",
            client_ids,
        ).fetchall()
        pay_rows = db.execute(
            f"""SELECT p.client_id,
                       COALESCE(SUM(p.amount),0) - COALESCE((
                          SELECT SUM(amount) FROM payment_allocations
                          WHERE payment_id IN (SELECT id FROM payments WHERE client_id = p.client_id)
                       ),0) AS unallocated,
                       COALESCE(SUM(p.amount),0) AS total_paid
                FROM payments p WHERE p.client_id IN ({ph})
                GROUP BY p.client_id""",
            client_ids,
        ).fetchall()
        inv_by  = {r["client_id"]: r for r in inv_rows}
        pay_by  = {r["client_id"]: r for r in pay_rows}
        for cid in client_ids:
            invr = inv_by.get(cid)
            payr = pay_by.get(cid)
            bal_map[cid] = {
                "total_invoiced": _f(invr["total_invoiced"]) if invr else 0.0,
                "outstanding":    _f(invr["outstanding"])    if invr else 0.0,
                "total_paid":     _f(payr["total_paid"])     if payr else 0.0,
                "unallocated":    _f(payr["unallocated"])    if payr else 0.0,
            }

    companies_by_client = {}
    if "companies" in include and client_ids:
        ph = ",".join("?" * len(client_ids))
        for r in db.execute(
            f"""SELECT id, client_id, name, tax_id, opening_balance
                FROM client_companies WHERE client_id IN ({ph}) ORDER BY name""",
            client_ids,
        ).fetchall():
            companies_by_client.setdefault(r["client_id"], []).append(dict(r))

    recents_by_client = {}
    if "recent_invoices" in include and client_ids:
        ph = ",".join("?" * len(client_ids))
        for r in db.execute(
            f"""SELECT id, client_id, invoice_number, issue_date, total, amount_paid, status
                FROM invoices
                WHERE client_id IN ({ph})
                ORDER BY issue_date DESC, id DESC""",
            client_ids,
        ).fetchall():
            lst = recents_by_client.setdefault(r["client_id"], [])
            if len(lst) < 5:
                lst.append(dict(r))

    items = []
    for r in rows:
        d = dict(r)
        if "balance" in include:
            d["balance"] = bal_map.get(r["id"])
        if "companies" in include:
            d["companies"] = companies_by_client.get(r["id"], [])
        if "recent_invoices" in include:
            d["recent_invoices"] = recents_by_client.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 8. /api/sales/by-product ──────────────────────────────────────────────────
@bp.route("/sales/by-product")
@require_auth
def sales_by_product():
    """Alias for /products/sales-velocity — caller-friendly name for sales aggregation."""
    return products_sales_velocity()


# ── 9. /api/sales/by-client ───────────────────────────────────────────────────
@bp.route("/sales/by-client")
@require_auth
def sales_by_client():
    """Total invoiced + total paid per client over a date range.

    Query params: date_from, date_to (default: last 90 days), min_invoiced, limit (default 200, max 500)
    """
    db = get_db()
    date_from    = _arg_date("date_from")
    date_to      = _arg_date("date_to")
    min_invoiced = _f(request.args.get("min_invoiced") or 0)
    limit        = _arg_int("limit", 200, hi=500)

    where = ["i.status != 'cancelled'"]
    params = []
    if date_from:
        where.append("i.issue_date >= ?"); params.append(date_from)
    if date_to:
        where.append("i.issue_date <= ?"); params.append(date_to)
    if not date_from and not date_to:
        where.append("i.issue_date >= date('now','-90 days')")

    rows = db.execute(
        f"""SELECT c.id AS client_id, c.name AS client_name,
                   COUNT(DISTINCT i.id)            AS invoice_count,
                   COALESCE(SUM(i.total),0)        AS total_invoiced,
                   COALESCE(SUM(i.amount_paid),0)  AS total_paid,
                   COALESCE(SUM(i.total - i.amount_paid),0) AS outstanding,
                   MIN(i.issue_date) AS first_invoice,
                   MAX(i.issue_date) AS last_invoice
            FROM invoices i
            JOIN clients c ON c.id = i.client_id
            WHERE {' AND '.join(where)}
            GROUP BY c.id
            HAVING total_invoiced >= ?
            ORDER BY total_invoiced DESC LIMIT ?""",
        params + [min_invoiced, limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        d = dict(r)
        for k in ("total_invoiced", "total_paid", "outstanding"):
            d[k] = _f(d.get(k))
        items.append(d)
    return jsonify({"result": {
        "items": items, "count": len(items), "truncated": truncated, "limit": limit,
        "date_from": date_from, "date_to": date_to,
    }})


# ── 10. /api/purchase-orders/bulk ─────────────────────────────────────────────
@bp.route("/purchase-orders/bulk")
@require_auth
def purchase_orders_bulk():
    """POs with optional line items in one call.

    Query params: supplier_id, status (CSV: 'open,closed'), include (CSV: 'items'),
                  limit (default 100, max 300)
    """
    db = get_db()
    supplier_id = request.args.get("supplier_id", type=int)
    statuses    = _csv_set(request.args.get("status"))
    include     = _csv_set(request.args.get("include"))
    limit       = _arg_int("limit", 100, hi=300)

    where, params = [], []
    if supplier_id is not None:
        where.append("po.supplier_id = ?"); params.append(supplier_id)
    if statuses:
        ph = ",".join("?" * len(statuses))
        where.append(f"po.status IN ({ph})"); params.extend(sorted(statuses))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        f"""SELECT po.id, po.name, po.supplier_id, s.name AS supplier_name,
                   po.expected_completion, po.status, po.notes, po.created_at
            FROM purchase_orders po
            LEFT JOIN suppliers s ON s.id = po.supplier_id
            {clause}
            ORDER BY po.created_at DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    po_ids = [r["id"] for r in rows]

    items_by_po = {}
    if "items" in include and po_ids:
        ph = ",".join("?" * len(po_ids))
        for r in db.execute(
            f"""SELECT poi.id, poi.po_id, poi.product_id, poi.sub_product_id,
                       poi.quantity, poi.price, poi.qty_dispatched, poi.product_name,
                       p.name AS product_db_name, sp.name AS sub_db_name
                FROM purchase_order_items poi
                LEFT JOIN products p ON p.id = poi.product_id
                LEFT JOIN sub_products sp ON sp.id = poi.sub_product_id
                WHERE poi.po_id IN ({ph})""",
            po_ids,
        ).fetchall():
            d = dict(r)
            d["pending"] = _f(d["quantity"]) - _f(d["qty_dispatched"])
            items_by_po.setdefault(r["po_id"], []).append(d)

    items = []
    for r in rows:
        d = dict(r)
        if "items" in include:
            d["line_items"] = items_by_po.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 11. /api/dispatches/bulk ──────────────────────────────────────────────────
@bp.route("/dispatches/bulk")
@require_auth
def dispatches_bulk():
    """Dispatches with optional line items and PO allocations.

    Query params: supplier_id, status (CSV), date_from, date_to,
                  include (CSV: 'items,allocations'), limit (default 100, max 300)
    """
    db = get_db()
    supplier_id = request.args.get("supplier_id", type=int)
    statuses    = _csv_set(request.args.get("status"))
    date_from   = _arg_date("date_from")
    date_to     = _arg_date("date_to")
    include     = _csv_set(request.args.get("include"))
    limit       = _arg_int("limit", 100, hi=300)

    where, params = [], []
    if supplier_id is not None:
        where.append("d.supplier_id = ?"); params.append(supplier_id)
    if statuses:
        ph = ",".join("?" * len(statuses))
        where.append(f"d.status IN ({ph})"); params.extend(sorted(statuses))
    if date_from:
        where.append("d.dispatch_date >= ?"); params.append(date_from)
    if date_to:
        where.append("d.dispatch_date <= ?"); params.append(date_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        f"""SELECT d.id, d.name, d.supplier_id, s.name AS supplier_name,
                   d.dispatch_date, d.expected_arrival, d.status, d.notes, d.created_at
            FROM dispatches d
            LEFT JOIN suppliers s ON s.id = d.supplier_id
            {clause}
            ORDER BY d.dispatch_date DESC, d.id DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    d_ids = [r["id"] for r in rows]

    items_by_d = {}
    if "items" in include and d_ids:
        ph = ",".join("?" * len(d_ids))
        for r in db.execute(
            f"""SELECT di.id, di.dispatch_id, di.product_id, di.sub_product_id,
                       di.quantity, di.qty_received, di.price, di.cbm, di.gross_weight,
                       di.product_name, p.name AS product_db_name, sp.name AS sub_db_name
                FROM dispatch_items di
                LEFT JOIN products p ON p.id = di.product_id
                LEFT JOIN sub_products sp ON sp.id = di.sub_product_id
                WHERE di.dispatch_id IN ({ph})""",
            d_ids,
        ).fetchall():
            d = dict(r)
            d["pending_arrival"] = _f(d["quantity"]) - _f(d["qty_received"])
            items_by_d.setdefault(r["dispatch_id"], []).append(d)

    allocs_by_d = {}
    if "allocations" in include and d_ids:
        ph = ",".join("?" * len(d_ids))
        for r in db.execute(
            f"""SELECT dpa.id, dpa.dispatch_item_id, dpa.po_item_id, dpa.quantity,
                       di.dispatch_id, poi.po_id, po.name AS po_name
                FROM dispatch_po_allocations dpa
                JOIN dispatch_items di ON di.id = dpa.dispatch_item_id
                JOIN purchase_order_items poi ON poi.id = dpa.po_item_id
                JOIN purchase_orders po ON po.id = poi.po_id
                WHERE di.dispatch_id IN ({ph})""",
            d_ids,
        ).fetchall():
            allocs_by_d.setdefault(r["dispatch_id"], []).append(dict(r))

    items = []
    for r in rows:
        d = dict(r)
        if "items" in include:
            d["line_items"] = items_by_d.get(r["id"], [])
        if "allocations" in include:
            d["po_allocations"] = allocs_by_d.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 12. /api/palm-purchases/bulk ──────────────────────────────────────────────
@bp.route("/palm-purchases/bulk")
@require_auth
def palm_purchases_bulk():
    """Palm purchases (instant warehouse stock-ins) with optional line items.

    Query params: supplier_id, date_from, date_to, include (CSV: 'items'),
                  limit (default 100, max 300)
    """
    db = get_db()
    supplier_id = request.args.get("supplier_id", type=int)
    date_from   = _arg_date("date_from")
    date_to     = _arg_date("date_to")
    include     = _csv_set(request.args.get("include"))
    limit       = _arg_int("limit", 100, hi=300)

    where, params = [], []
    if supplier_id is not None:
        where.append("pp.supplier_id = ?"); params.append(supplier_id)
    if date_from:
        where.append("pp.purchase_date >= ?"); params.append(date_from)
    if date_to:
        where.append("pp.purchase_date <= ?"); params.append(date_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        f"""SELECT pp.id, pp.name, pp.supplier_id, s.name AS supplier_name,
                   pp.purchase_date, pp.notes, pp.total_cost, pp.created_at
            FROM palm_purchases pp
            LEFT JOIN suppliers s ON s.id = pp.supplier_id
            {clause}
            ORDER BY pp.purchase_date DESC, pp.id DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    pp_ids = [r["id"] for r in rows]

    items_by_pp = {}
    if "items" in include and pp_ids:
        ph = ",".join("?" * len(pp_ids))
        for r in db.execute(
            f"""SELECT ppi.id, ppi.palm_purchase_id, ppi.product_id, ppi.sub_product_id,
                       ppi.quantity, ppi.unit_cost, ppi.notes,
                       p.name AS product_name, sp.name AS sub_name
                FROM palm_purchase_items ppi
                LEFT JOIN products p ON p.id = ppi.product_id
                LEFT JOIN sub_products sp ON sp.id = ppi.sub_product_id
                WHERE ppi.palm_purchase_id IN ({ph})""",
            pp_ids,
        ).fetchall():
            items_by_pp.setdefault(r["palm_purchase_id"], []).append(dict(r))

    items = []
    for r in rows:
        d = dict(r)
        if "items" in include:
            d["line_items"] = items_by_pp.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ── 13. /api/supply-pipeline ──────────────────────────────────────────────────
@bp.route("/supply-pipeline")
@require_auth
def supply_pipeline():
    """For each product/sub-product, the open PO qty + in-transit qty + last received date.
    Answers: "what's already coming so I don't double-order?" in one call.

    Query params: product_ids, sub_product_ids, category_id, limit (default 300, max 1000)
    """
    db = get_db()
    product_ids = _csv_ints(request.args.get("product_ids"))
    sub_ids     = _csv_ints(request.args.get("sub_product_ids"))
    category_id = request.args.get("category_id", type=int)
    limit       = _arg_int("limit", 300, hi=1000)

    if category_id is not None and not product_ids:
        product_ids = [r["id"] for r in db.execute(
            "SELECT id FROM products WHERE category_id=?", (category_id,)
        ).fetchall()]

    if not product_ids and not sub_ids:
        return jsonify({"error": "Specify product_ids, sub_product_ids, or category_id."}), 400

    def _scope(prefix):
        parts, params = [], []
        if product_ids:
            ph = ",".join("?" * len(product_ids))
            parts.append(f"{prefix}.product_id IN ({ph})"); params.extend(product_ids)
        if sub_ids:
            ph = ",".join("?" * len(sub_ids))
            parts.append(f"{prefix}.sub_product_id IN ({ph})"); params.extend(sub_ids)
        # When both lists are provided, match EITHER (OR). When only one, that's the only filter.
        joiner = " OR " if (product_ids and sub_ids) else " AND "
        return ("(" + joiner.join(parts) + ")"), params

    # Open PO pending qty
    poi_scope, poi_params = _scope("poi")
    po_rows = db.execute(
        f"""SELECT poi.product_id, poi.sub_product_id,
                   SUM(poi.quantity - poi.qty_dispatched) AS pending_po,
                   COUNT(DISTINCT po.id) AS open_po_count
            FROM purchase_order_items poi
            JOIN purchase_orders po ON po.id = poi.po_id
            WHERE po.status = 'open' AND poi.quantity > poi.qty_dispatched
              AND {poi_scope}
            GROUP BY poi.product_id, poi.sub_product_id""",
        poi_params,
    ).fetchall()

    # In-transit dispatch qty (not yet received)
    di_scope, di_params = _scope("di")
    di_rows = db.execute(
        f"""SELECT di.product_id, di.sub_product_id,
                   SUM(di.quantity - di.qty_received) AS in_transit,
                   COUNT(DISTINCT d.id) AS in_transit_dispatch_count,
                   MIN(d.expected_arrival) AS next_arrival
            FROM dispatch_items di
            JOIN dispatches d ON d.id = di.dispatch_id
            WHERE d.status IN ('in_transit','partially_received')
              AND di.quantity > di.qty_received
              AND {di_scope}
            GROUP BY di.product_id, di.sub_product_id""",
        di_params,
    ).fetchall()

    # Last palm-purchase date
    ppi_scope, ppi_params = _scope("ppi")
    last_pp = db.execute(
        f"""SELECT ppi.product_id, ppi.sub_product_id, MAX(pp.purchase_date) AS last_date
            FROM palm_purchase_items ppi
            JOIN palm_purchases pp ON pp.id = ppi.palm_purchase_id
            WHERE {ppi_scope}
            GROUP BY ppi.product_id, ppi.sub_product_id""",
        ppi_params,
    ).fetchall()

    # Merge into one keyed map
    pipeline = {}
    def _key(pid, spid):
        return (pid, spid)

    for r in po_rows:
        k = _key(r["product_id"], r["sub_product_id"])
        pipeline.setdefault(k, {})["pending_po"]    = _f(r["pending_po"])
        pipeline[k]["open_po_count"]               = r["open_po_count"]
    for r in di_rows:
        k = _key(r["product_id"], r["sub_product_id"])
        pipeline.setdefault(k, {})["in_transit"]   = _f(r["in_transit"])
        pipeline[k]["in_transit_dispatch_count"]   = r["in_transit_dispatch_count"]
        pipeline[k]["next_arrival"]                = r["next_arrival"]
    for r in last_pp:
        k = _key(r["product_id"], r["sub_product_id"])
        pipeline.setdefault(k, {})["last_palm_purchase_date"] = r["last_date"]

    # Look up names for nicer payload
    all_pids = {k[0] for k in pipeline if k[0]}
    all_sids = {k[1] for k in pipeline if k[1]}
    pname = {}
    sname = {}
    if all_pids:
        ph = ",".join("?" * len(all_pids))
        for r in db.execute(f"SELECT id, name FROM products WHERE id IN ({ph})", list(all_pids)):
            pname[r["id"]] = r["name"]
    if all_sids:
        ph = ",".join("?" * len(all_sids))
        for r in db.execute(f"SELECT id, name FROM sub_products WHERE id IN ({ph})", list(all_sids)):
            sname[r["id"]] = r["name"]

    items = []
    for (pid, spid), data in pipeline.items():
        items.append({
            "product_id":              pid,
            "product_name":            pname.get(pid),
            "sub_product_id":          spid,
            "sub_product_name":        sname.get(spid) if spid else None,
            "pending_po":              data.get("pending_po", 0.0),
            "open_po_count":           data.get("open_po_count", 0),
            "in_transit":              data.get("in_transit", 0.0),
            "in_transit_dispatch_count": data.get("in_transit_dispatch_count", 0),
            "next_arrival":            data.get("next_arrival"),
            "last_palm_purchase_date": data.get("last_palm_purchase_date"),
        })
    truncated = len(items) > limit
    items = items[:limit]
    return jsonify({"result": {"items": items, "count": len(items), "truncated": truncated, "limit": limit}})


# ═════════════════════════════════════════════════════════════════════════════
# AGGREGATE / DETAIL ENDPOINTS (added for richer MCP coverage)
# ═════════════════════════════════════════════════════════════════════════════

def _ledger_payload(db, client_id, company_id=None, date_from=None, date_to=None):
    """Build a structured ledger (entries + running balance) for a client or one of
    their companies. Mirrors the JSON branch of /api/clients/<id>/ledger.
    Returns None if the client/company doesn't exist.
    """
    client = db.execute("SELECT name, opening_balance FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return None
    if company_id:
        co = db.execute(
            "SELECT name, opening_balance FROM client_companies WHERE id=? AND client_id=?",
            (company_id, client_id),
        ).fetchone()
        if not co:
            return None
        ledger_name = f"{client['name']} / {co['name']}"
        ob = _f(co["opening_balance"])
        inv_where, inv_params = "WHERE client_id=? AND company_id=? AND status != 'cancelled'", [client_id, company_id]
        pay_where, pay_params = "WHERE client_id=? AND company_id=?", [client_id, company_id]
    else:
        ledger_name = client["name"]
        ob = _f(client["opening_balance"])
        inv_where, inv_params = "WHERE client_id=? AND status != 'cancelled'", [client_id]
        pay_where, pay_params = "WHERE client_id=?", [client_id]

    if date_from:
        inv_where += " AND issue_date >= ?";   inv_params.append(date_from)
        pay_where += " AND payment_date >= ?"; pay_params.append(date_from)
    if date_to:
        inv_where += " AND issue_date <= ?";   inv_params.append(date_to)
        pay_where += " AND payment_date <= ?"; pay_params.append(date_to)

    invoices = db.execute(
        f"SELECT invoice_number, issue_date, total, status FROM invoices {inv_where} ORDER BY issue_date, id",
        inv_params,
    ).fetchall()
    payments = db.execute(
        f"SELECT id, amount, payment_date, method, reference, notes FROM payments {pay_where} ORDER BY payment_date, id",
        pay_params,
    ).fetchall()

    entries = []
    if ob != 0 and not date_from:
        entries.append({
            "date": "", "type": "opening",
            "label": f"Opening Balance ({'debt' if ob > 0 else 'credit'})",
            "debit": ob if ob > 0 else 0, "credit": abs(ob) if ob < 0 else 0,
        })

    merged = sorted(
        [{"sort": r["issue_date"] or "",  "kind": "invoice", "row": dict(r)} for r in invoices] +
        [{"sort": r["payment_date"] or "", "kind": "payment", "row": dict(r)} for r in payments],
        key=lambda x: x["sort"],
    )
    pmt_ids = [m["row"]["id"] for m in merged if m["kind"] == "payment"]
    allocs_by_pmt = {}
    if pmt_ids:
        ph = ",".join("?" * len(pmt_ids))
        for r in db.execute(
            f"""SELECT pa.payment_id, pa.amount, i.invoice_number
                FROM payment_allocations pa JOIN invoices i ON i.id = pa.invoice_id
                WHERE pa.payment_id IN ({ph})""",
            pmt_ids,
        ).fetchall():
            allocs_by_pmt.setdefault(r["payment_id"], []).append(dict(r))

    for item in merged:
        r = item["row"]
        if item["kind"] == "invoice":
            entries.append({"date": r["issue_date"], "type": "invoice",
                            "label": r["invoice_number"], "debit": _f(r["total"]), "credit": 0})
        else:
            label = r["method"] or "payment"
            if r["reference"]:
                label += f" / {r['reference']}"
            entries.append({"date": r["payment_date"], "type": "payment", "label": label,
                            "debit": 0, "credit": _f(r["amount"]),
                            "payment_id": r["id"], "allocations": allocs_by_pmt.get(r["id"], [])})

    running = 0.0
    for e in entries:
        running += e["credit"] - e["debit"]
        e["running"] = running

    return {
        "ledger_name":   ledger_name,
        "company_id":    company_id,
        "date_from":     date_from,
        "date_to":       date_to,
        "entries":       entries,
        "final_balance": running,
    }


@bp.route("/clients/<int:client_id>/full")
@require_auth
def client_full(client_id):
    """Everything about one client in a single call: details, invoices (with line items
    + payment status), companies (each with its ledger), and the complete client ledger.

    Query params:
      invoice_days  — invoices issued within the last N days (default 30; -1 = all).
      invoice_limit — cap on invoices returned (default 500, max 2000).
    """
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return jsonify({"error": f"Client ID {client_id} not found."}), 404

    raw = request.args.get("invoice_days")
    try:
        invoice_days = int(raw) if raw is not None else 30
    except (TypeError, ValueError):
        invoice_days = 30
    invoice_limit = _arg_int("invoice_limit", 500, hi=2000)

    inv_where, inv_params = "WHERE i.client_id=?", [client_id]
    if invoice_days != -1:
        inv_where += " AND date(i.issue_date) >= date('now', ?)"
        inv_params.append(f"-{max(0, invoice_days)} days")

    invoices = db.execute(
        f"SELECT i.* FROM invoices i {inv_where} ORDER BY i.issue_date DESC, i.id DESC LIMIT ?",
        inv_params + [invoice_limit + 1],
    ).fetchall()
    inv_truncated = len(invoices) > invoice_limit
    invoices = invoices[:invoice_limit]
    inv_ids = [r["id"] for r in invoices]

    items_by_inv = {}
    if inv_ids:
        ph = ",".join("?" * len(inv_ids))
        for it in db.execute(
            f"""SELECT ii.*, p.name AS product_name, sp.name AS sub_product_name,
                       COALESCE(NULLIF(sp.pcs_per_carton, 0), p.pcs_per_carton, 0) AS pcs_per_carton
                FROM invoice_items ii
                LEFT JOIN products p ON p.id = ii.product_id
                LEFT JOIN sub_products sp ON sp.id = ii.sub_product_id
                WHERE ii.invoice_id IN ({ph})""",
            inv_ids,
        ).fetchall():
            d = dict(it)
            pcs = d.get("pcs_per_carton") or 0
            qty = _f(d.get("quantity"))
            items_by_inv.setdefault(it["invoice_id"], []).append({
                "product_id":       d["product_id"],
                "product_name":     d["product_name"] or d["description"],
                "sub_product_id":   d["sub_product_id"],
                "sub_product_name": d["sub_product_name"],
                "description":      d["description"],
                "box_size":         pcs,
                "quantity":         qty,
                "quantity_boxes":   round(qty / pcs, 3) if pcs else None,
                "unit_price":       _f(d["unit_price"]),
                "discount_type":    d.get("discount_type"),
                "discount_value":   _f(d.get("discount_value")),
                "tax_rate":         _f(d["tax_rate"]),
                "line_total":       _f(d["line_total"]),
            })

    inv_items = []
    for r in invoices:
        d = dict(r)
        total, paid = _f(d["total"]), _f(d["amount_paid"])
        inv_items.append({
            "id":             d["id"],
            "invoice_number": d["invoice_number"],
            "issue_date":     d["issue_date"],
            "due_date":       d["due_date"],
            "company_id":     d.get("company_id"),
            "subtotal":       _f(d["subtotal"]),
            "tax_total":      _f(d["tax_total"]),
            "discount_amount": _f(d["discount_amount"]),
            "total":          total,
            "amount_paid":    paid,
            "balance_due":    total - paid,
            "payment_status": d["status"],
            "items":          items_by_inv.get(d["id"], []),
        })

    companies = []
    for co in db.execute(
        "SELECT * FROM client_companies WHERE client_id=? ORDER BY name", (client_id,)
    ).fetchall():
        companies.append({
            "id":              co["id"],
            "name":            co["name"],
            "tax_id":          co["tax_id"],
            "opening_balance": _f(co["opening_balance"]),
            "ledger":          _ledger_payload(db, client_id, company_id=co["id"]),
        })

    complete_ledger = _ledger_payload(db, client_id)

    details = {
        "id":              client["id"],
        "name":            client["name"],
        "company":         client["company"],
        "email":           client["email"],
        "phone":           client["phone"],
        "address":         client["address"],
        "city":            client["city"],
        "country":         client["country"],
        "tax_id":          client["tax_id"],
        "notes":           client["notes"],
        "opening_balance": _f(client["opening_balance"]),
        "payment_terms":   client["payment_terms"] if "payment_terms" in client.keys() else None,
        "created_at":      client["created_at"],
    }

    return jsonify({"result": {
        "details":             details,
        "balance":             complete_ledger["final_balance"] if complete_ledger else 0.0,
        "invoice_window_days": invoice_days,
        "invoice_count":       len(inv_items),
        "invoices_truncated":  inv_truncated,
        "invoices":            inv_items,
        "companies":           companies,
        "ledger":              complete_ledger,
    }})


@bp.route("/categories/<int:category_id>/products")
@require_auth
def category_products(category_id):
    """All products in a category with nested sub-products, each showing bucket
    quantities (warehouse / production / transit), min_quantity, and box size.

    Query param: include_inactive=0/1 (default 0).
    """
    db = get_db()
    cat = db.execute("SELECT id, name, description FROM categories WHERE id=?", (category_id,)).fetchone()
    if not cat:
        return jsonify({"error": f"Category ID {category_id} not found."}), 404
    include_inactive = request.args.get("include_inactive", "0") in ("1", "true", "yes")

    p_where, p_params = "WHERE category_id=?", [category_id]
    if not include_inactive:
        p_where += " AND is_active=1"
    products = db.execute(
        f"""SELECT id, name, sku, unit_price, stock_qty, production_qty, in_transit_qty,
                   min_quantity, pcs_per_carton, is_active
            FROM products {p_where} ORDER BY name""",
        p_params,
    ).fetchall()
    pids = [p["id"] for p in products]

    subs_by_p = {}
    if pids:
        ph = ",".join("?" * len(pids))
        s_where, s_params = f"WHERE product_id IN ({ph})", list(pids)
        if not include_inactive:
            s_where += " AND is_active=1"
        for s in db.execute(
            f"""SELECT id, product_id, name, sku, unit_price, stock_qty, production_qty,
                       in_transit_qty, min_quantity, pcs_per_carton, is_active
                FROM sub_products {s_where} ORDER BY name""",
            s_params,
        ).fetchall():
            subs_by_p.setdefault(s["product_id"], []).append({
                "sub_product_id": s["id"],
                "name":           s["name"],
                "sku":            s["sku"],
                "box_size":       s["pcs_per_carton"] or 0,
                "quantities": {
                    "warehouse":  _f(s["stock_qty"]),
                    "production": _f(s["production_qty"]),
                    "transit":    _f(s["in_transit_qty"]),
                },
                "min_quantity": _f(s["min_quantity"]),
                "is_active":    bool(s["is_active"]),
            })

    items = []
    for p in products:
        subs = subs_by_p.get(p["id"], [])
        items.append({
            "product_id": p["id"],
            "name":       p["name"],
            "sku":        p["sku"],
            "box_size":   p["pcs_per_carton"] or 0,
            "quantities": {
                "warehouse":  _f(p["stock_qty"]),
                "production": _f(p["production_qty"]),
                "transit":    _f(p["in_transit_qty"]),
            },
            "min_quantity":     _f(p["min_quantity"]),
            "is_active":        bool(p["is_active"]),
            "has_sub_products": bool(subs),
            "sub_products":     subs,
        })

    return jsonify({"result": {
        "category_id":   cat["id"],
        "category_name": cat["name"],
        "count":         len(items),
        "products":      items,
    }})


@bp.route("/products/<int:product_id>/stock-history-grouped")
@require_auth
def product_stock_history_grouped(product_id):
    """Stock-movement history for a product, grouped by sub-product.
    Defaults to the WAREHOUSE bucket; pass bucket=production|transit|all.
    Products with no sub-products return a single product-level group.

    Query params: bucket (default 'warehouse'), limit (per group, default 50, max 500).
    """
    db = get_db()
    prod = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
    if not prod:
        return jsonify({"error": f"Product {product_id} not found"}), 404

    bucket = (request.args.get("bucket") or "warehouse").strip().lower()
    limit = _arg_int("limit", 50, hi=500)
    types = None
    if bucket not in ("all", ""):
        if bucket not in _BUCKET_MOVEMENT_TYPES:
            return jsonify({"error": "bucket must be one of: warehouse, production, transit, all"}), 400
        types = _BUCKET_MOVEMENT_TYPES[bucket]

    def _hist(extra_where, extra_params):
        where, params = ["product_id = ?"] + extra_where, [product_id] + extra_params
        if types:
            ph = ",".join("?" * len(types))
            where.append(f"movement_type IN ({ph})"); params.extend(types)
        rows = db.execute(
            f"""SELECT id, sub_product_id, movement_type, quantity, notes, invoice_id,
                       dispatch_id, palm_purchase_id, expected_arrival, created_at
                FROM stock_movements WHERE {' AND '.join(where)}
                ORDER BY created_at DESC, id DESC LIMIT ?""",
            params + [limit],
        ).fetchall()
        return [{
            "id":               r["id"],
            "type":             r["movement_type"],
            "quantity":         _f(r["quantity"]),
            "notes":            r["notes"],
            "invoice_id":       r["invoice_id"],
            "dispatch_id":      r["dispatch_id"],
            "palm_purchase_id": r["palm_purchase_id"],
            "expected_arrival": r["expected_arrival"],
            "sub_product_id":   r["sub_product_id"],
            "created_at":       r["created_at"],
        } for r in rows]

    subs = db.execute(
        "SELECT id, name, stock_qty, production_qty, in_transit_qty "
        "FROM sub_products WHERE product_id=? ORDER BY name", (product_id,)
    ).fetchall()

    groups = []
    if subs:
        for s in subs:
            h = _hist(["sub_product_id = ?"], [s["id"]])
            groups.append({
                "sub_product_id":   s["id"],
                "sub_product_name": s["name"],
                "current": {"warehouse": _f(s["stock_qty"]), "production": _f(s["production_qty"]), "transit": _f(s["in_transit_qty"])},
                "history": h,
                "count":   len(h),
            })
        parent_hist = _hist(["sub_product_id IS NULL"], [])
        if parent_hist:
            prow = db.execute("SELECT stock_qty, production_qty, in_transit_qty FROM products WHERE id=?", (product_id,)).fetchone()
            groups.append({
                "sub_product_id":   None,
                "sub_product_name": "(product-level / no variant)",
                "current": {"warehouse": _f(prow["stock_qty"]), "production": _f(prow["production_qty"]), "transit": _f(prow["in_transit_qty"])},
                "history": parent_hist,
                "count":   len(parent_hist),
            })
    else:
        prow = db.execute("SELECT stock_qty, production_qty, in_transit_qty FROM products WHERE id=?", (product_id,)).fetchone()
        h = _hist([], [])
        groups.append({
            "sub_product_id":   None,
            "sub_product_name": None,
            "current": {"warehouse": _f(prow["stock_qty"]), "production": _f(prow["production_qty"]), "transit": _f(prow["in_transit_qty"])},
            "history": h,
            "count":   len(h),
        })

    return jsonify({"result": {
        "product_id":       product_id,
        "product_name":     prod["name"],
        "bucket_filter":    bucket,
        "has_sub_products": bool(subs),
        "group_count":      len(groups),
        "groups":           groups,
        "limit":            limit,
    }})


@bp.route("/products/<int:product_id>/stock-action", methods=["POST"])
@require_auth
def product_stock_action(product_id):
    """Perform a semantic stock MOVE (transfers between buckets / adds stock).

    Body: {action, quantity, sub_product_id?, expected_arrival?, notes?}
      action: add_stock                 → warehouse += qty
              send_to_production         → warehouse → production
              dispatch_from_production   → production → transit (needs expected_arrival)
              mark_arrived               → transit → warehouse
    """
    data = _jb()
    action = (data.get("action") or "").strip()
    quantity = _f(data.get("quantity", 0))
    sub_product_id = data.get("sub_product_id") or None
    notes = data.get("notes") or "Stock action via Claude"
    expected_arrival = data.get("expected_arrival") or None

    valid = {"add_stock", "send_to_production", "dispatch_from_production", "mark_arrived"}
    if action not in valid:
        return jsonify({"error": f"action must be one of: {', '.join(sorted(valid))}"}), 400
    if quantity <= 0:
        return jsonify({"error": "quantity must be positive."}), 400

    db = get_db()
    if sub_product_id:
        row = db.execute("SELECT name FROM sub_products WHERE id=?", (sub_product_id,)).fetchone()
        if not row:
            return jsonify({"error": f"Sub-product ID {sub_product_id} not found."}), 404
        name = row["name"]
    else:
        row = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return jsonify({"error": f"Product ID {product_id} not found."}), 404
        name = row["name"]

    if action == "dispatch_from_production":
        if not expected_arrival:
            return jsonify({"error": "expected_arrival (YYYY-MM-DD) is required for dispatch_from_production."}), 400
        try:
            datetime.strptime(expected_arrival, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "expected_arrival must be YYYY-MM-DD."}), 400

    from ..services import product_service
    sid = int(sub_product_id) if sub_product_id else None
    if action == "add_stock":
        product_service.add_stock(product_id, sid, quantity, notes)
        desc = "added to warehouse"
    elif action == "send_to_production":
        product_service.send_to_production(product_id, sid, quantity, notes)
        desc = "moved warehouse → production"
    elif action == "dispatch_from_production":
        product_service.dispatch_from_production(product_id, sid, quantity, expected_arrival, notes)
        desc = f"moved production → transit (ETA {expected_arrival})"
    else:
        product_service.mark_arrived(product_id, sid, quantity, notes)
        desc = "moved transit → warehouse"

    return jsonify({"result": f"✓ {name}: {quantity:.0f} {desc}."})


def _dispatch_items_map(db, d_ids):
    items_by_d = {}
    if not d_ids:
        return items_by_d
    ph = ",".join("?" * len(d_ids))
    for r in db.execute(
        f"""SELECT di.id, di.dispatch_id, di.product_id, di.sub_product_id,
                   di.quantity, di.qty_received, di.price, di.product_name,
                   p.name AS product_db_name, sp.name AS sub_db_name
            FROM dispatch_items di
            LEFT JOIN products p ON p.id = di.product_id
            LEFT JOIN sub_products sp ON sp.id = di.sub_product_id
            WHERE di.dispatch_id IN ({ph})""",
        d_ids,
    ).fetchall():
        d = dict(r)
        d["display_name"] = d["sub_db_name"] or d["product_db_name"] or d["product_name"]
        d["pending"] = _f(d["quantity"]) - _f(d["qty_received"])
        items_by_d.setdefault(r["dispatch_id"], []).append(d)
    return items_by_d


@bp.route("/transit/received")
@require_auth
def transit_received():
    """Dispatches that have arrived (status received / partially_received).
    Without a date range, returns the single latest arrival; with a range,
    returns all in that window. Ordering & date filters use expected_arrival.

    Query params: date_from, date_to (YYYY-MM-DD), include=items, limit.
    """
    db = get_db()
    date_from, date_to = _arg_date("date_from"), _arg_date("date_to")
    include = _csv_set(request.args.get("include"))
    has_range = bool(date_from or date_to)
    limit = _arg_int("limit", 50 if has_range else 1, hi=300)

    where, params = ["d.status IN ('received','partially_received')"], []
    if date_from:
        where.append("d.expected_arrival >= ?"); params.append(date_from)
    if date_to:
        where.append("d.expected_arrival <= ?"); params.append(date_to)

    rows = db.execute(
        f"""SELECT d.id, d.name, d.supplier_id, s.name AS supplier_name,
                   d.dispatch_date, d.expected_arrival, d.status, d.notes, d.created_at
            FROM dispatches d LEFT JOIN suppliers s ON s.id = d.supplier_id
            WHERE {' AND '.join(where)}
            ORDER BY d.expected_arrival DESC, d.id DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items_by_d = _dispatch_items_map(db, [r["id"] for r in rows]) if "items" in include else {}

    items = []
    for r in rows:
        d = dict(r)
        if "items" in include:
            d["line_items"] = items_by_d.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {
        "items": items, "count": len(items), "truncated": truncated, "limit": limit,
        "note": "received = dispatches fully/partially received; date filter & ordering use expected_arrival",
    }})


@bp.route("/transit/upcoming")
@require_auth
def transit_upcoming():
    """Dispatches still arriving (status in_transit / partially_received).
    Without a date range, returns the single next arrival (expected_arrival >= today);
    with a range, returns all expected in that window. Ordered by expected_arrival ASC.

    Query params: date_from, date_to (YYYY-MM-DD), include=items, limit.
    """
    db = get_db()
    date_from, date_to = _arg_date("date_from"), _arg_date("date_to")
    include = _csv_set(request.args.get("include"))
    has_range = bool(date_from or date_to)
    limit = _arg_int("limit", 50 if has_range else 1, hi=300)

    where, params = ["d.status IN ('in_transit','partially_received')"], []
    if has_range:
        if date_from:
            where.append("d.expected_arrival >= ?"); params.append(date_from)
        if date_to:
            where.append("d.expected_arrival <= ?"); params.append(date_to)
    else:
        where.append("(d.expected_arrival IS NULL OR d.expected_arrival >= date('now'))")

    rows = db.execute(
        f"""SELECT d.id, d.name, d.supplier_id, s.name AS supplier_name,
                   d.dispatch_date, d.expected_arrival, d.status, d.notes, d.created_at
            FROM dispatches d LEFT JOIN suppliers s ON s.id = d.supplier_id
            WHERE {' AND '.join(where)}
            ORDER BY (d.expected_arrival IS NULL), d.expected_arrival ASC, d.id ASC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    items_by_d = _dispatch_items_map(db, [r["id"] for r in rows]) if "items" in include else {}

    items = []
    for r in rows:
        d = dict(r)
        if "items" in include:
            d["line_items"] = items_by_d.get(r["id"], [])
        items.append(d)
    return jsonify({"result": {
        "items": items, "count": len(items), "truncated": truncated, "limit": limit,
    }})

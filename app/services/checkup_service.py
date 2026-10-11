"""Checkup: everything sold from one invoice serial onwards, client by client.

The owner names an invoice ("from INV-0150 onwards") and gets, for every client
that was invoiced in that range, the invoices (serial order, with line items),
a ledger that opens with the balance the client carried into the checkup, and
what they bought product by product.

Rules (the same ones the page documents):

* The cutoff is by invoice **serial** (the number after ``INV-``), never by date.
  Two live invoices are dated out of serial order, so a date filter would drift.
* Drafts (``D-###``) have no serial and never appear; cancelled invoices are
  excluded exactly as the client ledger already excludes them.
* Payments carry no serial, so they join the window by **date**: on/after the
  cutoff invoice's issue date unless ``payments_from`` overrides it.
* Balance brought forward = opening balance + every serial below the cutoff +
  every payment (and manual ledger entry) dated before ``payments_from``.
  Sign convention is the client ledger's: negative = the client owes us.
* A client is listed only when it has at least one invoice in the range.
"""
import re
from datetime import date

from ..database import get_db

_DIGITS = re.compile(r"(\d+)")

# Serial of an issued invoice: 'INV-0060' -> 60. Drafts ('D-012') don't match.
_SEQ_SQL = "CAST(SUBSTR(invoice_number, 5) AS INTEGER)"
_ISSUED_SQL = "invoice_number LIKE 'INV-%'"


def parse_serial(value):
    """'INV-0060', 'inv 60', '60' -> 60. None when there are no digits."""
    if value is None:
        return None
    m = _DIGITS.search(str(value))
    return int(m.group(1)) if m else None


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _r2(v):
    return round(float(v), 2)


def _in(ids):
    ids = list(ids)
    return "(" + ",".join("?" * len(ids)) + ")", ids


def _invoice_by_serial(db, seq):
    return db.execute(
        f"SELECT id, invoice_number, issue_date, client_id, status FROM invoices "
        f"WHERE {_ISSUED_SQL} AND {_SEQ_SQL} = ? LIMIT 1",
        (seq,),
    ).fetchone()


def latest_serial(db=None):
    """Highest issued serial, or None when nothing has been invoiced yet."""
    db = db or get_db()
    row = db.execute(
        f"SELECT invoice_number FROM invoices WHERE {_ISSUED_SQL} "
        f"ORDER BY {_SEQ_SQL} DESC LIMIT 1"
    ).fetchone()
    return parse_serial(row["invoice_number"]) if row else None


def invoice_index(client_ids=None):
    """Light list of issued invoices for the page's serial picker:
    [{invoice_number, issue_date, client_name}] newest first."""
    db = get_db()
    where, params = f"WHERE i.{_ISSUED_SQL}", []
    if client_ids is not None:
        ph, ids = _in(client_ids)
        if not ids:
            return []
        where += f" AND i.client_id IN {ph}"
        params += ids
    rows = db.execute(
        f"SELECT i.invoice_number, i.issue_date, c.name AS client_name "
        f"FROM invoices i JOIN clients c ON c.id = i.client_id {where} "
        f"ORDER BY {_SEQ_SQL} DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def build_checkup(from_invoice, inclusive=True, to_invoice=None, payments_from=None,
                  client_ids=None, include_items=True, include_products=True, sort="name"):
    """Return the checkup dict ({cutoff, summary, clients}) or None when the
    cutoff invoice does not exist. Raises ValueError for unusable arguments.

    client_ids: optional iterable restricting the clients (row-scoped users pass
    their scope). An empty list yields an empty checkup.
    sort: 'name' (A-Z, case-insensitive, trimmed) or 'invoice' (by each client's
    first serial in range, so the page follows the invoice book).
    """
    db = get_db()
    today = date.today().isoformat()

    from_seq = parse_serial(from_invoice)
    if not from_seq:
        raise ValueError("from_invoice is required (e.g. INV-0150).")
    from_row = _invoice_by_serial(db, from_seq)
    if not from_row:
        return None

    if to_invoice not in (None, ""):
        to_seq = parse_serial(to_invoice)
        to_row = _invoice_by_serial(db, to_seq) if to_seq else None
        if not to_row:
            raise ValueError(f"No invoice matches to_invoice={to_invoice!r}.")
        if to_seq < from_seq:
            raise ValueError("to_invoice must not be below from_invoice.")
        pay_to = to_row["issue_date"]
    else:
        to_seq = latest_serial(db) or from_seq
        to_row = _invoice_by_serial(db, to_seq)
        pay_to = None

    lo = from_seq if inclusive else from_seq + 1
    hi = to_seq
    pay_from = payments_from or from_row["issue_date"]
    if sort not in ("name", "invoice"):
        sort = "name"

    scope_sql, scope_params = "", []
    if client_ids is not None:
        ph, ids = _in(client_ids)
        if not ids:
            return _empty(from_row, to_row, inclusive, pay_from, pay_to, sort, today)
        scope_sql, scope_params = f" AND i.client_id IN {ph}", ids

    # -- Invoices in range, serial order ------------------------------------
    inv_rows = db.execute(
        f"""SELECT i.*, cc.name AS company_name
            FROM invoices i LEFT JOIN client_companies cc ON cc.id = i.company_id
            WHERE i.{_ISSUED_SQL} AND {_SEQ_SQL} BETWEEN ? AND ?
              AND i.status != 'cancelled'{scope_sql}
            ORDER BY {_SEQ_SQL}""",
        [lo, hi] + scope_params,
    ).fetchall()
    if not inv_rows:
        return _empty(from_row, to_row, inclusive, pay_from, pay_to, sort, today)

    inv_by_client = {}
    for r in inv_rows:
        inv_by_client.setdefault(r["client_id"], []).append(dict(r))
    cids = list(inv_by_client)
    ph, ids = _in(cids)

    clients = {r["id"]: dict(r) for r in db.execute(
        f"SELECT id, name, opening_balance, payment_terms FROM clients WHERE id IN {ph}", ids
    ).fetchall()}
    companies = {}
    for r in db.execute(
        f"SELECT id, client_id, name, opening_balance FROM client_companies "
        f"WHERE client_id IN {ph} ORDER BY id", ids
    ).fetchall():
        companies.setdefault(r["client_id"], []).append({"company_id": r["id"], "name": r["name"]})

    # -- Everything before the line (per client) -----------------------------
    before_inv = {r["client_id"]: _f(r["v"]) for r in db.execute(
        f"SELECT client_id, SUM(total) AS v FROM invoices "
        f"WHERE {_ISSUED_SQL} AND {_SEQ_SQL} < ? AND status != 'cancelled' "
        f"AND client_id IN {ph} GROUP BY client_id", [lo] + ids
    ).fetchall()}
    before_pay = {r["client_id"]: _f(r["v"]) for r in db.execute(
        f"SELECT client_id, SUM(amount) AS v FROM payments "
        f"WHERE payment_date < ? AND client_id IN {ph} GROUP BY client_id", [pay_from] + ids
    ).fetchall()}
    before_manual = {r["client_id"]: _f(r["v"]) for r in db.execute(
        f"SELECT client_id, SUM(COALESCE(credit,0) - COALESCE(debit,0)) AS v FROM ledger_entries "
        f"WHERE entry_date < ? AND client_id IN {ph} GROUP BY client_id", [pay_from] + ids
    ).fetchall()}

    # -- Payments and manual entries inside the window -----------------------
    pay_where, pay_params = f"p.client_id IN {ph} AND p.payment_date >= ?", ids + [pay_from]
    man_where, man_params = f"client_id IN {ph} AND entry_date >= ?", ids + [pay_from]
    if pay_to:
        pay_where += " AND p.payment_date <= ?"; pay_params.append(pay_to)
        man_where += " AND entry_date <= ?";     man_params.append(pay_to)
    pay_by_client = {}
    for r in db.execute(
        f"""SELECT p.id, p.client_id, p.company_id, p.amount, p.payment_date, p.method,
                   p.reference, p.notes, p.is_opening_balance, cc.name AS company_name,
                   (SELECT GROUP_CONCAT(i.invoice_number, ', ')
                      FROM payment_allocations pa JOIN invoices i ON i.id = pa.invoice_id
                     WHERE pa.payment_id = p.id) AS invoice_numbers
            FROM payments p LEFT JOIN client_companies cc ON cc.id = p.company_id
            WHERE {pay_where} ORDER BY p.payment_date, p.id""",
        pay_params,
    ).fetchall():
        pay_by_client.setdefault(r["client_id"], []).append(dict(r))
    man_by_client = {}
    for r in db.execute(
        f"SELECT * FROM ledger_entries WHERE {man_where} ORDER BY entry_date, id", man_params
    ).fetchall():
        man_by_client.setdefault(r["client_id"], []).append(dict(r))

    # -- Line items (needed for products even when items are not emitted) ----
    items_by_inv = {}
    if include_items or include_products:
        inv_ids = [r["id"] for r in inv_rows]
        iph, iids = _in(inv_ids)
        for r in db.execute(
            f"""SELECT ii.*, p.name AS product_name, sp.name AS sub_product_name,
                       COALESCE(NULLIF(sp.pcs_per_carton, 0), p.pcs_per_carton, 0) AS pcs_per_carton
                FROM invoice_items ii
                LEFT JOIN products p ON p.id = ii.product_id
                LEFT JOIN sub_products sp ON sp.id = ii.sub_product_id
                WHERE ii.invoice_id IN {iph} ORDER BY ii.id""",
            iids,
        ).fetchall():
            d = dict(r)
            pcs = _f(d.get("pcs_per_carton"))
            qty = _f(d.get("quantity"))
            items_by_inv.setdefault(d["invoice_id"], []).append({
                "product_id":       d["product_id"],
                "sub_product_id":   d["sub_product_id"],
                "product_name":     d["product_name"] or d["description"],
                "sub_product_name": d["sub_product_name"],
                "sku":              d.get("sku"),
                "description":      d["description"],
                "quantity":         qty,
                "box_size":         pcs,
                "quantity_boxes":   round(qty / pcs, 3) if pcs else None,
                "unit_price":       _f(d["unit_price"]),
                "discount_type":    d.get("discount_type") or "percent",
                "discount_value":   _f(d.get("discount_value")),
                "line_total":       _f(d["line_total"]),
            })

    # -- Assemble per client --------------------------------------------------
    out = []
    for cid, invs in inv_by_client.items():
        c = clients.get(cid)
        if not c:
            continue
        opening = _f(c["opening_balance"])
        bbf = _r2(-opening - before_inv.get(cid, 0.0) + before_pay.get(cid, 0.0)
                  + before_manual.get(cid, 0.0))

        merged = (
            [{"d": r["issue_date"] or "", "k": "invoice", "s": _seq(r["invoice_number"]), "r": r} for r in invs] +
            [{"d": r["payment_date"] or "", "k": "payment", "s": 10**7 + r["id"], "r": r} for r in pay_by_client.get(cid, [])] +
            [{"d": r["entry_date"] or "", "k": "manual",  "s": 2 * 10**7 + r["id"], "r": r} for r in man_by_client.get(cid, [])]
        )
        merged.sort(key=lambda m: (m["d"], m["s"]))

        running = bbf
        entries = [{
            "date":    from_row["issue_date"], "type": "bbf",
            "label":   "Balance brought forward",
            "note":    (f"opening balance + every invoice before {from_row['invoice_number']}"
                        if inclusive else
                        f"opening balance + every invoice up to and including {from_row['invoice_number']}")
                       + f" + payments before {pay_from}",
            "debit":   _r2(-bbf) if bbf < 0 else 0.0,
            "credit":  bbf if bbf > 0 else 0.0,
            "running": bbf,
        }]
        for m in merged:
            r = m["r"]
            if m["k"] == "invoice":
                running = _r2(running - _f(r["total"]))
                entries.append({
                    "date": r["issue_date"], "type": "invoice",
                    "label": r["invoice_number"], "company": r.get("company_name") or "",
                    "company_id": r.get("company_id"), "invoice_id": r["id"],
                    "debit": _f(r["total"]), "credit": 0.0, "running": running,
                })
            elif m["k"] == "payment":
                running = _r2(running + _f(r["amount"]))
                parts = [p for p in (r["method"], r["reference"], r["notes"]) if p]
                is_ob = bool(r.get("is_opening_balance"))
                if is_ob:
                    note = "against opening balance"
                elif r.get("invoice_numbers"):
                    note = f"applied to {r['invoice_numbers']}"
                else:
                    note = "on account"
                entries.append({
                    "date": r["payment_date"], "type": "payment",
                    "label": " · ".join(parts) if parts else "Payment", "note": note,
                    "company": r.get("company_name") or "", "company_id": r.get("company_id"),
                    "payment_id": r["id"], "method": r["method"], "reference": r["reference"],
                    "invoice_numbers": r.get("invoice_numbers"), "is_opening_balance": is_ob,
                    "debit": 0.0, "credit": _f(r["amount"]), "running": running,
                })
            else:
                debit, credit = _f(r.get("debit")), _f(r.get("credit"))
                running = _r2(running + credit - debit)
                entries.append({
                    "date": r["entry_date"], "type": "manual",
                    "label": r.get("description") or "Manual entry",
                    "debit": debit, "credit": credit, "running": running,
                })

        inv_out, prod = [], {}
        for r in invs:
            total, paid = _f(r["total"]), _f(r["amount_paid"])
            overdue = bool(r["due_date"] and r["due_date"] < today and r["status"] not in ("paid", "cancelled"))
            items = items_by_inv.get(r["id"], [])
            for it in items:
                if it["product_id"] is None:
                    continue
                g = prod.setdefault(it["product_id"], {
                    "product_id": it["product_id"], "name": it["product_name"],
                    "lines": 0, "total_qty": 0.0, "total_boxes": 0.0,
                    "has_box_size": False, "total_amount": 0.0,
                })
                g["lines"] += 1
                g["total_qty"] += it["quantity"]
                if it["box_size"]:
                    g["has_box_size"] = True
                    g["total_boxes"] += it["quantity"] / it["box_size"]
                g["total_amount"] = _r2(g["total_amount"] + it["line_total"])
            row = {
                "invoice_id": r["id"], "invoice_number": r["invoice_number"],
                "seq": _seq(r["invoice_number"]),
                "issue_date": r["issue_date"], "due_date": r["due_date"],
                "company_id": r.get("company_id"), "company": r.get("company_name") or "",
                "status": r["status"], "overdue": overdue,
                "subtotal": _f(r["subtotal"]), "tax_total": _f(r["tax_total"]),
                "discount_amount": _f(r["discount_amount"]),
                "total": total, "amount_paid": paid, "balance_due": _r2(total - paid),
            }
            if include_items:
                row["items"] = items
            inv_out.append(row)

        products = sorted(prod.values(), key=lambda p: (-p["total_amount"], p["name"]))
        for p in products:
            p["total_boxes"] = round(p["total_boxes"], 3)
        pays = pay_by_client.get(cid, [])
        sales = _r2(sum(i["total"] for i in inv_out))
        out.append({
            "client_id": cid, "name": (c["name"] or "").strip(),
            "payment_terms": c["payment_terms"] or 0,
            "companies": companies.get(cid, []),
            "totals": {
                "invoice_count": len(inv_out), "first_invoice": inv_out[0]["invoice_number"],
                "sales": sales,
                "collected": _r2(sum(_f(p["amount"]) for p in pays)),
                "outstanding": _r2(sum(i["balance_due"] for i in inv_out)),
                "overdue_count": sum(1 for i in inv_out if i["overdue"]),
                "payment_count": len(pays),
            },
            "balance": {"brought_forward": bbf, "closing": running, "as_of": pay_to or today},
            "invoices": inv_out,
            "ledger": {"entries": entries, "final_balance": running},
            "products": products if include_products else [],
            "_first_seq": inv_out[0]["seq"],
        })

    if sort == "invoice":
        out.sort(key=lambda c: c["_first_seq"])
    else:
        out.sort(key=lambda c: (c["name"].casefold(), c["client_id"]))
    for c in out:
        del c["_first_seq"]

    all_inv = [i for c in out for i in c["invoices"]]
    return {
        "cutoff": _cutoff(from_row, to_row, inclusive, pay_from, pay_to, sort, today),
        "summary": {
            "invoice_count":        len(all_inv),
            "sales_total":          _r2(sum(i["total"] for i in all_inv)),
            "collected_total":      _r2(sum(c["totals"]["collected"] for c in out)),
            "payment_count":        sum(c["totals"]["payment_count"] for c in out),
            "outstanding_on_range": _r2(sum(i["balance_due"] for i in all_inv)),
            "overdue_count":        sum(1 for i in all_inv if i["overdue"]),
            "client_count":         len(out),
        },
        "clients": out,
    }


def _seq(invoice_number):
    return parse_serial(invoice_number) or 0


def _cutoff(from_row, to_row, inclusive, pay_from, pay_to, sort, today):
    return {
        "from_invoice":  from_row["invoice_number"],
        "from_seq":      _seq(from_row["invoice_number"]),
        "inclusive":     bool(inclusive),
        "from_date":     from_row["issue_date"],
        "from_client_id": from_row["client_id"],
        "to_invoice":    to_row["invoice_number"] if to_row else None,
        "to_seq":        _seq(to_row["invoice_number"]) if to_row else None,
        "to_date":       to_row["issue_date"] if to_row else None,
        "payments_from": pay_from,
        "payments_to":   pay_to,
        "sort":          sort,
        "generated_on":  today,
    }


def _empty(from_row, to_row, inclusive, pay_from, pay_to, sort, today):
    return {
        "cutoff": _cutoff(from_row, to_row, inclusive, pay_from, pay_to, sort, today),
        "summary": {"invoice_count": 0, "sales_total": 0.0, "collected_total": 0.0,
                    "payment_count": 0, "outstanding_on_range": 0.0, "overdue_count": 0,
                    "client_count": 0},
        "clients": [],
    }


def checkup_from_args(args, client_ids=None):
    """Shared by the page's /checkup/data and the assistant's /api/checkup.
    ``args`` is any mapping of query params. Returns (payload, http_status)."""
    def _bool(v, default=True):
        if v is None or str(v).strip() == "":
            return default
        return str(v).strip().lower() not in ("0", "false", "no", "off")

    pay_from = (args.get("payments_from") or "").strip() or None
    if pay_from and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pay_from):
        return {"error": "payments_from must be YYYY-MM-DD."}, 400
    raw_include = args.get("include")
    if raw_include is None:
        raw_include = "items,products"          # absent = expand everything; "" = neither
    include = {s.strip().lower() for s in raw_include.split(",") if s.strip()}
    from_invoice = args.get("from_invoice") or args.get("from")
    try:
        result = build_checkup(
            from_invoice,
            inclusive=_bool(args.get("inclusive"), True),
            to_invoice=(args.get("to_invoice") or args.get("to") or "").strip() or None,
            payments_from=pay_from,
            client_ids=client_ids,
            include_items="items" in include,
            include_products="products" in include,
            sort=(args.get("sort") or "name").strip().lower(),
        )
    except ValueError as e:
        return {"error": str(e)}, 400
    if result is None:
        return {"error": f"No invoice matches {from_invoice!r}."}, 404
    return result, 200

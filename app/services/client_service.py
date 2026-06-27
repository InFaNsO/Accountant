from ..database import get_db


def get_all_clients():
    db = get_db()
    return db.execute(
        """
        SELECT
            c.*,
            COALESCE(inv60.purchases_60d, 0)    AS purchases_60d,
            COALESCE(pay60.payments_60d, 0)     AS payments_60d,
            COALESCE(t_inv.total_invoiced, 0)   AS total_invoiced,
            COALESCE(t_pay.total_paid, 0)       AS total_paid
        FROM clients c
        LEFT JOIN (
            SELECT client_id, SUM(total) AS purchases_60d
            FROM invoices
            WHERE issue_date >= date('now', '-60 days')
              AND status != 'cancelled'
            GROUP BY client_id
        ) inv60 ON c.id = inv60.client_id
        LEFT JOIN (
            SELECT client_id, SUM(amount) AS payments_60d
            FROM payments
            WHERE payment_date >= date('now', '-60 days')
            GROUP BY client_id
        ) pay60 ON c.id = pay60.client_id
        LEFT JOIN (
            SELECT client_id, SUM(total) AS total_invoiced
            FROM invoices WHERE status != 'cancelled'
            GROUP BY client_id
        ) t_inv ON c.id = t_inv.client_id
        LEFT JOIN (
            SELECT client_id, SUM(amount) AS total_paid
            FROM payments
            GROUP BY client_id
        ) t_pay ON c.id = t_pay.client_id
        ORDER BY c.name
        """
    ).fetchall()


def get_all_clients_with_companies():
    """Like get_all_clients() but also attaches per-company financial stats."""
    db = get_db()
    clients = db.execute("""
        SELECT
            c.*,
            COALESCE(inv60.purchases_60d, 0)  AS purchases_60d,
            COALESCE(pay60.payments_60d, 0)   AS payments_60d,
            COALESCE(t_inv.total_invoiced, 0) AS total_invoiced,
            COALESCE(t_pay.total_paid, 0)     AS total_paid
        FROM clients c
        LEFT JOIN (SELECT client_id, SUM(total) AS purchases_60d FROM invoices
                   WHERE issue_date >= date('now','-60 days') AND status != 'cancelled'
                   GROUP BY client_id) inv60 ON c.id = inv60.client_id
        LEFT JOIN (SELECT client_id, SUM(amount) AS payments_60d FROM payments
                   WHERE payment_date >= date('now','-60 days') GROUP BY client_id) pay60 ON c.id = pay60.client_id
        LEFT JOIN (SELECT client_id, SUM(total) AS total_invoiced FROM invoices
                   WHERE status != 'cancelled' GROUP BY client_id) t_inv ON c.id = t_inv.client_id
        LEFT JOIN (SELECT client_id, SUM(amount) AS total_paid FROM payments
                   GROUP BY client_id) t_pay ON c.id = t_pay.client_id
        ORDER BY c.name
    """).fetchall()

    company_rows = db.execute("""
        SELECT
            cc.*,
            COALESCE(inv60.purchases_60d, 0)  AS purchases_60d,
            COALESCE(pay60.payments_60d, 0)   AS payments_60d,
            COALESCE(t_inv.total_invoiced, 0) AS total_invoiced,
            COALESCE(t_pay.total_paid, 0)     AS total_paid
        FROM client_companies cc
        LEFT JOIN (SELECT company_id, SUM(total) AS purchases_60d FROM invoices
                   WHERE issue_date >= date('now','-60 days') AND status != 'cancelled'
                   GROUP BY company_id) inv60 ON cc.id = inv60.company_id
        LEFT JOIN (SELECT company_id, SUM(amount) AS payments_60d FROM payments
                   WHERE payment_date >= date('now','-60 days') GROUP BY company_id) pay60 ON cc.id = pay60.company_id
        LEFT JOIN (SELECT company_id, SUM(total) AS total_invoiced FROM invoices
                   WHERE status != 'cancelled' GROUP BY company_id) t_inv ON cc.id = t_inv.company_id
        LEFT JOIN (SELECT company_id, SUM(amount) AS total_paid FROM payments
                   GROUP BY company_id) t_pay ON cc.id = t_pay.company_id
        ORDER BY cc.client_id, cc.name
    """).fetchall()

    co_map = {}
    for co in company_rows:
        co_map.setdefault(co['client_id'], []).append(dict(co))

    result = []
    for c in clients:
        d = dict(c)
        d['companies'] = co_map.get(c['id'], [])
        result.append(d)
    return result


def get_all_companies_with_client():
    """Return all client_companies joined with client name, for combobox injection."""
    return get_db().execute("""
        SELECT cc.id, cc.name, cc.client_id, c.name AS client_name
        FROM client_companies cc
        JOIN clients c ON c.id = cc.client_id
        ORDER BY cc.name
    """).fetchall()


def get_client(client_id):
    return get_db().execute(
        "SELECT * FROM clients WHERE id = ?", (client_id,)
    ).fetchone()


def create_client(data, companies=None):
    """Create a client. If companies list provided, create them and derive OB from their sum."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO clients (name, phone, notes, opening_balance, payment_terms)
           VALUES (?, ?, ?, 0, ?)""",
        (
            data["name"],
            data.get("phone"),
            data.get("notes"),
            int(data.get("payment_terms") or 30),
        ),
    )
    client_id = cur.lastrowid

    if companies:
        for co in companies:
            if co.get("name"):
                ob = _signed_opening(co)
                db.execute(
                    """INSERT INTO client_companies
                           (client_id, name, tax_id, opening_balance)
                       VALUES (?, ?, ?, ?)""",
                    (client_id, co["name"], co.get("tax_id"), ob),
                )
        _sync_client_ob(db, client_id)

    db.commit()
    return client_id


def _signed_opening(data):
    """Return signed opening balance: positive = debt, negative = credit."""
    amt = abs(float(data.get("opening_balance_amt") or data.get("opening_balance") or 0))
    if data.get("opening_balance_type") == "credit":
        return -amt
    return amt


def _sync_client_ob(db, client_id):
    """Set clients.opening_balance = SUM of all company opening balances."""
    row = db.execute(
        "SELECT COALESCE(SUM(opening_balance), 0) AS s FROM client_companies WHERE client_id=?",
        (client_id,),
    ).fetchone()
    db.execute(
        "UPDATE clients SET opening_balance=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (row["s"], client_id),
    )


def update_client(client_id, data, companies=None):
    """Update a client's basic info. If companies list provided, sync company records."""
    db = get_db()
    db.execute(
        """UPDATE clients
           SET name=?, phone=?, notes=?,
               payment_terms=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            data["name"],
            data.get("phone"),
            data.get("notes"),
            int(data.get("payment_terms") or 30),
            client_id,
        ),
    )

    if companies is not None:
        existing_ids = {
            row["id"]
            for row in db.execute(
                "SELECT id FROM client_companies WHERE client_id=?", (client_id,)
            ).fetchall()
        }
        submitted_ids = set()
        for co in companies:
            if not co.get("name"):
                continue
            ob = _signed_opening(co)
            co_id = co.get("id")
            if co_id:
                co_id = int(co_id)
                db.execute(
                    "UPDATE client_companies SET name=?, tax_id=?, opening_balance=? WHERE id=?",
                    (co["name"], co.get("tax_id"), ob, co_id),
                )
                submitted_ids.add(co_id)
            else:
                row = db.execute(
                    """INSERT INTO client_companies (client_id, name, tax_id, opening_balance)
                       VALUES (?, ?, ?, ?)""",
                    (client_id, co["name"], co.get("tax_id"), ob),
                )
                submitted_ids.add(row.lastrowid)
        # Delete companies removed from form
        for gone_id in existing_ids - submitted_ids:
            db.execute("UPDATE invoices SET company_id=NULL WHERE company_id=?", (gone_id,))
            db.execute("UPDATE payments SET company_id=NULL WHERE company_id=?", (gone_id,))
            db.execute("DELETE FROM client_companies WHERE id=?", (gone_id,))
        _sync_client_ob(db, client_id)

    db.commit()


def delete_client(client_id):
    db = get_db()
    db.execute("DELETE FROM payments WHERE client_id = ?", (client_id,))
    db.execute("DELETE FROM invoices WHERE client_id = ?", (client_id,))
    db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    db.commit()


def get_client_invoices(client_id):
    return get_db().execute(
        "SELECT * FROM invoices WHERE client_id = ? ORDER BY created_at DESC",
        (client_id,),
    ).fetchall()


def get_client_product_breakdown(client_id, date_from=None, date_to=None):
    """Split the catalogue into what this client bought in a date window vs not.

    date_from / date_to are 'YYYY-MM-DD' strings (inclusive). When omitted they
    default to the last 2 months (date('now','-2 months') … today).

    Returns a dict:
      {
        "purchased": [ {product_id, name, category_id, category_name, total_qty,
                        total_boxes, has_box_size, total_amount}, ... ]   # sorted by amount desc
        "purchased_by_category": [ {category_id, category_name, total_amount,
                        total_boxes, any_box_size, products:[...]}, ... ]  # by total spend desc
        "not_purchased": [ {category_id, category_name, products:[{id,name}]}, ... ]  # by category, then name
        "period_label": "2 months",   # or "<from> → <to>" for an explicit range
        "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD",   # effective bounds used
      }

    total_qty is pieces; total_boxes uses the sub-product's pcs_per_carton when set,
    else the parent product's (same COALESCE rule the invoice/box toggle uses). Lines
    for products with no box size contribute 0 boxes (qty still counts toward total_qty).
    Only catalogued line items (product_id NOT NULL) on non-cancelled invoices count.
    """
    db = get_db()

    # Resolve the effective window once (lets us echo the dates back and reuse them).
    bounds = db.execute(
        "SELECT COALESCE(?, date('now', '-2 months')) AS df, COALESCE(?, date('now')) AS dt",
        (date_from, date_to),
    ).fetchone()
    eff_from, eff_to = bounds["df"], bounds["dt"]

    purchased_rows = db.execute(
        """
        SELECT
            p.id                                   AS product_id,
            p.name                                 AS name,
            p.category_id                          AS category_id,
            COALESCE(cat.name, 'Uncategorized')    AS category_name,
            SUM(ii.quantity)                       AS total_qty,
            SUM(ii.line_total)                     AS total_amount,
            SUM(
                CASE WHEN COALESCE(NULLIF(sp.pcs_per_carton, 0), p.pcs_per_carton, 0) > 0
                     THEN ii.quantity * 1.0 / COALESCE(NULLIF(sp.pcs_per_carton, 0), p.pcs_per_carton)
                     ELSE 0 END
            )                                      AS total_boxes,
            MAX(
                CASE WHEN COALESCE(NULLIF(sp.pcs_per_carton, 0), p.pcs_per_carton, 0) > 0
                     THEN 1 ELSE 0 END
            )                                      AS has_box_size
        FROM invoice_items ii
        JOIN invoices i        ON i.id = ii.invoice_id
        JOIN products p        ON p.id = ii.product_id
        LEFT JOIN sub_products sp ON sp.id = ii.sub_product_id
        LEFT JOIN categories cat  ON cat.id = p.category_id
        WHERE i.client_id = ?
          AND i.status != 'cancelled'
          AND i.issue_date >= ?
          AND i.issue_date <= ?
          AND ii.product_id IS NOT NULL
        GROUP BY p.id
        ORDER BY total_amount DESC, total_boxes DESC, p.name
        """,
        (client_id, eff_from, eff_to),
    ).fetchall()

    purchased = [dict(r) for r in purchased_rows]
    purchased_ids = {r["product_id"] for r in purchased}

    # Group the purchased products under their category (collapsible in the UI).
    # `purchased` is already sorted by amount desc, so products stay amount-ordered
    # within each category; we re-sort the categories themselves by total spend.
    pcats = {}
    for r in purchased:
        key = r["category_id"] or 0
        if key not in pcats:
            pcats[key] = {
                "category_id":   r["category_id"],
                "category_name": r["category_name"],
                "total_amount":  0.0,
                "total_boxes":   0.0,
                "any_box_size":  False,
                "products":      [],
            }
        g = pcats[key]
        g["products"].append(r)
        g["total_amount"] += float(r["total_amount"] or 0)
        g["total_boxes"]  += float(r["total_boxes"] or 0)
        if r["has_box_size"]:
            g["any_box_size"] = True
    purchased_by_category = sorted(
        pcats.values(), key=lambda c: c["total_amount"], reverse=True
    )

    # Every active catalogue product, grouped by category, minus what they bought.
    catalogue = db.execute(
        """
        SELECT p.id, p.name, p.category_id,
               COALESCE(cat.name, 'Uncategorized') AS category_name
        FROM products p
        LEFT JOIN categories cat ON cat.id = p.category_id
        WHERE p.is_active = 1
        ORDER BY category_name, p.name
        """
    ).fetchall()

    cats = {}
    cat_order = []
    for row in catalogue:
        if row["id"] in purchased_ids:
            continue
        key = row["category_id"] or 0
        if key not in cats:
            cats[key] = {
                "category_id":   row["category_id"],
                "category_name": row["category_name"],
                "products":      [],
            }
            cat_order.append(key)
        cats[key]["products"].append({"id": row["id"], "name": row["name"]})

    not_purchased = [cats[k] for k in cat_order]

    label = f"{eff_from} → {eff_to}" if (date_from or date_to) else "2 months"
    return {
        "purchased":             purchased,
        "purchased_by_category": purchased_by_category,
        "not_purchased":         not_purchased,
        "period_label":          label,
        "date_from":             eff_from,
        "date_to":               eff_to,
    }


def get_client_balance(client_id):
    """
    Returns signed balance: negative = client owes us, positive = client has credit.

    Formula:
      balance = (total_payments + credit_ob) - (total_invoiced + debit_ob)

    This is independent of invoice.amount_paid so it stays correct whether or not
    the OB credit has been baked into amount_paid via Recalculate.
    """
    db = get_db()
    client_row = db.execute(
        "SELECT opening_balance FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if not client_row:
        return 0.0

    ob       = float(client_row["opening_balance"] or 0)
    debit_ob = max(0.0, ob)    # positive OB = client owes us from before
    credit_ob = max(0.0, -ob)  # negative OB = client pre-paid credit

    total_invoiced = float(db.execute(
        "SELECT COALESCE(SUM(total), 0) AS s FROM invoices WHERE client_id=? AND status != 'cancelled'",
        (client_id,),
    ).fetchone()["s"])

    total_paid = float(db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE client_id=?",
        (client_id,),
    ).fetchone()["s"])

    return (total_paid + credit_ob) - (total_invoiced + debit_ob)


# ── Client companies ──────────────────────────────────────────────────────────

def get_companies(client_id):
    return get_db().execute(
        "SELECT * FROM client_companies WHERE client_id=? ORDER BY name",
        (client_id,),
    ).fetchall()


def create_company(client_id, data):
    db = get_db()
    ob = _signed_opening(data)
    cur = db.execute(
        """INSERT INTO client_companies (client_id, name, tax_id, opening_balance)
           VALUES (?, ?, ?, ?)""",
        (client_id, data["name"], data.get("tax_id"), ob),
    )
    _sync_client_ob(db, client_id)
    db.commit()
    return cur.lastrowid


def update_company(company_id, data):
    db = get_db()
    ob = _signed_opening(data)
    db.execute(
        "UPDATE client_companies SET name=?, tax_id=?, opening_balance=? WHERE id=?",
        (data["name"], data.get("tax_id"), ob, company_id),
    )
    row = db.execute("SELECT client_id FROM client_companies WHERE id=?", (company_id,)).fetchone()
    if row:
        _sync_client_ob(db, row["client_id"])
    db.commit()


def get_company_balance(company_id, client_id):
    """Returns signed balance for a single company: positive = credit, negative = owes."""
    db = get_db()
    co = db.execute(
        "SELECT opening_balance FROM client_companies WHERE id=?", (company_id,)
    ).fetchone()
    if not co:
        return 0.0
    ob        = float(co["opening_balance"] or 0)
    credit_ob = max(0.0, -ob)
    debit_ob  = max(0.0, ob)
    total_invoiced = float(db.execute(
        "SELECT COALESCE(SUM(total), 0) AS s FROM invoices "
        "WHERE company_id=? AND client_id=? AND status != 'cancelled'",
        (company_id, client_id),
    ).fetchone()["s"])
    total_paid = float(db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payments "
        "WHERE company_id=? AND client_id=?",
        (company_id, client_id),
    ).fetchone()["s"])
    return (total_paid + credit_ob) - (total_invoiced + debit_ob)


def delete_company(company_id):
    db = get_db()
    row = db.execute("SELECT client_id FROM client_companies WHERE id=?", (company_id,)).fetchone()
    db.execute("UPDATE invoices SET company_id=NULL WHERE company_id=?", (company_id,))
    db.execute("UPDATE payments SET company_id=NULL WHERE company_id=?", (company_id,))
    db.execute("DELETE FROM client_companies WHERE id=?", (company_id,))
    if row:
        _sync_client_ob(db, row["client_id"])
    db.commit()

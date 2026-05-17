from ..database import get_db
from .product_service import adjust_stock, get_products_with_subs


def get_all_tallies():
    return get_db().execute(
        "SELECT * FROM stock_tallies ORDER BY created_at DESC"
    ).fetchall()


def create_tally(name, notes):
    db = get_db()
    cur = db.execute(
        "INSERT INTO stock_tallies (name, notes) VALUES (?, ?)",
        (name, notes or None),
    )
    tally_id = cur.lastrowid

    groups = get_products_with_subs(active_only=True)
    for group in groups:
        p    = group["product"]
        subs = group["subs"]
        if subs:
            for s in subs:
                if s["is_active"]:
                    db.execute(
                        "INSERT INTO stock_tally_items (tally_id, product_id, sub_id, digital_qty) VALUES (?,?,?,?)",
                        (tally_id, p["id"], s["id"], s["stock_qty"] or 0),
                    )
        else:
            db.execute(
                "INSERT INTO stock_tally_items (tally_id, product_id, sub_id, digital_qty) VALUES (?,?,NULL,?)",
                (tally_id, p["id"], p["stock_qty"] or 0),
            )
    db.commit()
    return tally_id


def get_tally(tally_id):
    db    = get_db()
    tally = db.execute("SELECT * FROM stock_tallies WHERE id=?", (tally_id,)).fetchone()
    if not tally:
        return None, []

    rows = db.execute("""
        SELECT ti.*,
               p.name AS product_name, p.sku AS product_sku, p.pcs_per_carton,
               COALESCE(c.name, 'Uncategorized') AS category_name,
               s.name AS sub_name, s.sku AS sub_sku
        FROM stock_tally_items ti
        JOIN products p ON ti.product_id = p.id
        LEFT JOIN categories c ON p.category_id = c.id
        LEFT JOIN sub_products s ON ti.sub_id = s.id
        WHERE ti.tally_id = ?
        ORDER BY COALESCE(c.name, 'Uncategorized'), p.name, COALESCE(NULLIF(s.sku,''), s.name)
    """, (tally_id,)).fetchall()

    # Build flat product groups
    product_groups = {}
    product_order  = []
    for row in rows:
        pid = row["product_id"]
        if pid not in product_groups:
            product_groups[pid] = {
                "product_id":    pid,
                "product_name":  row["product_name"],
                "product_sku":   row["product_sku"],
                "pcs_per_carton": row["pcs_per_carton"],
                "category_name": row["category_name"],
                "refreshed_at":  row["refreshed_at"],
                "rows":          [],
            }
            product_order.append(pid)
        product_groups[pid]["rows"].append(dict(row))

    # Organise by category
    categories    = {}
    category_order = []
    for pid in product_order:
        group = product_groups[pid]
        cat   = group["category_name"]
        if cat not in categories:
            categories[cat] = {"category_name": cat, "groups": []}
            category_order.append(cat)
        categories[cat]["groups"].append(group)

    return tally, [categories[cat] for cat in category_order]


def save_tally_items(tally_id, form_data):
    db = get_db()
    tally = db.execute("SELECT status FROM stock_tallies WHERE id=?", (tally_id,)).fetchone()
    if not tally or tally["status"] != "draft":
        return
    for key, val in form_data.items():
        if not key.startswith("item_"):
            continue
        try:
            item_id = int(key[5:])
        except ValueError:
            continue
        val = val.strip() if val else ""
        if val == "":
            physical_qty = None
        else:
            try:
                physical_qty = float(val.replace(",", ""))
            except ValueError:
                physical_qty = None
        db.execute(
            "UPDATE stock_tally_items SET physical_qty=? WHERE id=? AND tally_id=?",
            (physical_qty, item_id, tally_id),
        )
    db.commit()


def refresh_product_digital(tally_id, product_id):
    db = get_db()
    tally = db.execute("SELECT status FROM stock_tallies WHERE id=?", (tally_id,)).fetchone()
    if not tally or tally["status"] != "draft":
        return False
    db.execute("""
        UPDATE stock_tally_items
        SET digital_qty  = (SELECT sp.stock_qty FROM sub_products sp WHERE sp.id = stock_tally_items.sub_id),
            refreshed_at = CURRENT_TIMESTAMP
        WHERE tally_id=? AND product_id=? AND sub_id IS NOT NULL
    """, (tally_id, product_id))
    db.execute("""
        UPDATE stock_tally_items
        SET digital_qty  = (SELECT p.stock_qty FROM products p WHERE p.id = stock_tally_items.product_id),
            refreshed_at = CURRENT_TIMESTAMP
        WHERE tally_id=? AND product_id=? AND sub_id IS NULL
    """, (tally_id, product_id))
    db.commit()
    return True


def apply_tally(tally_id):
    db = get_db()
    tally = db.execute("SELECT * FROM stock_tallies WHERE id=?", (tally_id,)).fetchone()
    if not tally or tally["status"] != "draft":
        return False, "Tally is not a draft or does not exist."

    pending = db.execute(
        "SELECT COUNT(*) FROM stock_tally_items WHERE tally_id=? AND physical_qty IS NULL",
        (tally_id,),
    ).fetchone()[0]
    if pending > 0:
        return False, f"{pending} item(s) still pending — fill in all physical quantities first."

    items = db.execute(
        "SELECT * FROM stock_tally_items WHERE tally_id=?", (tally_id,)
    ).fetchall()

    notes = f"Stock tally #{tally_id} — {tally['name']}"
    for item in items:
        diff = (item["digital_qty"] or 0) - (item["physical_qty"] or 0)
        if diff == 0:
            continue
        if diff > 0:
            adjust_stock(item["product_id"], item["sub_id"], "warehouse", "decrease", diff, notes)
        else:
            adjust_stock(item["product_id"], item["sub_id"], "warehouse", "increase", abs(diff), notes)

    db.execute(
        "UPDATE stock_tallies SET status='applied', applied_at=CURRENT_TIMESTAMP WHERE id=?",
        (tally_id,),
    )
    db.commit()
    return True, None

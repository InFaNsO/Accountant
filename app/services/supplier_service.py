from ..database import get_db


def get_all_suppliers():
    return get_db().execute(
        "SELECT * FROM suppliers ORDER BY name"
    ).fetchall()


def get_supplier(supplier_id):
    return get_db().execute(
        "SELECT * FROM suppliers WHERE id=?", (supplier_id,)
    ).fetchone()


def create_supplier(data):
    db = get_db()
    cur = db.execute(
        """INSERT INTO suppliers (name, company, email, phone, address, notes)
           VALUES (?,?,?,?,?,?)""",
        (data["name"], data.get("company"), data.get("email"),
         data.get("phone"), data.get("address"), data.get("notes")),
    )
    db.commit()
    return cur.lastrowid


def update_supplier(supplier_id, data):
    db = get_db()
    db.execute(
        """UPDATE suppliers SET name=?, company=?, email=?, phone=?, address=?, notes=?
           WHERE id=?""",
        (data["name"], data.get("company"), data.get("email"),
         data.get("phone"), data.get("address"), data.get("notes"),
         supplier_id),
    )
    db.commit()


def delete_supplier(supplier_id):
    db = get_db()
    db.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
    db.commit()


# ── Supplier products (price list) ────────────────────────────────────────────

def get_supplier_products(supplier_id):
    return get_db().execute(
        """SELECT sp.*,
                  COALESCE(p.name, sub.name) AS item_name,
                  CASE WHEN sp.sub_product_id IS NOT NULL
                       THEN par.name || ' — ' || sub.name
                       ELSE p.name END AS display_name,
                  p.unit_price AS default_price
           FROM supplier_products sp
           LEFT JOIN products p   ON sp.product_id = p.id
           LEFT JOIN sub_products sub ON sp.sub_product_id = sub.id
           LEFT JOIN products par ON sub.product_id = par.id
           WHERE sp.supplier_id=?
           ORDER BY display_name""",
        (supplier_id,),
    ).fetchall()


def add_supplier_product(supplier_id, data):
    db = get_db()
    product_id     = data.get("product_id") or None
    sub_product_id = data.get("sub_product_id") or None
    price          = float(data["price"]) if data.get("price") else None
    cur = db.execute(
        """INSERT INTO supplier_products (supplier_id, product_id, sub_product_id, price, notes)
           VALUES (?,?,?,?,?)""",
        (supplier_id, product_id, sub_product_id, price, data.get("notes")),
    )
    db.commit()
    return cur.lastrowid


def update_supplier_product(sp_id, price, notes):
    db = get_db()
    db.execute(
        "UPDATE supplier_products SET price=?, notes=? WHERE id=?",
        (price, notes or None, sp_id),
    )
    db.commit()


def remove_supplier_product(sp_id):
    db = get_db()
    db.execute("DELETE FROM supplier_products WHERE id=?", (sp_id,))
    db.commit()


def get_suppliers_for_product(product_id=None, sub_product_id=None):
    """Return suppliers that supply a given product/sub-product."""
    db = get_db()
    if sub_product_id:
        return db.execute(
            """SELECT sp.*, s.name AS supplier_name
               FROM supplier_products sp JOIN suppliers s ON sp.supplier_id=s.id
               WHERE sp.sub_product_id=? ORDER BY s.name""",
            (sub_product_id,),
        ).fetchall()
    return db.execute(
        """SELECT sp.*, s.name AS supplier_name
           FROM supplier_products sp JOIN suppliers s ON sp.supplier_id=s.id
           WHERE sp.product_id=? AND sp.sub_product_id IS NULL ORDER BY s.name""",
        (product_id,),
    ).fetchall()

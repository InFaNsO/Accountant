import sqlite3
from flask import g


def get_db(app=None):
    from flask import current_app
    _app = app or current_app
    if "db" not in g:
        g.db = sqlite3.connect(_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    with app.app_context():
        db = sqlite3.connect(app.config["DATABASE"])
        db.row_factory = sqlite3.Row
        _create_schema(db)
        db.close()
    app.teardown_appcontext(close_db)


def _migrate_payment_allocations(db):
    """One-time migration from split-row payments to a single payment row + allocations.

    Idempotent: skips if payment_allocations already has any rows.
    """
    existing = db.execute("SELECT COUNT(*) FROM payment_allocations").fetchone()[0]
    if existing > 0:
        return

    # Step 1 — seed allocations from any payment that already targets an invoice.
    db.execute(
        "INSERT INTO payment_allocations (payment_id, invoice_id, amount) "
        "SELECT id, invoice_id, amount FROM payments WHERE invoice_id IS NOT NULL"
    )

    # Step 2 — collapse system-split rows. A "split" is multiple payments rows that share
    # client_id, payment_date, method, reference, notes, company_id — these were created
    # by a single create_payment() call in the old codebase.
    groups = db.execute("""
        SELECT GROUP_CONCAT(id) AS ids, SUM(amount) AS total
        FROM payments
        GROUP BY client_id, payment_date,
                 COALESCE(method,''), COALESCE(reference,''),
                 COALESCE(notes,''),  COALESCE(company_id,0)
        HAVING COUNT(*) > 1
    """).fetchall()

    for g in groups:
        ids  = sorted(int(x) for x in g["ids"].split(","))
        keep = ids[0]
        drop = ids[1:]
        # Re-point allocations from soon-to-be-deleted rows to the keeper.
        for d in drop:
            db.execute(
                "UPDATE payment_allocations SET payment_id=? WHERE payment_id=?",
                (keep, d),
            )
        db.execute(
            "UPDATE payments SET amount=?, invoice_id=NULL WHERE id=?",
            (g["total"], keep),
        )
        db.executemany("DELETE FROM payments WHERE id=?", [(d,) for d in drop])

    # Step 3 — for any surviving payment row that still carries invoice_id, NULL it.
    # The allocation row created in step 1 is now the source of truth.
    db.execute("UPDATE payments SET invoice_id=NULL WHERE invoice_id IS NOT NULL")
    db.commit()


def _add_column(db, table, column, definition):
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()
    except Exception:
        pass


def _create_schema(db):
    # ── Core tables (idempotent) ──────────────────────────────
    db.executescript("""
        PRAGMA foreign_keys = OFF;

        CREATE TABLE IF NOT EXISTS clients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            company         TEXT,
            email           TEXT,
            phone           TEXT,
            address         TEXT,
            city            TEXT,
            country         TEXT,
            tax_id          TEXT,
            notes           TEXT,
            opening_balance REAL DEFAULT 0,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS products (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id     INTEGER,
            name            TEXT NOT NULL,
            sku             TEXT,
            description     TEXT,
            unit_price      REAL NOT NULL DEFAULT 0,
            tax_rate        REAL DEFAULT 0,
            track_inventory INTEGER DEFAULT 1,
            stock_qty       REAL DEFAULT 0,
            min_quantity    REAL DEFAULT 0,
            production_qty  REAL DEFAULT 0,
            in_transit_qty  REAL DEFAULT 0,
            is_active       INTEGER DEFAULT 1,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS sub_products (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id       INTEGER NOT NULL,
            name             TEXT NOT NULL,
            sku              TEXT,
            description      TEXT,
            use_parent_price INTEGER DEFAULT 0,
            unit_price       REAL DEFAULT 0,
            tax_rate         REAL DEFAULT 0,
            track_inventory  INTEGER DEFAULT 1,
            stock_qty        REAL DEFAULT 0,
            min_quantity     REAL DEFAULT 0,
            production_qty   REAL DEFAULT 0,
            in_transit_qty   REAL DEFAULT 0,
            is_active        INTEGER DEFAULT 1,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS stock_movements (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id          INTEGER,
            sub_product_id      INTEGER,
            movement_type       TEXT NOT NULL,
            quantity            REAL NOT NULL,
            notes               TEXT,
            expected_arrival    DATE,
            linked_movement_id  INTEGER,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id)     REFERENCES products(id),
            FOREIGN KEY (sub_product_id) REFERENCES sub_products(id)
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number  TEXT UNIQUE NOT NULL,
            client_id       INTEGER NOT NULL,
            status          TEXT DEFAULT 'draft',
            issue_date      DATE NOT NULL,
            due_date        DATE,
            notes           TEXT,
            subtotal        REAL DEFAULT 0,
            tax_total       REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            total           REAL DEFAULT 0,
            amount_paid     REAL DEFAULT 0,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );

        CREATE TABLE IF NOT EXISTS invoice_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id     INTEGER NOT NULL,
            product_id     INTEGER,
            sub_product_id INTEGER,
            description    TEXT NOT NULL,
            quantity       REAL DEFAULT 1,
            unit_price     REAL NOT NULL,
            tax_rate       REAL DEFAULT 0,
            line_total     REAL NOT NULL,
            FOREIGN KEY (invoice_id)     REFERENCES invoices(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id)     REFERENCES products(id),
            FOREIGN KEY (sub_product_id) REFERENCES sub_products(id)
        );

        PRAGMA foreign_keys = ON;
    """)

    # ── Migrate payments table to allow nullable invoice_id ───
    cols = {row[1] for row in db.execute("PRAGMA table_info(payments)")}
    if "invoice_id" in cols:
        # Check if invoice_id is nullable by inspecting the schema text
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='payments'"
        ).fetchone()
        schema_sql = schema[0] if schema else ""
        needs_migration = "invoice_id   INTEGER NOT NULL" in schema_sql or \
                          "invoice_id INTEGER NOT NULL" in schema_sql

        if needs_migration:
            db.executescript("""
                PRAGMA foreign_keys = OFF;
                CREATE TABLE IF NOT EXISTS payments_new (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id    INTEGER NOT NULL,
                    invoice_id   INTEGER,
                    amount       REAL NOT NULL,
                    payment_date DATE NOT NULL,
                    method       TEXT DEFAULT 'cash',
                    reference    TEXT,
                    notes        TEXT,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id)  REFERENCES clients(id),
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
                );
                INSERT INTO payments_new
                    SELECT id, client_id, invoice_id, amount, payment_date,
                           method, reference, notes, created_at
                    FROM payments;
                DROP TABLE payments;
                ALTER TABLE payments_new RENAME TO payments;
                PRAGMA foreign_keys = ON;
            """)
    else:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS payments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id    INTEGER NOT NULL,
                invoice_id   INTEGER,
                amount       REAL NOT NULL,
                payment_date DATE NOT NULL,
                method       TEXT DEFAULT 'cash',
                reference    TEXT,
                notes        TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id)  REFERENCES clients(id),
                FOREIGN KEY (invoice_id) REFERENCES invoices(id)
            );
        """)

    # ── Suppliers + purchase orders + dispatches ──────────────
    db.executescript("""
        PRAGMA foreign_keys = OFF;

        CREATE TABLE IF NOT EXISTS suppliers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            company    TEXT,
            email      TEXT,
            phone      TEXT,
            address    TEXT,
            notes      TEXT,
            is_active  INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS supplier_products (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id    INTEGER NOT NULL,
            product_id     INTEGER,
            sub_product_id INTEGER,
            price          REAL,
            notes          TEXT,
            FOREIGN KEY (supplier_id)    REFERENCES suppliers(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id)     REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (sub_product_id) REFERENCES sub_products(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            name                TEXT NOT NULL,
            supplier_id         INTEGER,
            expected_completion DATE,
            status              TEXT DEFAULT 'open',
            notes               TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
        );

        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            po_id          INTEGER NOT NULL,
            product_id     INTEGER,
            sub_product_id INTEGER,
            quantity       REAL NOT NULL,
            price          REAL,
            qty_dispatched REAL DEFAULT 0,
            FOREIGN KEY (po_id)          REFERENCES purchase_orders(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id)     REFERENCES products(id),
            FOREIGN KEY (sub_product_id) REFERENCES sub_products(id)
        );

        CREATE TABLE IF NOT EXISTS dispatches (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            supplier_id      INTEGER,
            dispatch_date    DATE,
            expected_arrival DATE,
            status           TEXT DEFAULT 'in_transit',
            notes            TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
        );

        CREATE TABLE IF NOT EXISTS dispatch_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id    INTEGER NOT NULL,
            product_id     INTEGER,
            sub_product_id INTEGER,
            quantity       REAL NOT NULL,
            qty_received   REAL DEFAULT 0,
            price          REAL,
            cbm            REAL,
            gross_weight   REAL,
            FOREIGN KEY (dispatch_id)    REFERENCES dispatches(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id)     REFERENCES products(id),
            FOREIGN KEY (sub_product_id) REFERENCES sub_products(id)
        );

        CREATE TABLE IF NOT EXISTS dispatch_po_allocations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_item_id INTEGER NOT NULL,
            po_item_id       INTEGER NOT NULL,
            quantity         REAL NOT NULL,
            FOREIGN KEY (dispatch_item_id) REFERENCES dispatch_items(id) ON DELETE CASCADE,
            FOREIGN KEY (po_item_id)       REFERENCES purchase_order_items(id)
        );

        PRAGMA foreign_keys = ON;
    """)

    # ── Palm purchases (instant warehouse stock-in) ───────────
    db.executescript("""
        CREATE TABLE IF NOT EXISTS palm_purchases (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT,
            supplier_id   INTEGER,
            purchase_date DATE NOT NULL,
            notes         TEXT,
            total_cost    REAL DEFAULT 0,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
        );

        CREATE TABLE IF NOT EXISTS palm_purchase_items (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            palm_purchase_id INTEGER NOT NULL,
            product_id       INTEGER,
            sub_product_id   INTEGER,
            quantity         REAL NOT NULL,
            unit_cost        REAL DEFAULT 0,
            notes            TEXT,
            FOREIGN KEY (palm_purchase_id) REFERENCES palm_purchases(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id)       REFERENCES products(id),
            FOREIGN KEY (sub_product_id)   REFERENCES sub_products(id)
        );
    """)

    # ── Payment allocations (links one payment to N invoices) ─
    db.executescript("""
        CREATE TABLE IF NOT EXISTS payment_allocations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL,
            invoice_id INTEGER NOT NULL,
            amount     REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (payment_id) REFERENCES payments(id) ON DELETE CASCADE,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        );
        CREATE INDEX IF NOT EXISTS idx_pa_payment ON payment_allocations(payment_id);
        CREATE INDEX IF NOT EXISTS idx_pa_invoice ON payment_allocations(invoice_id);
    """)
    _migrate_payment_allocations(db)

    # ── Manual ledger entries ─────────────────────────────────
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ledger_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER NOT NULL,
            entry_date  DATE NOT NULL,
            description TEXT,
            debit       REAL DEFAULT 0,
            credit      REAL DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );
    """)

    # ── Client companies (multiple companies per client) ──────
    db.executescript("""
        CREATE TABLE IF NOT EXISTS client_companies (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id  INTEGER NOT NULL,
            name       TEXT NOT NULL,
            email      TEXT,
            phone      TEXT,
            address    TEXT,
            city       TEXT,
            country    TEXT,
            tax_id     TEXT,
            notes      TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        );
    """)

    # ── Users & permissions ───────────────────────────────────
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'user',
            is_active     INTEGER DEFAULT 1,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_permissions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            module     TEXT NOT NULL,
            can_view   INTEGER DEFAULT 0,
            can_create INTEGER DEFAULT 0,
            can_edit   INTEGER DEFAULT 0,
            can_delete INTEGER DEFAULT 0,
            UNIQUE(user_id, module),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_dashboard_sections (
            user_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            PRIMARY KEY (user_id, section),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    # ── Stock tallies ─────────────────────────────────────────
    db.executescript("""
        CREATE TABLE IF NOT EXISTS stock_tallies (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            notes      TEXT,
            status     TEXT DEFAULT 'draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            applied_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS stock_tally_items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tally_id     INTEGER NOT NULL,
            product_id   INTEGER NOT NULL,
            sub_id       INTEGER,
            digital_qty  REAL NOT NULL DEFAULT 0,
            physical_qty REAL,
            refreshed_at TIMESTAMP,
            FOREIGN KEY (tally_id)   REFERENCES stock_tallies(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id),
            FOREIGN KEY (sub_id)     REFERENCES sub_products(id)
        );
    """)

    # ── Mobile push notifications ─────────────────────────────
    db.executescript("""
        CREATE TABLE IF NOT EXISTS device_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            fcm_token  TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS notification_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            type         TEXT NOT NULL,
            reference_id INTEGER NOT NULL,
            sent_date    TEXT NOT NULL,
            UNIQUE(type, reference_id, sent_date)
        );
    """)

    # ── Column-level migrations for existing installs ─────────
    _add_column(db, "user_permissions",  "can_financials",  "INTEGER DEFAULT 0")
    _add_column(db, "clients",          "opening_balance", "REAL DEFAULT 0")
    _add_column(db, "clients",          "payment_terms",   "INTEGER DEFAULT 0")
    _add_column(db, "client_companies", "opening_balance", "REAL DEFAULT 0")
    _add_column(db, "invoices",         "company_id",      "INTEGER")
    _add_column(db, "payments",         "company_id",      "INTEGER")
    _add_column(db, "products",         "category_id",     "INTEGER")
    _add_column(db, "products",         "min_quantity",    "REAL DEFAULT 0")
    _add_column(db, "products",         "production_qty",  "REAL DEFAULT 0")
    _add_column(db, "products",         "in_transit_qty",  "REAL DEFAULT 0")
    _add_column(db, "sub_products",     "production_qty",  "REAL DEFAULT 0")
    _add_column(db, "sub_products",     "in_transit_qty",  "REAL DEFAULT 0")
    _add_column(db, "invoice_items",    "sub_product_id",  "INTEGER")
    _add_column(db, "invoice_items",    "sku",             "TEXT")
    _add_column(db, "stock_movements",  "dispatch_id",     "INTEGER")
    _add_column(db, "stock_movements",  "invoice_id",      "INTEGER")
    _add_column(db, "products",              "pcs_per_carton",    "INTEGER DEFAULT 0")
    _add_column(db, "products",              "has_eco_range",     "INTEGER DEFAULT 0")
    _add_column(db, "products",              "eco_parent_id",     "INTEGER")
    _add_column(db, "sub_products",          "eco_parent_sub_id", "INTEGER")
    _add_column(db, "purchase_order_items",  "product_name",      "TEXT")
    _add_column(db, "dispatch_items",        "product_name",      "TEXT")
    # Discount type/value: invoice-level (default 'value' so existing flat-Rs data is preserved)
    _add_column(db, "invoices",              "discount_type",     "TEXT DEFAULT 'value'")
    _add_column(db, "invoices",              "discount_value",    "REAL DEFAULT 0")
    # Per-line-item discount (default 'percent' with value 0 → 0% discount)
    _add_column(db, "invoice_items",         "discount_type",     "TEXT DEFAULT 'percent'")
    _add_column(db, "invoice_items",         "discount_value",    "REAL DEFAULT 0")
    _add_column(db, "sub_products",          "pcs_per_carton",    "INTEGER DEFAULT 0")
    _add_column(db, "stock_movements",        "palm_purchase_id", "INTEGER")

    db.commit()

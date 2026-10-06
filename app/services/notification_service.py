import logging
import os
import sqlite3
from datetime import date

logger = logging.getLogger(__name__)

_firebase_ready = False


def init_firebase():
    global _firebase_ready
    creds_path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "")
    if not creds_path or not os.path.exists(creds_path):
        logger.warning("FIREBASE_CREDENTIALS_PATH not set or file missing — push notifications disabled")
        return

    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(creds_path))
        _firebase_ready = True
        logger.info("Firebase initialized")
    except Exception as e:
        logger.error("Firebase init failed: %s", e)


def _get_all_tokens(db_path: str) -> list[str]:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT fcm_token FROM device_tokens").fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.error("Failed to fetch FCM tokens: %s", e)
        return []


def _already_notified(db_path: str, ntype: str, ref_id: int, today: str) -> bool:
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT 1 FROM notification_log WHERE type=? AND reference_id=? AND sent_date=?",
            (ntype, ref_id, today),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _mark_notified(db_path: str, ntype: str, ref_id: int, today: str):
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR IGNORE INTO notification_log (type, reference_id, sent_date) VALUES (?,?,?)",
            (ntype, ref_id, today),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to mark notification: %s", e)


def _send_multicast(tokens: list[str], title: str, body: str, data: dict):
    if not _firebase_ready or not tokens:
        return
    try:
        from firebase_admin import messaging

        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in data.items()},
            tokens=tokens,
        )
        response = messaging.send_each_for_multicast(message)
        logger.info("FCM sent %d/%d", response.success_count, len(tokens))

        # Remove tokens that are no longer valid
        invalid = []
        for i, resp in enumerate(response.responses):
            if not resp.success and resp.exception:
                code = getattr(resp.exception, "code", "")
                if code in ("registration-token-not-registered", "invalid-argument"):
                    invalid.append(tokens[i])
        if invalid:
            _remove_invalid_tokens(data.get("db_path", ""), invalid)
    except Exception as e:
        logger.error("FCM send failed: %s", e)


def _remove_invalid_tokens(db_path: str, tokens: list[str]):
    if not db_path or not tokens:
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.executemany("DELETE FROM device_tokens WHERE fcm_token=?", [(t,) for t in tokens])
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to remove invalid tokens: %s", e)


def check_overdue_invoices(app):
    with app.app_context():
        db_path = app.config["DATABASE"]
        today = date.today().isoformat()
        tokens = _get_all_tokens(db_path)
        if not tokens:
            return

        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                """
                SELECT i.id, i.invoice_number, c.name, i.total - i.amount_paid
                FROM invoices i
                JOIN clients c ON c.id = i.client_id
                WHERE i.due_date < ? AND i.status NOT IN ('paid', 'cancelled')
                  AND (i.total - i.amount_paid) > 0
                """,
                (today,),
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.error("DB query failed (overdue): %s", e)
            return

        for inv_id, inv_number, client_name, balance in rows:
            if _already_notified(db_path, "overdue_invoice", inv_id, today):
                continue
            _send_multicast(
                tokens,
                title="Overdue Invoice",
                body=f"{inv_number} · {client_name} · ₹{balance:,.0f} overdue",
                data={"url": "/invoices", "type": "overdue_invoice", "db_path": db_path},
            )
            _mark_notified(db_path, "overdue_invoice", inv_id, today)


def check_low_stock(app):
    with app.app_context():
        db_path = app.config["DATABASE"]
        today = date.today().isoformat()
        tokens = _get_all_tokens(db_path)
        if not tokens:
            return

        try:
            conn = sqlite3.connect(db_path)
            # Standalone products (no sub-products)
            prod_rows = conn.execute(
                """SELECT p.id, p.name, p.stock_qty, p.min_quantity
                   FROM products p
                   WHERE p.is_active=1 AND p.min_quantity>0 AND p.stock_qty<p.min_quantity
                     AND NOT EXISTS (SELECT 1 FROM sub_products sp WHERE sp.product_id=p.id AND sp.is_active=1)""",
            ).fetchall()
            # Sub-products
            sub_rows = conn.execute(
                """SELECT s.id, p.name || ' — ' || s.name, s.stock_qty, s.min_quantity
                   FROM sub_products s
                   JOIN products p ON p.id=s.product_id
                   WHERE s.is_active=1 AND s.min_quantity>0 AND s.stock_qty<s.min_quantity""",
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.error("DB query failed (low stock): %s", e)
            return

        for prod_id, name, stock, minimum in prod_rows:
            if _already_notified(db_path, "low_stock", prod_id, today):
                continue
            _send_multicast(
                tokens,
                title="Low Stock Alert",
                body=f"{name} · {stock:.0f} remaining (min {minimum:.0f})",
                data={"url": "/products", "type": "low_stock", "db_path": db_path},
            )
            _mark_notified(db_path, "low_stock", prod_id, today)

        for sub_id, name, stock, minimum in sub_rows:
            if _already_notified(db_path, "low_stock_sub", sub_id, today):
                continue
            _send_multicast(
                tokens,
                title="Low Stock Alert",
                body=f"{name} · {stock:.0f} remaining (min {minimum:.0f})",
                data={"url": "/products", "type": "low_stock_sub", "db_path": db_path},
            )
            _mark_notified(db_path, "low_stock_sub", sub_id, today)


def check_transit_due(app):
    with app.app_context():
        db_path = app.config["DATABASE"]
        today = date.today().isoformat()
        tokens = _get_all_tokens(db_path)
        if not tokens:
            return

        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                """SELECT d.id, d.name, d.expected_arrival, s.name AS supplier_name
                   FROM dispatches d
                   LEFT JOIN suppliers s ON s.id=d.supplier_id
                   WHERE d.status IN ('in_transit','partially_received')
                     AND d.expected_arrival IS NOT NULL
                     AND d.expected_arrival <= date('now','+3 days')""",
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.error("DB query failed (transit due): %s", e)
            return

        for dispatch_id, name, arrival, supplier in rows:
            if _already_notified(db_path, "transit_due", dispatch_id, today):
                continue
            overdue = arrival < today
            label = "Overdue" if overdue else f"Due {arrival}"
            supplier_str = f" · {supplier}" if supplier else ""
            _send_multicast(
                tokens,
                title="Transit Order Due" if not overdue else "Transit Order Overdue",
                body=f"{name}{supplier_str} · {label}",
                data={"url": "/transit", "type": "transit_due", "db_path": db_path},
            )
            _mark_notified(db_path, "transit_due", dispatch_id, today)

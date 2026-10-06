from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from ..database import get_db

bp = Blueprint("mobile", __name__, url_prefix="/api/mobile")


@bp.route("/register-token", methods=["POST"])
@login_required
def register_token():
    data = request.get_json(silent=True) or {}
    token = (data.get("fcm_token") or "").strip()
    if not token:
        return jsonify({"error": "fcm_token required"}), 400

    db = get_db()
    db.execute(
        """
        INSERT INTO device_tokens (user_id, fcm_token, last_seen)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(fcm_token) DO UPDATE SET
            user_id   = excluded.user_id,
            last_seen = CURRENT_TIMESTAMP
        """,
        (current_user.id, token),
    )
    db.commit()
    return jsonify({"status": "ok"})


@bp.route("/unregister-token", methods=["DELETE"])
@login_required
def unregister_token():
    data = request.get_json(silent=True) or {}
    token = (data.get("fcm_token") or "").strip()
    if not token:
        return jsonify({"error": "fcm_token required"}), 400

    db = get_db()
    db.execute(
        "DELETE FROM device_tokens WHERE fcm_token = ? AND user_id = ?",
        (token, current_user.id),
    )
    db.commit()
    return jsonify({"status": "ok"})

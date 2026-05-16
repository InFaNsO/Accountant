import os
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

bp = Blueprint("ai", __name__, url_prefix="/ai")


@bp.route("/chat", methods=["POST"])
@login_required
def ai_chat():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "AI assistant is not configured. Set the ANTHROPIC_API_KEY environment variable."}), 503

    data    = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    history = data.get("history") or []

    if not message:
        return jsonify({"error": "Message is required."}), 400

    # Build full message list including the new user message
    messages = list(history) + [{"role": "user", "content": message}]

    try:
        from ..services.ai_service import chat
        response_text = chat(messages, current_user)
        return jsonify({
            "response": response_text,
            "history":  messages + [{"role": "assistant", "content": response_text}],
        })
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"AI error: {str(e)}"}), 500

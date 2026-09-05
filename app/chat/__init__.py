"""Chat blueprint: the streaming turn endpoint and the inbox API.

There is deliberately no conversation storage here. A turn arrives with the
whole conversation in the request body, runs, and streams back the updated
conversation for the browser to keep. The only persistence is the inbox.
"""

import json
import logging

from flask import (Blueprint, Response, abort, jsonify, request,
                   stream_with_context)
from flask_login import current_user, login_required

from . import agent, history as hist, inbox, llm
from . import sql_tool  # noqa: F401 — registers query_sql / describe_schema
from . import tools
from .tools import MODE_CHAT, MODE_HELPER

logger = logging.getLogger(__name__)

bp = Blueprint("chat", __name__, url_prefix="/chat")

MAX_TEXT = 8000


def chat_level(user):
    """'none' | 'helper' | 'agent'. The owner always gets the full surface."""
    if tools.is_god(user):
        return "agent"
    return getattr(user, "chat_level", "helper") or "helper"


def _require(level):
    """Abort unless the user has at least this level and a model is configured."""
    have = chat_level(current_user)
    if have == "none" or (level == "agent" and have != "agent"):
        abort(403)
    if not llm.provider_available():
        abort(503)


@bp.route("/api/turn", methods=["POST"])
@login_required
def turn():
    """Run one turn and stream the events back.

    Body: {mode, history, history_sig, text? | decisions?, page_context?}
    """
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") if data.get("mode") in (MODE_HELPER, MODE_CHAT) else None
    if mode is None:
        return jsonify({"error": "Unknown mode"}), 400
    _require("agent" if mode == MODE_CHAT else "helper")

    incoming = data.get("history") or []
    if incoming and not hist.verify(incoming, data.get("history_sig"),
                                    current_user.id, mode):
        # Either tampering or a stale tab from before a restart. Both are
        # recoverable by starting a fresh conversation, so say so.
        return jsonify({"error": "This conversation could not be verified. "
                                 "Start a new chat.", "reset": True}), 409

    text = (data.get("text") or "").strip()[:MAX_TEXT]
    decisions = data.get("decisions")
    if decisions is not None and not isinstance(decisions, dict):
        return jsonify({"error": "decisions must be an object"}), 400
    if not text and decisions is None:
        return jsonify({"error": "Nothing to send"}), 400

    # Resolve the caller now. The generator runs after the view returns, so it
    # must not read current_user, request or anything else ambient — under
    # streaming those are not reliably the caller's any more.
    user, uid = current_user._get_current_object(), current_user.id
    page_context = data.get("page_context")

    @stream_with_context
    def generate():
        try:
            for event, payload in agent.run_turn(
                user=user, mode=mode, history=incoming,
                text=text or None, decisions=decisions,
                page_context=page_context,
            ):
                if "history" in payload:
                    payload["history_sig"] = hist.sign(payload["history"], uid, mode)
                yield _sse(event, payload)
        except Exception:                                    # noqa: BLE001
            logger.exception("chat turn failed")
            yield _sse("error", {"message": "Something went wrong on the "
                                            "server. Please try again.",
                                 "retryable": True})
        yield ": end\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",      # nginx must not buffer an SSE stream
        "Connection": "keep-alive",
    })


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


# ── Inbox ────────────────────────────────────────────────────────────────────

@bp.route("/api/inbox")
@login_required
def inbox_list():
    _require("helper")
    rows = inbox.listing(current_user.id, limit=int(request.args.get("limit", 50)),
                         include_read=request.args.get("unread") != "1")
    return jsonify({
        "items": [inbox.to_json(r) for r in rows],
        "unread": inbox.unread_count(current_user.id),
    })


@bp.route("/api/inbox/unread")
@login_required
def inbox_unread():
    if chat_level(current_user) == "none":
        return jsonify({"unread": 0})
    return jsonify({"unread": inbox.unread_count(current_user.id)})


@bp.route("/api/inbox", methods=["POST"])
@login_required
def inbox_save():
    """Save a chat message (or anything else) to the inbox."""
    _require("helper")
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    body = data.get("body_md") or ""
    if not title and not body:
        return jsonify({"error": "Nothing to save"}), 400
    if not title:
        title = body.strip().splitlines()[0][:80] or "Saved note"
    new_id = inbox.create(current_user.id, "saved", title, body, read=True)
    return jsonify({"id": new_id, "unread": inbox.unread_count(current_user.id)})


@bp.route("/api/inbox/<int:delivery_id>/read", methods=["POST"])
@login_required
def inbox_read(delivery_id):
    _require("helper")
    inbox.mark_read(current_user.id, delivery_id)
    return jsonify({"unread": inbox.unread_count(current_user.id)})


@bp.route("/api/inbox/read-all", methods=["POST"])
@login_required
def inbox_read_all():
    _require("helper")
    inbox.mark_all_read(current_user.id)
    return jsonify({"unread": 0})


@bp.route("/api/inbox/<int:delivery_id>", methods=["DELETE"])
@login_required
def inbox_delete(delivery_id):
    _require("helper")
    inbox.delete(current_user.id, delivery_id)
    return jsonify({"unread": inbox.unread_count(current_user.id)})

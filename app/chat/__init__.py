"""Chat blueprint: the streaming turn endpoint and the inbox API.

There is deliberately no conversation storage here. A turn arrives with the
whole conversation in the request body, runs, and streams back the updated
conversation for the browser to keep. The only persistence is the inbox.
"""

import json
import logging

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request,
                   stream_with_context, url_for)
from flask_login import current_user, login_required

from . import agent, history as hist, inbox, llm
from . import scheduled  # noqa: F401 — registers the scheduling tools
from . import sql_tool   # noqa: F401 — registers query_sql / describe_schema
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


def register_template_globals(app):
    """Expose chat_level to every template so base.html can decide what to show.

    Reports 'none' when no model is configured, which hides the launcher, the
    bell and the nav item — a half-configured deploy shows no dead UI.
    """
    @app.context_processor
    def _chat_context():
        if not (current_user and current_user.is_authenticated
                and llm.provider_available()):
            return {"chat_level": "none"}
        return {"chat_level": chat_level(current_user)}


@bp.route("/")
@login_required
def index():
    """The full chat page."""
    if chat_level(current_user) != "agent":
        abort(403)
    return render_template("chat/index.html",
                           chat_available=llm.provider_available())


@bp.route("/scheduled")
@login_required
def scheduled_page():
    """Reminders and scheduled reports, with their run history."""
    _require("helper")
    tasks = []
    for row in scheduled.listing(current_user.id):
        last_run = scheduled.runs_for(row["id"], limit=1)
        tasks.append({
            "id": row["id"], "kind": row["kind"], "name": row["name"],
            "prompt": row["prompt"],
            "schedule_words": scheduled.describe_schedule(row),
            "next_words": _ist(row["next_run_at"]),
            "last_words": _ist(row["last_run_at"]),
            "last_status": last_run[0]["status"] if last_run else None,
        })

    history = []
    for run in _recent_runs(current_user.id):
        history.append({
            "task_name": run["name"], "status": run["status"],
            "when_words": run["started_at"], "error": run["error"],
            "headline": (run["report_md"] or "").strip().splitlines()[0][:120]
                        if run["report_md"] else "",
        })

    return render_template(
        "chat/scheduled.html", tasks=tasks, history=history,
        dispatcher_running=_dispatcher_running(),
    )


def _ist(stored):
    """A stored UTC timestamp as a short IST string."""
    when = scheduled._load(stored)
    return f"{when.astimezone(scheduled.IST):%a %d %b, %H:%M}" if when else None


def _recent_runs(user_id, limit=10):
    from ..database import get_db
    return get_db().execute(
        """SELECT r.*, t.name FROM chat_task_runs r
             JOIN chat_tasks t ON t.id = r.task_id
            WHERE t.user_id = ? ORDER BY r.id DESC LIMIT ?""",
        (user_id, limit)).fetchall()


def _dispatcher_running():
    try:
        import apscheduler  # noqa: F401
        return True
    except ImportError:
        return False


@bp.route("/scheduled/<int:task_id>/cancel", methods=["POST"])
@login_required
def scheduled_cancel(task_id):
    _require("helper")
    row = scheduled.get(current_user.id, task_id)
    if row is None:
        abort(404)
    scheduled.cancel(current_user.id, task_id)
    flash(f"Cancelled “{row['name']}”.", "success")
    return redirect(url_for("chat.scheduled_page"))


@bp.route("/scheduled/<int:task_id>/run", methods=["POST"])
@login_required
def scheduled_run_now(task_id):
    """Run a saved report immediately — the same path the dispatcher takes."""
    _require("helper")
    row = scheduled.get(current_user.id, task_id)
    if row is None or row["kind"] != "report":
        abort(404)
    run_id = scheduled.run_report(current_app._get_current_object(), row)
    from ..database import get_db
    run = get_db().execute("SELECT status, error FROM chat_task_runs WHERE id=?",
                           (run_id,)).fetchone()
    if run and run["status"] == "ok":
        flash(f"“{row['name']}” ran — the result is in your inbox.", "success")
    else:
        flash(f"“{row['name']}” failed: {(run['error'] if run else 'unknown error')}",
              "error")
    return redirect(url_for("chat.scheduled_page"))


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

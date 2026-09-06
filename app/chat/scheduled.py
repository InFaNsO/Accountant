"""Reminders and scheduled reports.

Two kinds of saved task, one dispatcher:

  reminder — a line of text delivered at a time. No model call, so it costs
             nothing and cannot fail for an interesting reason.
  report   — a saved prompt run headlessly with read-only tools, whose output
             is kept and delivered.

Both land in the inbox, which is the only place assistant output persists, and
both push to the phone through the notification service already in the app.

The dispatcher runs in-process on the existing APScheduler instance. Gunicorn
runs more than one worker and each starts its own scheduler, so claiming a due
task is a compare-and-set on next_run_at: whichever worker's UPDATE actually
changes a row owns that firing, and the others see zero rows and move on.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..database import get_db
from . import inbox
from .tools import ToolError, local_tool

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
TZ_NAME = "Asia/Kolkata"

MAX_TASKS_PER_USER = 100
RUNS_KEPT_PER_TASK = 10
REMINDER_RETENTION_DAYS = 30


# ── Time helpers ─────────────────────────────────────────────────────────────

def _utc_now():
    return datetime.now(timezone.utc)


def _store(dt):
    """UTC, second precision, as a string SQLite can compare lexicographically."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _load(text):
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_when(when):
    """A local (IST) datetime from the model, as UTC.

    The model is given the current IST date and time, so it resolves "tomorrow
    at 9" itself and passes an ISO timestamp. Anything it cannot resolve should
    come back as an error it can read, not a silently wrong time.
    """
    if not when:
        raise ToolError("A date and time is required, as YYYY-MM-DDTHH:MM.")
    text = str(when).strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError(
            f"Could not read '{when}' as a date and time. Use YYYY-MM-DDTHH:MM "
            f"in IST, for example 2026-09-07T09:00."
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    if dt <= datetime.now(IST):
        raise ToolError(
            f"{dt:%d %b %Y %H:%M} is in the past. Ask the user which future "
            f"date and time they meant."
        )
    return dt.astimezone(timezone.utc)


def next_from_cron(cron, after=None):
    """Next firing of a crontab expression, in UTC."""
    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        raise ToolError("Recurring schedules need APScheduler installed on the server.")
    try:
        import zoneinfo
        trigger = CronTrigger.from_crontab(cron, timezone=zoneinfo.ZoneInfo(TZ_NAME))
    except Exception:                                        # noqa: BLE001
        raise ToolError(
            f"'{cron}' is not a valid schedule. Use crontab form: "
            f"'0 9 * * *' is 9am daily, '0 9 * * MON' is 9am every Monday, "
            f"'0 9 1 * *' is 9am on the 1st."
        )
    base = (after or _utc_now()).astimezone(IST)
    nxt = trigger.get_next_fire_time(None, base)
    if nxt is None:
        raise ToolError(f"'{cron}' will never fire.")
    return nxt.astimezone(timezone.utc)


def describe_schedule(row):
    """The schedule in words, for the confirmation card and the list."""
    if row["cron"]:
        return f"repeats: {_cron_words(row['cron'])}"
    due = _load(row["due_at"])
    return f"once, on {due.astimezone(IST):%a %d %b %Y at %H:%M} IST" if due else "unscheduled"


_DOW = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
        "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday",
        "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
        "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}


def _cron_words(cron):
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    try:
        at = f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return cron
    if dow != "*" and dom == "*":
        return f"every {_DOW.get(dow.upper(), dow)} at {at} IST"
    if dom != "*":
        return f"on day {dom} of each month at {at} IST"
    return f"every day at {at} IST"


# ── Storage ──────────────────────────────────────────────────────────────────

def create(user_id, kind, name, *, prompt=None, when=None, cron=None,
           link_entity=None, link_id=None):
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) c FROM chat_tasks WHERE user_id=? AND enabled=1",
        (user_id,)).fetchone()["c"]
    if count >= MAX_TASKS_PER_USER:
        raise ToolError(f"You already have {count} active items. Cancel some first.")

    due_at = next_at = None
    if cron:
        next_at = next_from_cron(cron)
    else:
        due_at = parse_when(when)
        next_at = due_at

    cur = db.execute(
        """INSERT INTO chat_tasks
               (user_id, kind, name, prompt, due_at, cron, timezone,
                link_entity, link_id, enabled, next_run_at)
           VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
        (user_id, kind, name[:200], prompt, _store(due_at) if due_at else None,
         cron, TZ_NAME, link_entity, link_id, _store(next_at)),
    )
    db.commit()
    return cur.lastrowid


def listing(user_id, include_disabled=False):
    sql = "SELECT * FROM chat_tasks WHERE user_id=?"
    if not include_disabled:
        sql += " AND enabled=1"
    sql += " ORDER BY enabled DESC, next_run_at"
    return get_db().execute(sql, (user_id,)).fetchall()


def get(user_id, task_id):
    return get_db().execute(
        "SELECT * FROM chat_tasks WHERE id=? AND user_id=?", (task_id, user_id)
    ).fetchone()


def cancel(user_id, task_id):
    db = get_db()
    cur = db.execute(
        "UPDATE chat_tasks SET enabled=0, next_run_at=NULL WHERE id=? AND user_id=?",
        (task_id, user_id))
    db.commit()
    return cur.rowcount > 0


def runs_for(task_id, limit=RUNS_KEPT_PER_TASK):
    return get_db().execute(
        "SELECT * FROM chat_task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?",
        (task_id, limit)).fetchall()


# ── Dispatch ─────────────────────────────────────────────────────────────────

def dispatch_due(app, now=None):
    """Fire everything that is due. Returns how many tasks this worker claimed."""
    fired = 0
    with app.app_context():
        db = get_db()
        now = now or _utc_now()
        due = db.execute(
            """SELECT * FROM chat_tasks
                WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at LIMIT 20""",
            (_store(now),)).fetchall()
        for task in due:
            if not _claim(db, task, now):
                continue          # another worker got there first
            fired += 1
            try:
                if task["kind"] == "reminder":
                    _fire_reminder(task)
                else:
                    run_report(app, task)
            except Exception:                                # noqa: BLE001
                logger.exception("scheduled task %s failed", task["id"])
    return fired


def _claim(db, task, now):
    """Compare-and-set on next_run_at. Exactly one worker wins a given firing."""
    if task["cron"]:
        upcoming = _store(next_from_cron(task["cron"], now))
        still_enabled = 1
    else:
        upcoming, still_enabled = None, 0      # one-shot: done after this
    cur = db.execute(
        """UPDATE chat_tasks
              SET next_run_at=?, last_run_at=?, enabled=?
            WHERE id=? AND next_run_at=? AND enabled=1""",
        (upcoming, _store(now), still_enabled, task["id"], task["next_run_at"]),
    )
    db.commit()
    return cur.rowcount == 1


def _fire_reminder(task):
    body = task["prompt"] or ""
    delivery_id = inbox.create(
        task["user_id"], "reminder", task["name"], body,
        task_id=task["id"], link_entity=task["link_entity"], link_id=task["link_id"])
    _push(task["user_id"], task["name"], body[:120] or "Reminder", delivery_id)
    return delivery_id


def run_report(app, task):
    """Run a saved prompt headlessly and deliver the result."""
    from . import agent

    db = get_db()
    cur = db.execute(
        "INSERT INTO chat_task_runs (task_id, status, claimed_by) VALUES (?, 'running', ?)",
        (task["id"], "worker"))
    run_id = cur.lastrowid
    db.commit()

    from ..services.auth_service import load_user
    owner = load_user(task["user_id"])
    if owner is None:
        _finish(run_id, "error", error="The owner of this report no longer exists.")
        return run_id

    text_parts, tool_log, tokens = [], [], 0
    try:
        for event, payload in agent.run_turn(
            user=owner, mode="scheduled", history=[], text=task["prompt"]):
            if event == "text":
                text_parts.append(payload["delta"])
            elif event == "tool_end":
                tool_log.append({"tool": payload["name"], "ok": payload["ok"],
                                 "ms": payload["ms"]})
            elif event == "usage":
                tokens = payload.get("prompt", 0) + payload.get("completion", 0)
            elif event == "error":
                raise RuntimeError(payload.get("message", "unknown error"))
    except Exception as e:                                   # noqa: BLE001
        logger.exception("report %s failed", task["id"])
        _finish(run_id, "error", error=str(e), tool_log=tool_log)
        delivery_id = inbox.create(
            task["user_id"], "report", f"{task['name']} — failed",
            f"This scheduled report could not run:\n\n> {e}\n\n"
            f"It will try again at its next scheduled time.",
            task_id=task["id"], run_id=run_id)
        _push(task["user_id"], f"{task['name']} failed", str(e)[:120], delivery_id)
        return run_id

    report = "".join(text_parts).strip() or "The report produced no output."
    _finish(run_id, "ok", report=report, tool_log=tool_log, tokens=tokens)
    delivery_id = inbox.create(task["user_id"], "report", task["name"], report,
                               task_id=task["id"], run_id=run_id)
    _push(task["user_id"], task["name"], _headline(report), delivery_id)
    _prune_runs(task["id"])
    return run_id


def _finish(run_id, status, report=None, tool_log=None, error=None, tokens=0):
    db = get_db()
    db.execute(
        """UPDATE chat_task_runs
              SET status=?, finished_at=CURRENT_TIMESTAMP, report_md=?,
                  tool_log=?, error=?, tokens=?
            WHERE id=?""",
        (status, report, json.dumps(tool_log or []), error, tokens, run_id))
    db.commit()


def _headline(report):
    for line in report.splitlines():
        clean = line.strip().lstrip("#* ").strip()
        if clean:
            return clean[:120]
    return "Report ready"


def _prune_runs(task_id):
    db = get_db()
    db.execute(
        """DELETE FROM chat_task_runs
            WHERE task_id=? AND id NOT IN (
                SELECT id FROM chat_task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?)""",
        (task_id, task_id, RUNS_KEPT_PER_TASK))
    db.commit()


def _push(user_id, title, body, delivery_id):
    """Notify this one user's devices. Never let a push failure lose the item —
    it is already in the inbox by the time we get here."""
    try:
        from ..services import notification_service as ns
        if not getattr(ns, "_firebase_ready", False):
            return
        rows = get_db().execute(
            "SELECT fcm_token FROM device_tokens WHERE user_id=?", (user_id,)).fetchall()
        tokens = [r["fcm_token"] for r in rows]
        if tokens:
            ns._send_multicast(tokens, title, body,
                               {"type": "inbox", "delivery_id": str(delivery_id)})
    except Exception:                                        # noqa: BLE001
        logger.exception("push for delivery %s failed", delivery_id)


def purge_old(app):
    """Housekeeping: drop delivered one-shot reminders after a month."""
    with app.app_context():
        db = get_db()
        cutoff = _store(_utc_now() - timedelta(days=REMINDER_RETENTION_DAYS))
        db.execute(
            """DELETE FROM chat_tasks
                WHERE kind='reminder' AND enabled=0 AND cron IS NULL
                  AND last_run_at IS NOT NULL AND last_run_at < ?""", (cutoff,))
        db.commit()


# ── Tools ────────────────────────────────────────────────────────────────────

@local_tool(
    "create_reminder",
    "Schedule a reminder to be delivered to the user's inbox at a time, with a "
    "phone notification. Use this whenever the user asks to be reminded of "
    "something. For a one-off use `when`; for something recurring use `repeat`. "
    "Resolve relative wording ('tomorrow at 9', 'next Friday') against the "
    "current IST date and time given to you, and pass an exact timestamp.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "Short line the user will see, e.g. "
                                     "'Call Sharma about INV-231'."},
            "when": {"type": "string",
                     "description": "One-off time in IST as YYYY-MM-DDTHH:MM. "
                                    "Omit if using repeat."},
            "repeat": {"type": "string",
                       "description": "Crontab for a recurring reminder, e.g. "
                                      "'0 9 * * MON' for 9am every Monday. "
                                      "Omit for a one-off."},
            "note": {"type": "string",
                     "description": "Optional extra detail shown with the reminder."},
        },
        "required": ["title"],
    },
    modes=("helper", "chat"),
)
def create_reminder(user=None, title="", when=None, repeat=None, note=""):
    if not (title or "").strip():
        raise ToolError("A title is required.")
    if not when and not repeat:
        raise ToolError("Give either `when` for a one-off or `repeat` for a "
                        "recurring reminder.")
    task_id = create(user.id, "reminder", title.strip(), prompt=note or "",
                     when=when, cron=repeat)
    row = get(user.id, task_id)
    return (f"Reminder set: “{title.strip()}” — {describe_schedule(row)}. "
            f"It will appear in the inbox and notify the phone.")


@local_tool(
    "create_scheduled_report",
    "Save a request to be run automatically on a schedule, with the result "
    "delivered to the user's inbox. Use this when the user wants something "
    "regularly ('every Monday', 'each month end'). The prompt is run later with "
    "read-only tools and nobody present, so write it as a complete, "
    "self-contained instruction.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "Short name, e.g. 'Weekly overdue invoices'."},
            "prompt": {"type": "string",
                       "description": "The full request to run each time."},
            "repeat": {"type": "string",
                       "description": "Crontab in IST, e.g. '0 9 * * MON' for "
                                      "9am every Monday, '0 9 1 * *' for the 1st."},
        },
        "required": ["name", "prompt", "repeat"],
    },
    modes=("helper", "chat"),
)
def create_scheduled_report(user=None, name="", prompt="", repeat=""):
    if not (name or "").strip() or not (prompt or "").strip():
        raise ToolError("Both a name and a prompt are required.")
    task_id = create(user.id, "report", name.strip(), prompt=prompt.strip(), cron=repeat)
    row = get(user.id, task_id)
    return (f"Report scheduled: “{name.strip()}” — {describe_schedule(row)}. "
            f"Results arrive in the inbox.")


@local_tool(
    "list_scheduled",
    "List the user's reminders and scheduled reports, with when each next runs.",
    {"type": "object", "properties": {}},
    modes=("helper", "chat"),
)
def list_scheduled(user=None):
    rows = listing(user.id)
    if not rows:
        return "Nothing scheduled."
    lines = []
    for r in rows:
        nxt = _load(r["next_run_at"])
        lines.append(
            f"#{r['id']} [{r['kind']}] {r['name']} — {describe_schedule(r)}"
            + (f"; next {nxt.astimezone(IST):%d %b %H:%M} IST" if nxt else ""))
    return "\n".join(lines)


@local_tool(
    "cancel_scheduled",
    "Cancel a reminder or scheduled report by its id. Call list_scheduled first "
    "if you do not know the id.",
    {
        "type": "object",
        "properties": {"task_id": {"type": "integer",
                                   "description": "The id from list_scheduled."}},
        "required": ["task_id"],
    },
    modes=("helper", "chat"),
)
def cancel_scheduled(user=None, task_id=None):
    row = get(user.id, task_id)
    if row is None:
        raise ToolError(f"No scheduled item #{task_id} belonging to you.")
    cancel(user.id, task_id)
    return f"Cancelled “{row['name']}”."

"""The inbox — the one place chat output is allowed to persist.

Conversations are deliberately throwaway: they live in the browser tab and are
gone when it closes. That is fine for "what's their balance", and wrong for
"here is the overdue report you asked for every Monday". The inbox is the
bridge. Three things land in it:

  saved     — the user pressed Save on a chat answer worth keeping
  reminder  — a scheduled reminder came due
  report    — a scheduled report finished running

All three render the same way and are read the same way, so the drawer needs
one list and one card.
"""

from datetime import datetime

from ..database import get_db
from .tools import ToolError, local_tool

MAX_TITLE = 120
MAX_BODY = 20_000


def create(user_id, kind, title, body_md="", *, task_id=None, run_id=None,
           link_entity=None, link_id=None, read=False):
    """Add a delivery. Returns its id."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO chat_deliveries
               (user_id, kind, task_id, run_id, title, body_md,
                link_entity, link_id, read_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (user_id, kind, task_id, run_id, (title or "Untitled")[:MAX_TITLE],
         (body_md or "")[:MAX_BODY], link_entity, link_id,
         datetime.now().isoformat(" ", "seconds") if read else None),
    )
    db.commit()
    return cur.lastrowid


def listing(user_id, *, limit=50, include_read=True):
    """Newest first. Snoozed items stay hidden until their time comes."""
    sql = ["""SELECT d.*, (SELECT COUNT(*) FROM chat_files f
                             WHERE f.delivery_id = d.id) AS file_count
                FROM chat_deliveries d
               WHERE d.user_id = ?
                 AND (d.snoozed_until IS NULL
                      OR d.snoozed_until <= CURRENT_TIMESTAMP)"""]
    params = [user_id]
    if not include_read:
        sql.append("AND d.read_at IS NULL")
    sql.append("ORDER BY d.created_at DESC LIMIT ?")
    params.append(limit)
    return get_db().execute(" ".join(sql), params).fetchall()


def unread_count(user_id):
    row = get_db().execute(
        """SELECT COUNT(*) AS n FROM chat_deliveries
            WHERE user_id = ? AND read_at IS NULL
              AND (snoozed_until IS NULL OR snoozed_until <= CURRENT_TIMESTAMP)""",
        (user_id,),
    ).fetchone()
    return row["n"] if row else 0


def files_for(delivery_id):
    return get_db().execute(
        "SELECT id, token, filename, mime, size FROM chat_files "
        "WHERE delivery_id = ? ORDER BY id", (delivery_id,),
    ).fetchall()


def mark_read(user_id, delivery_id):
    db = get_db()
    db.execute(
        "UPDATE chat_deliveries SET read_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND user_id = ? AND read_at IS NULL",
        (delivery_id, user_id),
    )
    db.commit()


def mark_all_read(user_id):
    db = get_db()
    db.execute(
        "UPDATE chat_deliveries SET read_at = CURRENT_TIMESTAMP "
        "WHERE user_id = ? AND read_at IS NULL", (user_id,),
    )
    db.commit()


def snooze(user_id, delivery_id, until_iso):
    db = get_db()
    db.execute(
        "UPDATE chat_deliveries SET snoozed_until = ?, read_at = NULL "
        "WHERE id = ? AND user_id = ?", (until_iso, delivery_id, user_id),
    )
    db.commit()


def delete(user_id, delivery_id):
    db = get_db()
    db.execute("DELETE FROM chat_files WHERE delivery_id IN "
               "(SELECT id FROM chat_deliveries WHERE id=? AND user_id=?)",
               (delivery_id, user_id))
    db.execute("DELETE FROM chat_deliveries WHERE id = ? AND user_id = ?",
               (delivery_id, user_id))
    db.commit()


def to_json(row, with_files=True):
    data = {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "body_md": row["body_md"],
        "created_at": row["created_at"],
        "read": bool(row["read_at"]),
        "link": ({"entity": row["link_entity"], "id": row["link_id"]}
                 if row["link_entity"] else None),
        "task_id": row["task_id"],
    }
    if with_files and (row.keys() and "file_count" in row.keys()
                       and row["file_count"]):
        data["files"] = [
            {"token": f["token"], "filename": f["filename"], "size": f["size"]}
            for f in files_for(row["id"])
        ]
    return data


@local_tool(
    "save_to_inbox",
    "Save a note to the user's inbox so it survives after this conversation is "
    "closed. Chat history is not stored anywhere, so use this whenever the user "
    "asks you to remember, keep, save or note something down — and offer it "
    "when you produce a summary or figure they will clearly want again later. "
    "Put the full content in body_md as markdown; it is shown as-is.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "Short label for the inbox list."},
            "body_md": {"type": "string",
                        "description": "The content to keep, as markdown."},
        },
        "required": ["title", "body_md"],
    },
    modes=("helper", "chat"),
)
def save_to_inbox(user=None, title="", body_md=""):
    if not (title or "").strip():
        raise ToolError("A title is required.")
    create(user.id, "saved", title.strip(), body_md, read=True)
    return f"Saved to the inbox as “{title.strip()[:MAX_TITLE]}”."

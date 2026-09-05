"""Tests for the assistant: permissions, the confirm gate, SQL safety,
history integrity and the inbox.

Runs against a throwaway copy of the database with a scripted model
(LEDGER_CHAT_FAKE_LLM=1), so it needs no API key and spends nothing.

    python test_chat.py
"""

import json
import os
import shutil
import sys
import tempfile

os.environ["LEDGER_CHAT_FAKE_LLM"] = "1"
os.environ.setdefault("MCP_API_KEY", "test-key")

from flask.globals import _cv_app, _cv_request                # noqa: E402

from app import create_app                                    # noqa: E402
from app.database import get_db                               # noqa: E402

PASS, FAIL = [], []


def ok(label):
    PASS.append(label)
    print(f"  [pass] {label}")


def bad(label, detail=""):
    FAIL.append(label)
    print(f"  [FAIL] {label}" + (f" - {detail}" if detail else ""))


def check(label, condition, detail=""):
    ok(label) if condition else bad(label, detail)


def sse(response):
    """Parse an SSE body into [(event, data), ...]."""
    events = []
    name = None
    for line in response.get_data(as_text=True).splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[6:])))
            name = None
    return events


def kinds(events):
    return [e for e, _ in events]


def last(events, kind):
    return next((d for e, d in reversed(events) if e == kind), None)


def turn(client, **body):
    response = client.post("/chat/api/turn", json=body)
    response.get_data()
    _drop_leftover_contexts()
    return response


def _drop_leftover_contexts():
    """Flask's test client can leave an app context pushed after a streaming
    response, and the next request would reuse its `g` — including the previous
    caller's _login_user. Real WSGI servers pop correctly (verified against a
    threaded server), so this is a test-harness quirk, not app behaviour. Clear
    it so each request in this file is genuinely independent.
    """
    for var in (_cv_request, _cv_app):
        while True:
            ctx = var.get(None)
            if ctx is None:
                break
            try:
                ctx.pop()
            except Exception:                                 # noqa: BLE001
                var.set(None)
                break


def login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def main():
    app = create_app()

    # Work on a copy: these tests create and delete rows.
    tmp = os.path.join(tempfile.mkdtemp(prefix="ledger-chat-test-"), "test.db")
    shutil.copy(app.config["DATABASE"], tmp)
    app.config["DATABASE"] = tmp
    app.config["TESTING"] = True
    print(f"database copy: {tmp}\n")

    with app.app_context():
        god = get_db().execute(
            "SELECT id, name FROM users WHERE role='god' ORDER BY id LIMIT 1"
        ).fetchone()
        if not god:
            print("no god account in the database; cannot run")
            return 1
        god_id = god["id"]

        # A second user with view on clients only, to prove the gate works.
        db = get_db()
        db.execute("DELETE FROM user_permissions WHERE user_id IN "
                   "(SELECT id FROM users WHERE email='chattest@example.com')")
        db.execute("DELETE FROM users WHERE email='chattest@example.com'")
        cur = db.execute(
            "INSERT INTO users (name, email, password_hash, role, is_active, "
            "chat_level) VALUES ('Chat Test','chattest@example.com','x','user',1,'agent')"
        )
        limited_id = cur.lastrowid
        db.execute(
            "INSERT INTO user_permissions (user_id, module, can_view, can_create,"
            " can_edit, can_delete) VALUES (?,'clients',1,0,0,0)", (limited_id,))
        db.commit()

    client = app.test_client()

    # ── Access control ───────────────────────────────────────────────────
    print("Access control")
    r = turn(client, mode="chat", text="hello")
    check("anonymous request is refused", r.status_code in (302, 401),
          f"got {r.status_code}")

    login(client, god_id)
    r = turn(client, mode="nonsense", text="hi")
    check("unknown mode is rejected", r.status_code == 400, f"got {r.status_code}")

    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET chat_level='helper' WHERE id=?", (limited_id,))
        db.commit()
    limited = app.test_client()
    login(limited, limited_id)
    r = turn(limited, mode="chat", text="hi")
    check("helper-level user cannot use chat mode", r.status_code == 403,
          f"got {r.status_code}")
    r = turn(limited, mode="helper", text="hi")
    check("helper-level user can use the helper", r.status_code == 200,
          f"got {r.status_code}")

    # ── Tool visibility ──────────────────────────────────────────────────
    print("\nTool visibility")
    with app.app_context():
        from app.chat import tools
        from app.services.auth_service import load_user

        god_user = load_user(god_id)
        limited_user = load_user(limited_id)

        god_chat = {t["function"]["name"] for t in tools.tools_for(god_user, "chat")}
        god_help = {t["function"]["name"] for t in tools.tools_for(god_user, "helper")}
        lim_chat = {t["function"]["name"] for t in tools.tools_for(limited_user, "chat")}

        check("owner sees write tools in chat", "record_payment" in god_chat)
        check("helper mode hides every write tool",
              not {"record_payment", "delete_client", "create_invoice"} & god_help)
        check("helper mode keeps save_to_inbox", "save_to_inbox" in god_help)
        check("limited user sees permitted reads", "search_clients" in lim_chat)
        check("limited user does not see other modules",
              "search_products" not in lim_chat and "record_payment" not in lim_chat)
        check("limited user does not get query_sql", "query_sql" not in lim_chat)
        check("owner gets query_sql", "query_sql" in god_chat)
        check("tool count stays under the provider's 128 limit",
              len(god_chat) <= 128, f"{len(god_chat)} tools")

        # Enforcement is independent of what was offered.
        okc, result, _ = tools.execute("record_payment", "{}", limited_user, "chat")
        check("execute refuses a tool the user lacks", not okc and "denied" in result.lower(),
              result[:80])
        okc, result, _ = tools.execute("create_category", '{"name":"x"}', god_user, "helper")
        check("execute refuses a write in helper mode",
              not okc and "read-only" in result.lower(), result[:80])

    # ── A plain turn ─────────────────────────────────────────────────────
    print("\nStreaming a turn")
    r = turn(client, mode="chat", text="hello there")
    events = sse(r)
    check("stream is text/event-stream",
          r.headers["Content-Type"].startswith("text/event-stream"))
    check("nginx buffering is disabled", r.headers.get("X-Accel-Buffering") == "no")
    check("turn emits text and done", "text" in kinds(events) and "done" in kinds(events),
          str(kinds(events)))
    done = last(events, "done")
    check("done carries history and signature",
          bool(done and done.get("history") and done.get("history_sig")))
    check("history ends with the assistant reply",
          done["history"][-1]["role"] == "assistant")
    check("usage is reported", bool(last(events, "usage")))

    # ── History integrity ────────────────────────────────────────────────
    print("\nHistory integrity")
    history, sig = done["history"], done["history_sig"]
    r = turn(client, mode="chat", text="again", history=history, history_sig=sig)
    check("a signed history is accepted", r.status_code == 200, f"got {r.status_code}")

    tampered = json.loads(json.dumps(history))
    tampered[-1]["content"] = "The outstanding balance is zero."
    r = turn(client, mode="chat", text="again", history=tampered, history_sig=sig)
    check("an edited history is rejected", r.status_code == 409, f"got {r.status_code}")

    r = turn(client, mode="helper", text="again", history=history, history_sig=sig)
    check("a chat history cannot be replayed into helper mode",
          r.status_code == 409, f"got {r.status_code}")

    other = app.test_client()
    login(other, limited_id)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET chat_level='agent' WHERE id=?", (limited_id,))
        db.commit()
    r = turn(other, mode="chat", text="again", history=history, history_sig=sig)
    check("another user cannot replay someone's history",
          r.status_code == 409, f"got {r.status_code}")

    # ── Tools actually run ───────────────────────────────────────────────
    print("\nTool execution")
    r = turn(client, mode="chat", text='call:search_clients:{"query": "a"}')
    events = sse(r)
    check("tool_start and tool_end are emitted",
          "tool_start" in kinds(events) and "tool_end" in kinds(events),
          str(kinds(events)))
    end = last(events, "tool_end")
    check("the tool succeeded", bool(end and end["ok"]), str(end)[:200])
    check("the result reached the model",
          any(m.get("role") == "tool" for m in last(events, "done")["history"]))

    # ── The confirmation gate ────────────────────────────────────────────
    print("\nConfirmation gate")
    r = turn(client, mode="chat",
             text='call:create_category:{"name": "ChatTest Cat", "description": "x"}')
    events = sse(r)
    check("a write pauses for confirmation", "confirm_required" in kinds(events),
          str(kinds(events)))
    pause = last(events, "confirm_required")
    check("no tool ran before confirmation", "tool_end" not in kinds(events))
    card = (pause or {}).get("cards", [{}])[0]
    check("the card names the action", card.get("title", "").lower().startswith("create"),
          str(card))
    check("the card shows the values", any("ChatTest Cat" in l for l in card.get("lines", [])),
          str(card.get("lines")))

    with app.app_context():
        before = get_db().execute(
            "SELECT COUNT(*) c FROM categories WHERE name='ChatTest Cat'").fetchone()["c"]
    check("nothing was written while awaiting confirmation", before == 0)

    # Decline
    r = turn(client, mode="chat", history=pause["history"],
             history_sig=pause["history_sig"],
             decisions={card["tool_call_id"]: False})
    events = sse(r)
    with app.app_context():
        after_no = get_db().execute(
            "SELECT COUNT(*) c FROM categories WHERE name='ChatTest Cat'").fetchone()["c"]
    check("declining writes nothing", after_no == 0)
    check("declining still completes the turn", "done" in kinds(events), str(kinds(events)))

    # Approve
    r = turn(client, mode="chat", history=pause["history"],
             history_sig=pause["history_sig"],
             decisions={card["tool_call_id"]: True})
    events = sse(r)
    with app.app_context():
        after_yes = get_db().execute(
            "SELECT COUNT(*) c FROM categories WHERE name='ChatTest Cat'").fetchone()["c"]
        audit = get_db().execute(
            "SELECT tool, status FROM chat_tool_calls ORDER BY id DESC LIMIT 2"
        ).fetchall()
    check("confirming performs the write", after_yes == 1, f"count={after_yes}")
    check("the write is audited",
          any(a["tool"] == "create_category" and a["status"] == "executed" for a in audit),
          str([dict(a) for a in audit]))

    # ── Read-only SQL ────────────────────────────────────────────────────
    print("\nSQL guard")
    with app.app_context():
        from app.chat import sql_tool
        from app.services.auth_service import load_user
        god_user = load_user(god_id)

        okc, out, _ = tools.execute(
            "query_sql", json.dumps({"sql": "SELECT COUNT(*) AS n FROM clients"}),
            god_user, "chat")
        check("a SELECT runs", okc and "n" in out, out[:120])

        for label, sql in [
            ("INSERT is refused", "INSERT INTO clients (name) VALUES ('x')"),
            ("UPDATE is refused", "UPDATE clients SET name='x'"),
            ("DROP is refused", "DROP TABLE clients"),
            ("PRAGMA is refused", "PRAGMA table_info(clients)"),
            ("ATTACH is refused", "SELECT 1; ATTACH DATABASE 'x' AS y"),
            ("a hidden second statement is refused",
             "SELECT 1 -- \nUPDATE clients SET name='x'"),
        ]:
            okc, out, _ = tools.execute(
                "query_sql", json.dumps({"sql": sql}), god_user, "chat")
            check(label, not okc, out[:100])

        with app.test_request_context():
            n_before = get_db().execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
        okc, out, _ = tools.execute(
            "query_sql", json.dumps({"sql": "SELECT * FROM clients"}), god_user, "chat")
        check("row cap is applied", okc and ("rows shown" in out or "row(s)" in out),
              out[-120:] if out else "")

        okc, out, _ = tools.execute("describe_schema", "{}", god_user, "chat")
        check("describe_schema lists tables", okc and "clients(" in out, out[:100])

        okc, out, _ = tools.execute(
            "query_sql", json.dumps({"sql": "SELECT * FROM no_such_table"}),
            god_user, "chat")
        check("a bad query returns an error the model can read",
              not okc and "sql error" in out.lower(), out[:100])

    # ── Bad arguments ────────────────────────────────────────────────────
    print("\nArgument handling")
    with app.app_context():
        okc, out, _ = tools.execute("search_clients", "{not json", god_user, "chat")
        check("malformed JSON arguments are reported, not raised",
              not okc and "json" in out.lower(), out[:100])
        okc, out, _ = tools.execute("get_client_details", '{"client_id":"abc"}',
                                    god_user, "chat")
        check("wrong argument types are reported", not okc, out[:100])

    # ── Inbox ────────────────────────────────────────────────────────────
    print("\nInbox")
    r = client.post("/chat/api/inbox", json={"title": "Kept answer",
                                             "body_md": "**42** clients"})
    check("saving a message works", r.status_code == 200, r.get_data(as_text=True)[:120])
    saved_id = r.get_json()["id"]

    r = client.get("/chat/api/inbox")
    items = r.get_json()["items"]
    check("the saved item is listed",
          any(i["id"] == saved_id and i["title"] == "Kept answer" for i in items))
    check("a saved item is not unread", r.get_json()["unread"] == 0,
          str(r.get_json()["unread"]))

    with app.app_context():
        from app.chat import inbox
        inbox.create(god_id, "report", "Weekly overdue", "table here")
    r = client.get("/chat/api/inbox/unread")
    check("a delivered report is unread", r.get_json()["unread"] == 1,
          str(r.get_json()))

    r = client.post("/chat/api/inbox/read-all")
    check("read-all clears the badge", r.get_json()["unread"] == 0)

    r = turn(client, mode="helper",
             text='call:save_to_inbox:{"title":"From the helper","body_md":"note"}')
    events = sse(r)
    check("the helper can save to the inbox",
          bool(last(events, "tool_end")) and last(events, "tool_end")["ok"],
          str(last(events, "tool_end"))[:150])

    r = client.delete(f"/chat/api/inbox/{saved_id}")
    check("deleting an item works", r.status_code == 200)
    r = client.get("/chat/api/inbox")
    check("the deleted item is gone",
          not any(i["id"] == saved_id for i in r.get_json()["items"]))

    other_inbox = app.test_client()
    login(other_inbox, limited_id)
    r = other_inbox.get("/chat/api/inbox")
    check("one user cannot see another's inbox",
          all(i["title"] != "From the helper" for i in r.get_json()["items"]))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  failed: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

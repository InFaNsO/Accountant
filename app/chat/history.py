"""Conversation history lives in the browser, so the server has to be able to
trust what comes back.

Every response hands the client the updated history plus an HMAC over it,
bound to the user and the mode. On the next request we verify that signature
before feeding anything to the model — otherwise a client could invent tool
results ("the balance is zero"), replay another user's conversation, or
promote a helper thread into agent mode.

Tools still run server-side under the real user's permissions regardless, so
this protects the conversation's integrity, not the data.
"""

import hashlib
import hmac
import json

from flask import current_app

# Roughly how much of the conversation we carry forward. Tool results are the
# bulky part, so they get stubbed out first; only then do we drop old turns.
MAX_MESSAGES = 60
KEEP_FULL_RESULTS_FOR_TURNS = 2
MAX_RESULT_CHARS = 20_000
STUB = "[earlier tool result omitted to save space — call the tool again if needed]"


def _canonical(history, user_id, mode):
    return json.dumps(
        {"h": history, "u": int(user_id), "m": mode},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def sign(history, user_id, mode):
    """HMAC for a history, bound to who owns it and which mode produced it."""
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, _canonical(history, user_id, mode), hashlib.sha256).hexdigest()


def verify(history, signature, user_id, mode):
    """True when this history is one we produced for this user and mode."""
    if not signature or not isinstance(history, list):
        return False
    return hmac.compare_digest(sign(history, user_id, mode), str(signature))


def trim(history):
    """Shrink a history before handing it back to the browser.

    Old tool results become a stub — the model can always call the tool again,
    and keeping every `products_snapshot` payload would blow past sessionStorage
    long before it helped. Turn structure is preserved: an assistant message
    with tool_calls always keeps its matching tool messages, so the history
    stays valid to send back to the model.
    """
    out = [dict(m) for m in history]

    # Index the user turns so "the last two turns" is well defined.
    user_positions = [i for i, m in enumerate(out) if m.get("role") == "user"]
    cutoff = (user_positions[-KEEP_FULL_RESULTS_FOR_TURNS]
              if len(user_positions) > KEEP_FULL_RESULTS_FOR_TURNS else -1)

    for i, m in enumerate(out):
        if m.get("role") != "tool":
            continue
        content = m.get("content") or ""
        if i < cutoff:
            m["content"] = STUB
        elif len(content) > MAX_RESULT_CHARS:
            m["content"] = content[:MAX_RESULT_CHARS] + "\n…truncated."

    if len(out) > MAX_MESSAGES:
        out = _drop_oldest_turns(out, MAX_MESSAGES)
    return out


def _drop_oldest_turns(history, limit):
    """Drop whole turns from the front until we're under the limit.

    Cutting mid-turn would leave a `tool` message with no assistant call (or
    an assistant call with no result), which the API rejects.
    """
    starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
    for start in starts:
        if len(history) - start <= limit:
            return history[start:]
    return history[-limit:]


def pending_tool_calls(history):
    """Tool calls from the last assistant turn that have no result yet.

    This is what the confirmation card is built from, and what a resume has
    to fill in before the model can be called again.
    """
    last_assistant = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "assistant" and history[i].get("tool_calls"):
            last_assistant = i
            break
    if last_assistant is None:
        return []
    answered = {
        m.get("tool_call_id") for m in history[last_assistant + 1:]
        if m.get("role") == "tool"
    }
    return [tc for tc in history[last_assistant]["tool_calls"]
            if tc.get("id") not in answered]


def order_tool_results(history):
    """Put trailing tool messages back in their assistant's call order.

    Confirmation runs the approved calls in whatever order the user decided;
    providers are happier when results follow the order they were requested in.
    """
    last_assistant = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "assistant" and history[i].get("tool_calls"):
            last_assistant = i
            break
    if last_assistant is None:
        return history
    head, tail = history[:last_assistant + 1], history[last_assistant + 1:]
    if any(m.get("role") != "tool" for m in tail):
        return history                      # not a pure trailing result block
    wanted = [tc.get("id") for tc in history[last_assistant]["tool_calls"]]
    by_id = {m.get("tool_call_id"): m for m in tail}
    ordered = [by_id[i] for i in wanted if i in by_id]
    ordered += [m for m in tail if m.get("tool_call_id") not in wanted]
    return head + ordered

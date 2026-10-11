"""The turn loop.

One user message can take several round trips: the model asks for tools, we
run them, it reads the results and asks for more, and eventually it answers.
This drives that loop and emits events as it goes, so the browser can show
what is happening rather than a spinner.

The loop stops in three ways:
  * the model finishes           -> "done"
  * it asks to change something  -> "confirm_required", and the turn ends
                                    until the user approves on a new request
  * it runs out of budget        -> "done" with a note

Nothing is written to the database. The conversation only exists in the
events we emit; the browser keeps it and sends it back next time.
"""

import json
import logging
import time

from . import history as hist
from . import confirm, llm, prompts, tools
from .policy import is_write

logger = logging.getLogger(__name__)

MAX_MODEL_CALLS = 25
DEADLINE_SECONDS = {"helper": 300, "chat": 600, "scheduled": 900}
MAX_TOKENS = {"helper": 8000, "chat": 32000, "scheduled": 32000}
EFFORT = {"helper": "low", "chat": "high", "scheduled": "high"}


def run_turn(*, user, mode, history, text=None, decisions=None,
             page_context=None, provider=None):
    """Yield (event_name, payload) for one user message or one confirmation.

    `history` is the conversation as OpenAI-format messages, already verified
    by the caller. It is not mutated.
    """
    messages = [dict(m) for m in history]
    started = time.monotonic()
    deadline = started + DEADLINE_SECONDS.get(mode, 600)
    totals = {"prompt": 0, "completion": 0, "cached": 0}

    def finish(event, payload):
        payload["history"] = hist.trim(messages)
        return event, payload

    try:
        provider = provider or llm.get_provider()
    except llm.LLMError as e:
        yield "error", {"message": str(e), "retryable": False}
        return

    if decisions is not None:
        for event in _apply_decisions(messages, decisions, user, mode):
            yield event
        messages[:] = hist.order_tool_results(messages)
    elif text:
        messages.append({"role": "user", "content": text})
    else:
        yield "error", {"message": "Nothing to send.", "retryable": False}
        return

    tool_specs = tools.tools_for(user, mode)
    system = prompts.PROMPTS[mode]

    for call_number in range(MAX_MODEL_CALLS):
        if time.monotonic() > deadline:
            messages.append({"role": "assistant", "content":
                             "I ran out of time on this one. Ask me to continue "
                             "and I'll pick up where I left off."})
            yield finish("done", {"finish_reason": "timeout", "usage": totals})
            return

        yield "message_start", {}
        assistant_text, reasoning, calls = [], [], []
        finish_reason = "stop"

        try:
            for event in provider.stream_turn(
                system=system,
                messages=messages + [prompts.context_message(user, page_context)],
                tools=tool_specs,
                effort=EFFORT.get(mode, "high"),
                max_tokens=MAX_TOKENS.get(mode, 8000),
            ):
                kind = event["type"]
                if kind == "text":
                    assistant_text.append(event["delta"])
                    yield "text", {"delta": event["delta"]}
                elif kind == "thinking":
                    reasoning.append(event["delta"])
                    if mode != "helper":
                        yield "thinking", {"delta": event["delta"]}
                elif kind == "tool_call":
                    calls.append(event)
                elif kind == "usage":
                    for key in totals:
                        totals[key] += event.get(key, 0)
                    yield "usage", dict(totals)
                elif kind == "done":
                    finish_reason = event["finish_reason"]
        except llm.LLMError as e:
            # Keep whatever was streamed: a partial answer plus an error beats
            # a blank bubble, and the history stays consistent either way.
            if assistant_text:
                messages.append({"role": "assistant",
                                 "content": "".join(assistant_text)})
            yield finish("error", {"message": str(e), "retryable": True})
            return

        assistant = {"role": "assistant", "content": "".join(assistant_text) or None}
        if reasoning:
            assistant["reasoning_content"] = "".join(reasoning)
        if calls:
            assistant["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls
            ]
        messages.append(assistant)

        if not calls:
            yield finish("done", {"finish_reason": finish_reason, "usage": totals})
            return

        # Reads run now; anything that changes data waits for the user.
        needs_confirm = [c for c in calls if _needs_confirm(c["name"], mode)]
        for call in calls:
            if call in needs_confirm:
                continue
            for event in _run_call(call["id"], call["name"], call["arguments"],
                                   user, mode, messages):
                yield event

        if needs_confirm:
            cards = [
                confirm.build_card(c["id"], c["name"], _parse_args(c["arguments"]))
                for c in needs_confirm
            ]
            yield finish("confirm_required", {"cards": cards, "usage": totals})
            return

    messages.append({"role": "assistant", "content":
                     "I've used up the tool calls allowed for one message. "
                     "Say 'continue' if you want me to keep going."})
    yield finish("done", {"finish_reason": "turn_cap", "usage": totals})


def _needs_confirm(name, mode):
    """Writes pause in chat mode. The read-only surfaces never see write tools
    in the first place, so a call there is a mistake and gets refused by
    tools.execute rather than silently confirmed."""
    return mode == "chat" and is_write(name)


def _run_call(call_id, name, arguments, user, mode, messages):
    args = _parse_args(arguments)
    yield "tool_start", {
        "tool_call_id": call_id, "name": name, "args": args,
        "label": tools.label_for(name, args),
    }
    ok, result, ms = tools.execute(name, arguments, user, mode)
    messages.append({"role": "tool", "tool_call_id": call_id,
                     "name": name, "content": result})
    yield "tool_end", {
        "tool_call_id": call_id, "name": name, "ok": ok, "ms": ms,
        "preview": result[:400],
    }


def _apply_decisions(messages, decisions, user, mode):
    """Run the calls the user approved; refuse the rest. Every pending call
    gets a result, or the next model request would be malformed."""
    pending = hist.pending_tool_calls(messages)
    if not pending:
        yield "error", {"message": "There is nothing waiting to be confirmed.",
                        "retryable": False}
        return
    for call in pending:
        call_id = call.get("id")
        name = call.get("function", {}).get("name", "")
        arguments = call.get("function", {}).get("arguments", "{}")
        if decisions.get(call_id):
            for event in _run_call(call_id, name, arguments, user, mode, messages):
                yield event
        else:
            messages.append({
                "role": "tool", "tool_call_id": call_id, "name": name,
                "content": "The user declined this change. Do not attempt it "
                           "again. Acknowledge briefly and continue.",
            })
            yield "tool_end", {"tool_call_id": call_id, "name": name,
                               "ok": False, "ms": 0, "preview": "Declined"}


def _parse_args(arguments):
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}

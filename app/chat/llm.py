"""The model provider — the only file that knows which LLM we talk to.

Everything else in app/chat speaks in normalised events:

    {"type": "thinking",  "delta": str}
    {"type": "text",      "delta": str}
    {"type": "tool_call", "id": str, "name": str, "arguments": str}
    {"type": "usage",     "prompt": int, "completion": int, "cached": int}
    {"type": "done",      "finish_reason": str}

Default is GLM-5.3-Flash on Z.ai, whose API is OpenAI-shaped, so the `openai`
SDK does the HTTP, the SSE parsing and the tool-call delta accumulation for us.
Swapping models means adding a class here and nothing else.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
DEFAULT_MODEL = "glm-5.3-flash"


class LLMError(RuntimeError):
    """Raised for anything the caller should show the user verbatim."""


def _extra(obj, field):
    """Read a field the SDK doesn't model (GLM adds `reasoning_content`)."""
    value = getattr(obj, field, None)
    if value is None and hasattr(obj, "model_extra"):
        value = (obj.model_extra or {}).get(field)
    return value


class GLMProvider:
    """GLM-5.3-Flash (Z.ai) over the OpenAI-compatible chat completions API."""

    name = "glm"

    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or os.environ.get("GLM_API_KEY", "")
        self.base_url = base_url or os.environ.get("GLM_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.environ.get("GLM_MODEL", DEFAULT_MODEL)
        if not self.api_key:
            raise LLMError(
                "GLM_API_KEY is not set on the server, so the assistant is "
                "unavailable. Add it to the service environment and restart."
            )
        try:
            import openai
        except ImportError:                                    # pragma: no cover
            raise LLMError("The `openai` package is not installed on the server.")
        # Long tool-using turns: give the HTTP layer room, retry transient failures.
        self._client = openai.OpenAI(
            api_key=self.api_key, base_url=self.base_url,
            timeout=180.0, max_retries=2,
        )

    def stream_turn(self, *, system, messages, tools, effort="high", max_tokens=8000):
        """One model call. Yields normalised events; never raises mid-stream
        without first yielding what it already produced."""
        import openai

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": True,
            "max_tokens": max_tokens,
            # Z.ai's recommended sampling for this model.
            "temperature": 1,
            "top_p": 0.95,
            "extra_body": {
                # Thinking cannot be disabled on Flash; keep it in the history
                # so the model can follow its own earlier reasoning.
                "thinking": {"type": "enabled", "clear_thinking": False},
                "reasoning_effort": effort,
                "tool_stream": True,
            },
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"      # the only value GLM accepts

        try:
            stream = self._client.chat.completions.create(**payload)
        except openai.APIStatusError as e:
            raise LLMError(f"Model API error {e.status_code}: {_message_of(e)}")
        except openai.APIConnectionError:
            raise LLMError("Could not reach the model API. Check the server's network.")

        pending = {}          # index -> {id, name, arguments}
        finish_reason = None
        usage = None

        try:
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                if delta is None:
                    continue

                reasoning = _extra(delta, "reasoning_content")
                if reasoning:
                    yield {"type": "thinking", "delta": reasoning}
                if delta.content:
                    yield {"type": "text", "delta": delta.content}

                for tc in (delta.tool_calls or []):
                    slot = pending.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        except openai.APIStatusError as e:
            raise LLMError(f"Model API error {e.status_code}: {_message_of(e)}")
        except openai.APIError as e:
            raise LLMError(f"Model stream failed: {e}")

        for _, call in sorted(pending.items()):
            if not call["name"]:
                continue
            yield {
                "type": "tool_call",
                "id": call["id"] or f"call_{_}",
                "name": call["name"],
                "arguments": call["arguments"] or "{}",
            }

        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            yield {
                "type": "usage",
                "prompt": getattr(usage, "prompt_tokens", 0) or 0,
                "completion": getattr(usage, "completion_tokens", 0) or 0,
                "cached": getattr(details, "cached_tokens", 0) or 0 if details else 0,
            }

        # A turn that produced tool calls but no finish_reason still needs to
        # continue the loop, so infer it rather than trusting the last chunk.
        if finish_reason is None:
            finish_reason = "tool_calls" if pending else "stop"
        yield {"type": "done", "finish_reason": finish_reason}


def _message_of(e):
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return err["message"]
    return str(e)


class FakeProvider:
    """Scripted provider for development and tests — no API key, no network.

    Enabled with LEDGER_CHAT_FAKE_LLM=1. It exercises the whole pipeline
    (streaming, tool calls, confirmation, history round-trip) deterministically:

      "call:<tool>:<json args>"  → emits that tool call, then reports the result
      anything else              → a canned reply naming the tools it was offered
    """

    name = "fake"

    def __init__(self, *args, **kwargs):
        self.model = "fake-1"

    def stream_turn(self, *, system, messages, tools, effort="high", max_tokens=8000):
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), {}
        )
        text = last_user.get("content") or ""
        if isinstance(text, list):                       # multi-part content
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        already_called = any(m.get("role") == "tool" for m in messages)

        yield {"type": "thinking", "delta": "Considering the request. "}

        if text.startswith("call:") and not already_called:
            _, _, rest = text.partition("call:")
            name, _, raw_args = rest.partition(":")
            yield {"type": "text", "delta": f"Calling {name.strip()}.\n"}
            yield {
                "type": "tool_call",
                "id": "call_fake_1",
                "name": name.strip(),
                "arguments": (raw_args.strip() or "{}"),
            }
            yield {"type": "usage", "prompt": 100, "completion": 20, "cached": 0}
            yield {"type": "done", "finish_reason": "tool_calls"}
            return

        if already_called:
            result = next(
                (m.get("content", "") for m in reversed(messages)
                 if m.get("role") == "tool"), ""
            )
            for part in ("Here is what the tool returned:\n\n", str(result)[:2000]):
                yield {"type": "text", "delta": part}
        else:
            yield {"type": "text", "delta":
                   f"Fake provider. I was given {len(tools)} tools and "
                   f"{len(messages)} messages at effort '{effort}'."}
        yield {"type": "usage", "prompt": 120, "completion": 30, "cached": 40}
        yield {"type": "done", "finish_reason": "stop"}


def get_provider():
    """The provider for this process. Fake when LEDGER_CHAT_FAKE_LLM=1."""
    if os.environ.get("LEDGER_CHAT_FAKE_LLM") == "1":
        return FakeProvider()
    return GLMProvider()


def provider_available():
    """True when a turn could actually run — used to hide the UI when not."""
    return bool(
        os.environ.get("LEDGER_CHAT_FAKE_LLM") == "1"
        or os.environ.get("GLM_API_KEY")
    )

"""Owner-only settings: API keys and endpoints, without touching the server."""

import time

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..services import settings_service as settings
from ..services.auth_service import god_required

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/")
@login_required
@god_required
def index():
    return render_template(
        "settings.html",
        groups=settings.describe(),
        insecure_secret=settings.using_default_secret(),
    )


@bp.route("/", methods=["POST"])
@login_required
@god_required
def save():
    changed, cleared = [], []
    for spec in settings.SETTINGS:
        if request.form.get("clear_" + spec.key):
            settings.save(spec.key, "", current_user.id)
            cleared.append(spec.label)
            continue
        if spec.key not in request.form:
            continue
        value = request.form.get(spec.key, "").strip()
        # A secret field is always blank on load, so a blank submission means
        # "leave it alone" — clearing one is the explicit Clear button.
        if spec.secret and not value:
            continue
        if value != (settings.get(spec.key) or ""):
            settings.save(spec.key, value, current_user.id)
            changed.append(spec.label)

    if changed or cleared:
        parts = []
        if changed:
            parts.append("Updated " + ", ".join(changed))
        if cleared:
            parts.append("Cleared " + ", ".join(cleared))
        flash("; ".join(parts) + ".", "success")
    else:
        flash("Nothing changed.", "info")
    return redirect(url_for("settings.index"))


@bp.route("/test-assistant", methods=["POST"])
@login_required
@god_required
def test_assistant():
    """Send one tiny message to the model so a bad key fails here, loudly,
    rather than silently in someone's first conversation."""
    from ..chat import llm

    started = time.monotonic()
    try:
        provider = llm.GLMProvider(
            api_key=settings.get("GLM_API_KEY"),
            base_url=settings.get("GLM_BASE_URL"),
            model=settings.get("GLM_MODEL"),
        )
        reply = ""
        for event in provider.stream_turn(
            system="Reply with the single word OK.",
            messages=[{"role": "user", "content": "Say OK."}],
            tools=[], effort="low", max_tokens=32,
        ):
            if event["type"] == "text":
                reply += event["delta"]
        ms = int((time.monotonic() - started) * 1000)
        flash(f"Connected to {provider.model} in {ms} ms. It replied: "
              f"{reply.strip()[:60] or '(nothing)'}", "success")
    except llm.LLMError as e:
        flash(f"Could not reach the model: {e}", "error")
    except Exception as e:                                   # noqa: BLE001
        flash(f"Test failed: {e}", "error")
    return redirect(url_for("settings.index"))

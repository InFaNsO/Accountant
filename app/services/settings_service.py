"""Application settings that live in the database instead of the environment.

Editing a systemd unit over SSH to change an API key is a bad workflow, so the
owner can set them in the app. Two things make that safe enough to do:

* **Secrets are encrypted at rest**, with a key derived from SECRET_KEY — which
  lives in the service environment, not in the database. Copies of the database
  (a dev machine pulling production data, a backup on someone's laptop) carry
  ciphertext that is useless without the server's secret.
* **Plaintext never travels back to the browser.** The settings page shows only
  the last four characters. To change a key you replace it.

Resolution order is database first, environment second: what the owner set in
the UI is what runs, and the environment stays as the bootstrap for a fresh
deployment that has no database rows yet.
"""

import base64
import hashlib
import logging
import os

from flask import current_app, g

from ..database import get_db

logger = logging.getLogger(__name__)


class Setting:
    def __init__(self, key, label, group, help="", secret=False,
                 placeholder="", default=""):
        self.key = key
        self.label = label
        self.group = group
        self.help = help
        self.secret = secret
        self.placeholder = placeholder
        self.default = default


# Everything the owner can configure, in the order it appears on the page.
SETTINGS = [
    Setting("GLM_API_KEY", "API key", "Assistant", secret=True,
            placeholder="paste your Z.ai key",
            help="Z.ai key for GLM-5.3-Flash. Without it the assistant is "
                 "hidden everywhere and nothing else is affected."),
    Setting("GLM_MODEL", "Model", "Assistant", default="glm-5.3-flash",
            placeholder="glm-5.3-flash",
            help="Change only if you are moving to a different GLM model."),
    Setting("GLM_BASE_URL", "API endpoint", "Assistant",
            default="https://api.z.ai/api/paas/v4",
            placeholder="https://api.z.ai/api/paas/v4",
            help="Z.ai's international endpoint. Use open.bigmodel.cn's "
                 "endpoint instead if your account is on the China platform."),
    Setting("OLA_MAPS_API_KEY", "Ola Maps API key", "Maps", secret=True,
            placeholder="paste your Ola Maps key",
            help="Geocodes client addresses for the coverage map. Without it "
                 "addresses are stored but not placed on the map."),
]

BY_KEY = {s.key: s for s in SETTINGS}

# Values setup.sh writes as placeholders — present, but not a real key.
_PLACEHOLDERS = {"changeme", "change-this-mcp-key", "your-api-key", ""}


# ── Encryption ───────────────────────────────────────────────────────────────

def _fernet():
    """Fernet keyed off SECRET_KEY, so the database alone cannot reveal a key."""
    from cryptography.fernet import Fernet
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hashlib.sha256(b"ledger-settings-v1:" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def using_default_secret():
    """True when SECRET_KEY was never set, which makes the encryption above
    decorative. The settings page says so rather than implying safety."""
    return current_app.secret_key in ("dev-secret-change-in-prod", None, "")


# ── Storage ──────────────────────────────────────────────────────────────────

def _rows():
    """All stored settings, cached for the current request."""
    if "settings_rows" not in g:
        try:
            g.settings_rows = {
                r["key"]: r for r in
                get_db().execute("SELECT * FROM app_settings").fetchall()
            }
        except Exception:                                    # noqa: BLE001
            g.settings_rows = {}                             # table not created yet
    return g.settings_rows


def _invalidate():
    g.pop("settings_rows", None)


def _stored(key):
    """The decrypted stored value, or None. Never raises on a bad ciphertext."""
    row = _rows().get(key)
    if not row or row["value"] in (None, ""):
        return None
    if not row["is_secret"]:
        return row["value"]
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(row["value"].encode()).decode()
    except (InvalidToken, Exception):                        # noqa: BLE001
        # Almost always SECRET_KEY changed since this was saved. Treat it as
        # unset so the app keeps working, and let the page ask for it again.
        logger.warning("could not decrypt setting %s — SECRET_KEY may have changed", key)
        return None


def _from_env(key):
    value = os.environ.get(key, "")
    return None if value.strip() in _PLACEHOLDERS else value


def get(key, default=None):
    """The effective value: database, then environment, then the declared default."""
    value = _stored(key)
    if value:
        return value
    value = _from_env(key)
    if value:
        return value
    spec = BY_KEY.get(key)
    return default if default is not None else (spec.default if spec else "")


def source(key):
    """Where the effective value comes from: settings | environment | unset.

    'unreadable' means a row exists but could not be decrypted.
    """
    row = _rows().get(key)
    if row and row["value"]:
        return "settings" if _stored(key) else "unreadable"
    return "environment" if _from_env(key) else "unset"


def save(key, value, user_id):
    """Store a value. An empty value clears the row and falls back to the
    environment, which is how you undo a mistake without SSH."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise KeyError(key)
    db = get_db()
    value = (value or "").strip()
    if not value:
        db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    else:
        stored = _fernet().encrypt(value.encode()).decode() if spec.secret else value
        db.execute(
            """INSERT INTO app_settings (key, value, is_secret, updated_at, updated_by)
                    VALUES (?,?,?,CURRENT_TIMESTAMP,?)
               ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, is_secret=excluded.is_secret,
                    updated_at=CURRENT_TIMESTAMP, updated_by=excluded.updated_by""",
            (key, stored, 1 if spec.secret else 0, user_id),
        )
    db.commit()
    _invalidate()


def masked(key):
    """What the page shows for a secret: enough to recognise, not to use."""
    value = get(key)
    if not value:
        return ""
    if not BY_KEY.get(key, Setting(key, "", "")).secret:
        return value
    return "•" * 8 + value[-4:] if len(value) > 4 else "•" * 8


def describe():
    """Everything the settings page needs, grouped for rendering."""
    groups = {}
    for spec in SETTINGS:
        row = _rows().get(spec.key)
        groups.setdefault(spec.group, []).append({
            "spec": spec,
            "source": source(spec.key),
            "masked": masked(spec.key),
            "updated_at": row["updated_at"] if row else None,
        })
    return groups

"""Turning a pending tool call into something a human can approve.

The model asks to call `record_payment(client_id=7, amount=50000, …)`. Nobody
should have to read that to decide. These builders resolve ids to names and
render the call as a few plain lines — the amount, who it's for, the date —
so the person clicking Confirm knows what they are agreeing to.
"""

from flask import current_app

from ..database import get_db
from .policy import TOOL_POLICY

# Argument name -> (table, column to show). Anything not listed prints as-is.
_LOOKUPS = {
    "client_id":      ("clients", "name"),
    "company_id":     ("client_companies", "name"),
    "product_id":     ("products", "name"),
    "sub_product_id": ("sub_products", "name"),
    "supplier_id":    ("suppliers", "name"),
    "invoice_id":     ("invoices", "invoice_number"),
    "payment_id":     ("payments", "id"),
    "cat_id":         ("categories", "name"),
    "category_id":    ("categories", "name"),
    "dispatch_id":    ("dispatches", "id"),
    "po_id":          ("purchase_orders", "id"),
    "tally_id":       ("stock_tallies", "name"),
    "pp_id":          ("palm_purchases", "id"),
}

_MONEY_KEYS = {"amount", "total", "unit_price", "price", "rate", "cost",
               "opening_balance", "balance_lock_limit", "paid_amount"}

_VERBS = {
    "create": "Create", "update": "Update", "delete": "Delete",
    "record": "Record", "set": "Set", "add": "Add", "adjust": "Adjust",
    "close": "Close", "apply": "Apply", "receive": "Receive",
    "reconcile": "Reconcile", "save": "Save",
}


def build_card(tool_call_id, name, args):
    """{tool_call_id, tool, title, lines, danger} for one pending call."""
    action = TOOL_POLICY.get(name, ("", ""))[1]
    danger = action == "delete"
    lines = []
    for key, value in (args or {}).items():
        if value in (None, "", [], {}):
            continue
        lines.append(f"{_label(key)}: {_render(key, value)}")
    return {
        "tool_call_id": tool_call_id,
        "tool": name,
        "title": _title(name),
        "lines": lines[:12],
        "danger": danger,
    }


def _title(name):
    head, _, rest = name.partition("_")
    verb = _VERBS.get(head)
    if verb:
        return f"{verb} {rest.replace('_', ' ')}".strip()
    return name.replace("_", " ").capitalize()


def _label(key):
    return key.replace("_", " ").capitalize()


def _render(key, value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, dict)):
        return _summarise(value)
    if key in _MONEY_KEYS and isinstance(value, (int, float)):
        from .. import _format_inr
        return _format_inr(value)
    if key.endswith("_id"):
        resolved = _lookup(key, value)
        if resolved:
            return f"{resolved} (#{value})"
    return str(value)


def _summarise(value):
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    keys = ", ".join(list(value)[:4])
    return f"{{{keys}}}"


def _lookup(key, value):
    spec = _LOOKUPS.get(key)
    if not spec:
        return None
    table, column = spec
    if column == "id":
        return None
    try:
        row = get_db().execute(
            f"SELECT {column} AS label FROM {table} WHERE id = ?", (value,)
        ).fetchone()
    except Exception:                                        # noqa: BLE001
        current_app.logger.debug("confirm lookup failed for %s", key)
        return None
    return row["label"] if row else None

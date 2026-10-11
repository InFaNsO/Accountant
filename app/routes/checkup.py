"""Checkup: name an invoice, see everything sold from that serial onwards,
client by client, with each client's invoices and a ledger that opens with
the balance they carried into the checkup.

The page is rendered once and then talks to /checkup/data; the assistant
reaches the same computation through /api/checkup (see routes/api.py).
"""
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from ..services import checkup_service
from ..services.auth_service import (permission_required, get_scoped_client_ids,
                                     get_own_scoped_client_ids)

bp = Blueprint("checkup", __name__, url_prefix="/checkup")


def _scope():
    """Client ids the current user may see, or None for unrestricted
    (same rule as the clients and payments pages)."""
    scope = get_scoped_client_ids(current_user)
    return scope if scope is not None else get_own_scoped_client_ids(current_user)


def _csv_ints(raw):
    if not raw:
        return None
    return [int(x) for x in str(raw).split(",") if x.strip().isdigit()]


@bp.route("/")
@login_required
@permission_required("clients", "financials")
def index():
    scope = _scope()
    index_rows = checkup_service.invoice_index(scope)
    latest = checkup_service.parse_serial(index_rows[0]["invoice_number"]) if index_rows else None
    # Open on the last ~30 serials so the page is never an empty form.
    default_from = max(1, latest - 29) if latest else None
    return render_template(
        "checkup/index.html",
        invoice_index=index_rows,
        init={"latest": latest, "default_from": default_from},
    )


@bp.route("/data")
@login_required
@permission_required("clients", "financials")
def data():
    scope = _scope()
    client_ids = _csv_ints(request.args.get("client_ids"))
    if scope is not None:
        client_ids = [c for c in client_ids if c in scope] if client_ids else list(scope)
    payload, status = checkup_service.checkup_from_args(request.args, client_ids)
    return jsonify(payload), status

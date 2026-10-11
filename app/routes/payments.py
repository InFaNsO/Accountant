from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from ..services import payment_service, invoice_service, client_service
from ..services.auth_service import permission_required, get_scoped_client_ids, get_own_scoped_client_ids

bp = Blueprint("payments", __name__, url_prefix="/payments")


def _scope():
    """Client ids the current user may see, or None for unrestricted.
    Sales managers get their team's clients; anyone else with clients
    assigned directly to them (clients.sales_rep_id) gets just those."""
    scope = get_scoped_client_ids(current_user)
    return scope if scope is not None else get_own_scoped_client_ids(current_user)


@bp.route("/")
@login_required
@permission_required("payments", "view")
def list_payments():
    payments = payment_service.get_all_payments()
    scope = _scope()
    if scope is not None:
        payments = [p for p in payments if p["client_id"] in scope]
    # Per-client company lists, so each row's Company cell can offer the same
    # assign / re-assign dropdown as the client page's payments section.
    companies_by_client = {}
    for co in client_service.get_all_companies_with_client():
        companies_by_client.setdefault(co["client_id"], []).append({"id": co["id"], "name": co["name"]})
    can_assign = current_user.has_permission("clients", "financials")
    return render_template("payments/list.html", payments=payments,
                           companies_by_client=companies_by_client, can_assign=can_assign)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("payments", "create")
def new_payment():
    scope = _scope()
    clients       = client_service.get_all_clients_with_companies()
    invoices      = invoice_service.get_all_invoices()
    all_companies = client_service.get_all_companies_with_client()
    if scope is not None:
        clients       = [c for c in clients if c["id"] in scope]
        invoices      = [i for i in invoices if i["client_id"] in scope]
        all_companies = [co for co in all_companies if co["client_id"] in scope]

    # Pre-fill from query string (e.g. coming from invoice detail)
    prefill = {}
    invoice_id = request.args.get("invoice_id")
    client_id  = request.args.get("client_id")
    if invoice_id:
        inv = invoice_service.get_invoice(invoice_id)
        if inv:
            prefill = {
                "invoice_id": invoice_id,
                "client_id":  inv["client_id"],
                "amount":     round(inv["total"] - inv["amount_paid"], 2),
            }
    elif client_id:
        prefill = {"client_id": client_id}

    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("client_id") or not data.get("amount"):
            flash("Client and amount are required.", "error")
            return render_template("payments/form.html",
                                   clients=clients, invoices=invoices, all_companies=all_companies, prefill=data)
        if scope is not None and int(data["client_id"]) not in scope:
            abort(403)
        try:
            payment_service.create_payment(data)
            flash("Payment recorded and allocated.", "success")
        except Exception as e:
            flash(f"Error recording payment: {e}", "error")
            return render_template("payments/form.html",
                                   clients=clients, invoices=invoices, all_companies=all_companies, prefill=data)

        inv_id = data.get("invoice_id")
        if inv_id:
            return redirect(url_for("invoices.detail", invoice_id=inv_id))
        return redirect(url_for("clients.detail", client_id=data["client_id"]))

    return render_template("payments/form.html",
                           clients=clients, invoices=invoices, all_companies=all_companies, prefill=prefill)


@bp.route("/<int:payment_id>")
@login_required
@permission_required("payments", "view")
def detail(payment_id):
    p = payment_service.get_payment(payment_id)
    if not p:
        flash("Payment not found.", "error")
        return redirect(url_for("payments.list_payments"))
    scope = _scope()
    if scope is not None and p["client_id"] not in scope:
        abort(403)
    allocations = payment_service.get_payment_allocations(payment_id)
    companies   = client_service.get_companies(p["client_id"])
    company_name = next((c["name"] for c in companies if c["id"] == p["company_id"]), None)
    can_edit   = current_user.has_permission("payments", "edit")
    can_delete = current_user.has_permission("payments", "delete")
    return render_template("payments/detail.html", p=p, allocations=allocations,
                           company_name=company_name, can_edit=can_edit, can_delete=can_delete)


@bp.route("/<int:payment_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("payments", "edit")
def edit_payment(payment_id):
    p = payment_service.get_payment(payment_id)
    if not p:
        flash("Payment not found.", "error")
        return redirect(url_for("payments.list_payments"))
    scope = _scope()
    if scope is not None and p["client_id"] not in scope:
        abort(403)
    companies = client_service.get_companies(p["client_id"])

    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("amount") or not data.get("payment_date"):
            flash("Amount and payment date are required.", "error")
            return render_template("payments/edit.html", p={**dict(p), **data}, companies=companies)
        try:
            payment_service.update_payment(payment_id, data)
            flash("Payment updated.", "success")
        except Exception as e:
            flash(f"Error updating payment: {e}", "error")
            return render_template("payments/edit.html", p={**dict(p), **data}, companies=companies)
        return redirect(url_for("payments.detail", payment_id=payment_id))

    return render_template("payments/edit.html", p=dict(p), companies=companies)


@bp.route("/<int:payment_id>/toggle-opening-balance", methods=["POST"])
@login_required
@permission_required("payments", "edit")
def toggle_opening_balance(payment_id):
    p = payment_service.get_payment(payment_id)
    if not p:
        flash("Payment not found.", "error")
        return redirect(url_for("payments.list_payments"))
    scope = _scope()
    if scope is not None and p["client_id"] not in scope:
        abort(403)
    new_flag = 0 if (p["is_opening_balance"] or 0) else 1
    payment_service.set_payment_opening_balance(payment_id, new_flag)
    flash(
        "Payment marked as opening-balance (excluded from invoice allocation)."
        if new_flag else "Payment unmarked — it will be allocated to invoices again.",
        "success",
    )
    return redirect(request.referrer or url_for("payments.list_payments"))


@bp.route("/<int:payment_id>/delete", methods=["POST"])
@login_required
@permission_required("payments", "delete")
def delete_payment(payment_id):
    scope = _scope()
    if scope is not None:
        p = payment_service.get_payment(payment_id)
        if p and p["client_id"] not in scope:
            abort(403)
    allocs = payment_service.get_payment_allocations(payment_id)
    first_invoice = allocs[0]["invoice_id"] if allocs else None
    payment_service.delete_payment(payment_id)
    flash("Payment deleted.", "success")
    if first_invoice:
        return redirect(url_for("invoices.detail", invoice_id=first_invoice))
    return redirect(url_for("payments.list_payments"))


# ── Client companies list for JS (used by payment form) ──────────────────────
@bp.route("/api/client-companies/<int:client_id>")
@login_required
@permission_required("payments", "view")
def api_client_companies(client_id):
    scope = _scope()
    if scope is not None and client_id not in scope:
        abort(403)
    companies = client_service.get_companies(client_id)
    return jsonify([{"id": c["id"], "name": c["name"]} for c in companies])


# ── Client invoice list for JS (used by payment form) ────────────────────────
@bp.route("/api/client-invoices/<int:client_id>")
@login_required
@permission_required("payments", "view")
def api_client_invoices(client_id):
    scope = _scope()
    if scope is not None and client_id not in scope:
        abort(403)
    invoices = invoice_service.get_all_invoices()
    client_invs = [
        {
            "id":             i["id"],
            "invoice_number": i["invoice_number"],
            "total":          i["total"],
            "amount_paid":    i["amount_paid"],
            "remaining":      round(i["total"] - i["amount_paid"], 2),
            "status":         i["status"],
        }
        for i in invoices
        if i["client_id"] == client_id and i["status"] not in ("paid", "cancelled")
    ]
    return jsonify(client_invs)


# ── Batch import ──────────────────────────────────────────────────────────────
@bp.route("/import", methods=["GET", "POST"])
@login_required
@permission_required("payments", "create")
def import_payments():
    import io, xlrd
    from datetime import datetime

    scope = _scope()
    clients = client_service.get_all_clients()
    if scope is not None:
        clients = [c for c in clients if c["id"] in scope]

    if request.method == "POST" and "statement" in request.files:
        f = request.files["statement"]
        raw = f.read()
        try:
            wb = xlrd.open_workbook(file_contents=raw)
            ws = wb.sheet_by_index(0)
        except Exception as e:
            flash(f"Could not read file: {e}", "error")
            return render_template("payments/import.html", clients=clients, rows=[])

        rows = []
        header_row = None
        for r in range(ws.nrows):
            row_vals = [str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)]
            if row_vals[0] == "Date" and "Narration" in row_vals[1]:
                header_row = r
                break

        if header_row is None:
            flash("Could not find transaction header row in statement.", "error")
            return render_template("payments/import.html", clients=clients, rows=[])

        for r in range(header_row + 1, ws.nrows):
            date_val  = str(ws.cell_value(r, 0)).strip()
            narration = str(ws.cell_value(r, 1)).strip()
            ref       = str(ws.cell_value(r, 2)).strip()
            deposit   = ws.cell_value(r, 5)

            # Skip separators / summary rows
            if not date_val or date_val.startswith("*") or not narration or narration.startswith("*"):
                continue
            # Only credits (deposits = money received)
            try:
                deposit = float(deposit)
            except (ValueError, TypeError):
                continue
            if deposit <= 0:
                continue

            # Normalise date DD/MM/YY → YYYY-MM-DD
            try:
                dt = datetime.strptime(date_val, "%d/%m/%y")
                iso_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                iso_date = date_val

            rows.append({
                "date":      iso_date,
                "narration": narration[:80],
                "reference": ref,
                "amount":    deposit,
            })

        return render_template("payments/import.html",
                               clients=clients, rows=rows, show_table=True)

    if request.method == "POST" and request.form.get("action") == "save":
        saved = 0
        for key, val in request.form.items():
            if not key.startswith("client_"):
                continue
            idx = key.split("_", 1)[1]
            client_id = val.strip()
            if not client_id:
                continue
            if scope is not None and int(client_id) not in scope:
                continue
            amount    = request.form.get(f"amount_{idx}", "0")
            date      = request.form.get(f"date_{idx}", "")
            narration = request.form.get(f"narration_{idx}", "")
            ref       = request.form.get(f"ref_{idx}", "")
            try:
                payment_service.create_payment({
                    "client_id":    client_id,
                    "amount":       amount,
                    "payment_date": date,
                    "method":       "bank_transfer",
                    "reference":    ref,
                    "notes":        narration,
                })
                saved += 1
            except Exception:
                pass
        flash(f"{saved} payment(s) imported and allocated.", "success")
        return redirect(url_for("payments.list_payments"))

    return render_template("payments/import.html", clients=clients, rows=[])

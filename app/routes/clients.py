from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from ..services import client_service
from ..services.auth_service import permission_required
from ..database import get_db

bp = Blueprint("clients", __name__, url_prefix="/clients")


@bp.route("/")
@login_required
@permission_required("clients", "view")
def list_clients():
    clients = client_service.get_all_clients()
    can_financials = current_user.has_permission("clients", "financials")
    return render_template("clients/list.html", clients=clients, can_financials=can_financials)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("clients", "create")
def new_client():
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Client name is required.", "error")
            return render_template("clients/form.html", client=data, action="new")
        client_id = client_service.create_client(data)
        flash("Client created successfully.", "success")
        return redirect(url_for("clients.detail", client_id=client_id))
    return render_template("clients/form.html", client={}, action="new")


@bp.route("/<int:client_id>")
@login_required
@permission_required("clients", "view")
def detail(client_id):
    client = client_service.get_client(client_id)
    if not client:
        flash("Client not found.", "error")
        return redirect(url_for("clients.list_clients"))
    invoices = client_service.get_client_invoices(client_id)
    can_financials = current_user.has_permission("clients", "financials")
    balance = client_service.get_client_balance(client_id) if can_financials else None
    return render_template("clients/detail.html", client=client, invoices=invoices,
                           balance=balance, can_financials=can_financials)


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("clients", "edit")
def edit_client(client_id):
    client = client_service.get_client(client_id)
    if not client:
        flash("Client not found.", "error")
        return redirect(url_for("clients.list_clients"))
    if request.method == "POST":
        data = request.form.to_dict()
        if not data.get("name"):
            flash("Client name is required.", "error")
            return render_template("clients/form.html", client=data, action="edit", client_id=client_id)
        client_service.update_client(client_id, data)
        flash("Client updated successfully.", "success")
        return redirect(url_for("clients.detail", client_id=client_id))
    return render_template("clients/form.html", client=dict(client), action="edit", client_id=client_id)


@bp.route("/<int:client_id>/ledger")
@login_required
@permission_required("clients", "financials")
def ledger(client_id):
    client = client_service.get_client(client_id)
    if not client:
        return jsonify({"error": "Not found"}), 404

    date_from = request.args.get("from")   # YYYY-MM-DD or None
    date_to   = request.args.get("to")     # YYYY-MM-DD or None

    db = get_db()
    invoices = db.execute(
        "SELECT id, invoice_number, issue_date, total, amount_paid, status FROM invoices "
        "WHERE client_id=? AND status != 'cancelled' ORDER BY issue_date, id",
        (client_id,),
    ).fetchall()
    payments = db.execute(
        "SELECT id, amount, payment_date, method, reference, notes, invoice_id FROM payments "
        "WHERE client_id=? ORDER BY payment_date, id",
        (client_id,),
    ).fetchall()
    manual = db.execute(
        "SELECT * FROM ledger_entries WHERE client_id=? ORDER BY entry_date, id",
        (client_id,),
    ).fetchall()

    opening = float(client["opening_balance"] or 0)

    inv_list    = [{"sort": r["issue_date"]    or "", "kind": "invoice", "row": dict(r)} for r in invoices]
    pay_list    = [{"sort": r["payment_date"]  or "", "kind": "payment", "row": dict(r)} for r in payments]
    manual_list = [{"sort": r["entry_date"]    or "", "kind": "manual",  "row": dict(r)} for r in manual]
    combined    = sorted(inv_list + pay_list + manual_list, key=lambda x: x["sort"])

    def _build(items, start=None):
        """Build entry list and return (entries, final_running).
        If start is None, include the opening balance as the first entry.
        If start is a float, carry it forward (period view — opening already included in B/F).
        """
        running = 0.0 if start is None else start
        entries = []

        if start is None and opening != 0:
            if opening > 0:
                running -= opening
                entries.append({"date": "", "type": "opening",
                                "label": "Opening Balance (debt)",
                                "debit": opening, "credit": 0, "running": running})
            else:
                running += abs(opening)
                entries.append({"date": "", "type": "opening",
                                "label": "Opening Balance (credit)",
                                "debit": 0, "credit": abs(opening), "running": running})

        for item in items:
            r = item["row"]
            if item["kind"] == "invoice":
                running -= float(r["total"])
                entries.append({"date": r["issue_date"] or "", "type": "invoice",
                                "label": r["invoice_number"],
                                "debit": r["total"], "credit": 0, "running": running})
            elif item["kind"] == "payment":
                running += float(r["amount"])
                parts = [r["method"]] if r["method"] else []
                if r["reference"]: parts.append(r["reference"])
                if r["notes"]:     parts.append(r["notes"])
                entries.append({"date": r["payment_date"] or "", "type": "payment",
                                "label": " — ".join(parts) if parts else "Payment",
                                "invoice_id": r["invoice_id"],
                                "debit": 0, "credit": r["amount"], "running": running})
            else:  # manual
                debit  = float(r["debit"]  or 0)
                credit = float(r["credit"] or 0)
                running += credit - debit
                entries.append({"date": r["entry_date"] or "", "type": "manual",
                                "label": r["description"] or "Manual Entry",
                                "debit": debit, "credit": credit, "running": running})
        return entries, running

    if date_from and date_to:
        # Compute Balance Brought Forward = running total of everything BEFORE date_from
        before  = [i for i in combined if (i["sort"] or "") < date_from]
        _, bbf  = _build(before)  # includes opening balance

        # Entries within the window
        in_win  = [i for i in combined if date_from <= (i["sort"] or "") <= date_to]
        win_entries, final_balance = _build(in_win, start=bbf)

        # Prepend the BBF pseudo-entry
        entries = [{
            "date":    date_from,
            "type":    "bbf",
            "label":   "Balance Brought Forward",
            "debit":   abs(bbf) if bbf < 0 else 0,
            "credit":  bbf      if bbf > 0 else 0,
            "running": bbf,
        }] + win_entries

        return jsonify({
            "client_name":   client["name"],
            "entries":       entries,
            "final_balance": final_balance,
            "date_from":     date_from,
            "date_to":       date_to,
        })

    # Full ledger (no date filter)
    entries, final_balance = _build(combined)
    return jsonify({
        "client_name":   client["name"],
        "entries":       entries,
        "final_balance": final_balance,
    })


@bp.route("/<int:client_id>/ledger/entry", methods=["POST"])
@login_required
@permission_required("clients", "financials")
def add_ledger_entry(client_id):
    data        = request.get_json(silent=True) or {}
    entry_date  = data.get("entry_date")
    description = data.get("description", "")
    debit       = float(data.get("debit")  or 0)
    credit      = float(data.get("credit") or 0)
    if not entry_date:
        return jsonify({"error": "Date is required"}), 400
    if debit == 0 and credit == 0:
        return jsonify({"error": "Enter a debit or credit amount"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO ledger_entries (client_id, entry_date, description, debit, credit) VALUES (?,?,?,?,?)",
        (client_id, entry_date, description, debit, credit),
    )
    db.commit()
    return jsonify({"ok": True})


@bp.route("/<int:client_id>/delete", methods=["POST"])
@login_required
@permission_required("clients", "delete")
def delete_client(client_id):
    client_service.delete_client(client_id)
    flash("Client deleted.", "success")
    return redirect(url_for("clients.list_clients"))

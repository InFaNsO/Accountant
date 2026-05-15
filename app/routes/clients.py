from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from ..services import client_service
from ..database import get_db

bp = Blueprint("clients", __name__, url_prefix="/clients")


@bp.route("/")
def list_clients():
    clients = client_service.get_all_clients()
    return render_template("clients/list.html", clients=clients)


@bp.route("/new", methods=["GET", "POST"])
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
def detail(client_id):
    client = client_service.get_client(client_id)
    if not client:
        flash("Client not found.", "error")
        return redirect(url_for("clients.list_clients"))
    invoices = client_service.get_client_invoices(client_id)
    balance = client_service.get_client_balance(client_id)
    return render_template("clients/detail.html", client=client, invoices=invoices, balance=balance)


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
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
def ledger(client_id):
    client = client_service.get_client(client_id)
    if not client:
        return jsonify({"error": "Not found"}), 404
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

    opening = float(client["opening_balance"] or 0)
    entries = []
    running = 0.0

    if opening != 0:
        if opening > 0:
            running -= opening
            entries.append({"date": "", "type": "opening", "label": "Opening Balance (debt)",
                            "debit": opening, "credit": 0, "running": running})
        else:
            running += abs(opening)
            entries.append({"date": "", "type": "opening", "label": "Opening Balance (credit)",
                            "debit": 0, "credit": abs(opening), "running": running})

    # merge invoices and payments chronologically
    inv_list  = [{"sort": (r["issue_date"] or ""), "kind": "invoice",  "row": dict(r)} for r in invoices]
    pay_list  = [{"sort": (r["payment_date"] or ""), "kind": "payment", "row": dict(r)} for r in payments]
    combined  = sorted(inv_list + pay_list, key=lambda x: x["sort"])

    for item in combined:
        if item["kind"] == "invoice":
            r = item["row"]
            running -= r["total"]
            entries.append({"date": r["issue_date"] or "", "type": "invoice",
                            "label": r["invoice_number"],
                            "debit": r["total"], "credit": 0, "running": running})
        else:
            r = item["row"]
            running += r["amount"]
            ref_parts = [r["method"]] if r["method"] else []
            if r["reference"]: ref_parts.append(r["reference"])
            if r["notes"]:     ref_parts.append(r["notes"])
            entries.append({"date": r["payment_date"] or "", "type": "payment",
                            "label": " — ".join(ref_parts) if ref_parts else "Payment",
                            "invoice_id": r["invoice_id"],
                            "debit": 0, "credit": r["amount"], "running": running})

    return jsonify({
        "client_name": client["name"],
        "entries": entries,
        "final_balance": running,
    })


@bp.route("/<int:client_id>/delete", methods=["POST"])
def delete_client(client_id):
    client_service.delete_client(client_id)
    flash("Client deleted.", "success")
    return redirect(url_for("clients.list_clients"))

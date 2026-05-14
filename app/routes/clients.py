from flask import Blueprint, render_template, request, redirect, url_for, flash
from ..services import client_service

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


@bp.route("/<int:client_id>/delete", methods=["POST"])
def delete_client(client_id):
    client_service.delete_client(client_id)
    flash("Client deleted.", "success")
    return redirect(url_for("clients.list_clients"))

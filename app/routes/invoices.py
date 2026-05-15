import re
from flask import Blueprint, render_template, request, redirect, url_for, flash
from ..services import invoice_service, client_service, product_service

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


def _build_product_choices():
    choices = []
    for p in product_service.get_all_products(active_only=True):
        subs = product_service.get_sub_products(p["id"])
        if subs:
            for s in subs:
                if not s["is_active"]:
                    continue
                choices.append({
                    "id":             f"sub_{s['id']}",
                    "product_id":     p["id"],
                    "sub_product_id": s["id"],
                    "name":           f"{p['name']} — {s['name']}",
                    # SKU: sub's own SKU first, fall back to parent SKU
                    "sku":            s["sku"] or p["sku"] or "",
                    "unit_price":     p["unit_price"] if s["use_parent_price"] else (s["unit_price"] or 0),
                    "tax_rate":       p["tax_rate"] or 0,
                })
        else:
            choices.append({
                "id":             str(p["id"]),
                "product_id":     p["id"],
                "sub_product_id": None,
                "name":           p["name"],
                "sku":            p["sku"] or "",
                "unit_price":     p["unit_price"] or 0,
                "tax_rate":       p["tax_rate"] or 0,
            })
    return choices


def _parse_items(form):
    # Collect all indices present in the form — handles non-sequential indices
    # caused by the user deleting rows before adding new ones.
    indices = sorted({
        int(m.group(1))
        for key in form.keys()
        for m in [re.match(r"items\[(\d+)\]\[description\]", key)]
        if m
    })
    items = []
    for idx in indices:
        desc = form.get(f"items[{idx}][description]", "").strip()
        if desc:
            items.append({
                "product_id":     form.get(f"items[{idx}][product_id]") or None,
                "sub_product_id": form.get(f"items[{idx}][sub_product_id]") or None,
                "sku":            form.get(f"items[{idx}][sku]", "").strip() or None,
                "description":    desc,
                "quantity":       form.get(f"items[{idx}][quantity]", 1),
                "unit_price":     form.get(f"items[{idx}][unit_price]", 0),
                "tax_rate":       form.get(f"items[{idx}][tax_rate]", 0),
            })
    return items


@bp.route("/")
def list_invoices():
    invoices = invoice_service.get_all_invoices()
    return render_template("invoices/list.html", invoices=invoices)


@bp.route("/new", methods=["GET", "POST"])
def new_invoice():
    clients  = client_service.get_all_clients()
    products = _build_product_choices()
    if request.method == "POST":
        data = request.form.to_dict()
        items = _parse_items(request.form)
        if not data.get("client_id"):
            flash("Please select a client.", "error")
            return render_template("invoices/form.html", invoice=data, items=[], clients=clients, products=products, action="new")
        if not items:
            flash("Add at least one line item.", "error")
            return render_template("invoices/form.html", invoice=data, items=[], clients=clients, products=products, action="new")
        invoice_id = invoice_service.create_invoice(data, items)
        flash("Invoice created successfully.", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    return render_template("invoices/form.html", invoice={}, items=[], clients=clients, products=products, action="new")


@bp.route("/<int:invoice_id>")
def detail(invoice_id):
    invoice = invoice_service.get_invoice(invoice_id)
    if not invoice:
        flash("Invoice not found.", "error")
        return redirect(url_for("invoices.list_invoices"))
    items = invoice_service.get_invoice_items(invoice_id)
    payments = invoice_service.get_invoice_payments(invoice_id)
    return render_template("invoices/detail.html", invoice=invoice, items=items, payments=payments)


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
def edit_invoice(invoice_id):
    invoice = invoice_service.get_invoice(invoice_id)
    if not invoice:
        flash("Invoice not found.", "error")
        return redirect(url_for("invoices.list_invoices"))
    clients  = client_service.get_all_clients()
    products = _build_product_choices()
    if request.method == "POST":
        data = request.form.to_dict()
        items = _parse_items(request.form)
        if not data.get("client_id"):
            flash("Please select a client.", "error")
            existing_items = invoice_service.get_invoice_items(invoice_id)
            return render_template("invoices/form.html", invoice=data, items=existing_items, clients=clients, products=products, action="edit", invoice_id=invoice_id)
        if not items:
            flash("Add at least one line item.", "error")
            existing_items = invoice_service.get_invoice_items(invoice_id)
            return render_template("invoices/form.html", invoice=data, items=existing_items, clients=clients, products=products, action="edit", invoice_id=invoice_id)
        invoice_service.update_invoice(invoice_id, data, items)
        flash("Invoice updated.", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    items = invoice_service.get_invoice_items(invoice_id)
    return render_template("invoices/form.html", invoice=dict(invoice), items=[dict(i) for i in items], clients=clients, products=products, action="edit", invoice_id=invoice_id)


@bp.route("/<int:invoice_id>/status", methods=["POST"])
def update_status(invoice_id):
    status = request.form.get("status")
    if status in ("draft", "sent", "paid", "cancelled"):
        invoice_service.update_invoice_status(invoice_id, status)
        flash(f"Status updated to {status}.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/delete", methods=["POST"])
def delete_invoice(invoice_id):
    invoice_service.delete_invoice(invoice_id)
    flash("Invoice deleted.", "success")
    return redirect(url_for("invoices.list_invoices"))

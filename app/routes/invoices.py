import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from ..services import invoice_service, client_service, product_service
from ..services.auth_service import permission_required, get_scoped_client_ids

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


def _scope():
    """Client ids the current user may see, or None for unrestricted."""
    return get_scoped_client_ids(current_user)


def _guard_invoice(invoice):
    """403 when a row-scoped (sales) user reaches for an out-of-scope invoice."""
    scope = _scope()
    if scope is not None and (not invoice or invoice["client_id"] not in scope):
        abort(403)


def _build_product_choices():
    choices = []
    for p in product_service.get_all_products(active_only=True):
        subs = product_service.get_sub_products(p["id"])
        # pcs_per_carton may not exist on older sub_products rows; fall back to parent's
        ppc_parent = 0
        try:
            ppc_parent = int(p["pcs_per_carton"] or 0)
        except (KeyError, IndexError, TypeError):
            ppc_parent = 0
        if subs:
            for s in subs:
                if not s["is_active"]:
                    continue
                try:
                    ppc_sub = int(s["pcs_per_carton"] or 0)
                except (KeyError, IndexError, TypeError):
                    ppc_sub = 0
                choices.append({
                    "id":             f"sub_{s['id']}",
                    "product_id":     p["id"],
                    "sub_product_id": s["id"],
                    "name":           f"{p['name']} — {s['name']}",
                    "sku":            s["sku"] or p["sku"] or "",
                    "unit_price":     p["unit_price"] if s["use_parent_price"] else (s["unit_price"] or 0),
                    "tax_rate":       p["tax_rate"] or 0,
                    "stock_qty":      s["stock_qty"] or 0,
                    "pcs_per_carton": ppc_sub or ppc_parent,
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
                "stock_qty":      p["stock_qty"] or 0,
                "pcs_per_carton": ppc_parent,
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
                "discount_type":  (form.get(f"items[{idx}][discount_type]") or "percent").lower(),
                "discount_value": form.get(f"items[{idx}][discount_value]", 0),
            })
    return items


@bp.route("/api/client-companies/<int:client_id>")
@login_required
def api_client_companies(client_id):
    scope = _scope()
    if scope is not None and client_id not in scope:
        abort(403)
    companies = client_service.get_companies(client_id)
    return jsonify([{"id": c["id"], "name": c["name"]} for c in companies])


@bp.route("/api/stock-refresh")
@login_required
def api_stock_refresh():
    """Return a map of current stock_qty for all products + sub-products.
    Response: { "products": [{id, stock_qty}, ...], "sub_products": [{id, stock_qty}, ...] }
    """
    from ..database import get_db
    db = get_db()
    p_rows = db.execute("SELECT id, stock_qty FROM products WHERE is_active=1").fetchall()
    s_rows = db.execute("SELECT id, stock_qty FROM sub_products WHERE is_active=1").fetchall()
    return jsonify({
        "products":     [{"id": r["id"], "stock_qty": float(r["stock_qty"] or 0)} for r in p_rows],
        "sub_products": [{"id": r["id"], "stock_qty": float(r["stock_qty"] or 0)} for r in s_rows],
    })


@bp.route("/")
@login_required
@permission_required("invoices", "view")
def list_invoices():
    invoices = invoice_service.get_all_invoices()
    scope = _scope()
    if scope is not None:
        invoices = [i for i in invoices if i["client_id"] in scope]
    return render_template("invoices/list.html", invoices=invoices)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("invoices", "create")
def new_invoice():
    clients        = client_service.get_all_clients()
    scope = _scope()
    if scope is not None:
        clients = [c for c in clients if c["id"] in scope]
    products       = _build_product_choices()
    all_companies  = client_service.get_all_companies_with_client()
    locked_clients = client_service.get_locked_clients()
    if request.method == "POST":
        data = request.form.to_dict()
        items = _parse_items(request.form)
        if not data.get("client_id"):
            flash("Please select a client.", "error")
            return render_template("invoices/form.html", invoice=data, items=[], clients=clients, products=products, all_companies=all_companies, locked_clients=locked_clients, action="new")
        if scope is not None and int(data["client_id"]) not in scope:
            abort(403)
        if not items:
            flash("Add at least one line item.", "error")
            return render_template("invoices/form.html", invoice=data, items=[], clients=clients, products=products, all_companies=all_companies, locked_clients=locked_clients, action="new")
        lock = client_service.get_client_lock_status(int(data["client_id"]))
        if lock["locked"] and data.get("status", "issued") != "draft":
            flash("Client is locked (" + "; ".join(lock["reasons"]) + ") — "
                  "invoice saved as a draft. Issue it once the lock clears.", "info")
        invoice_id = invoice_service.create_invoice(data, items)
        flash("Invoice created successfully.", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    return render_template("invoices/form.html", invoice={}, items=[], clients=clients, products=products, all_companies=all_companies, locked_clients=locked_clients, action="new")


@bp.route("/<int:invoice_id>")
@login_required
@permission_required("invoices", "view")
def detail(invoice_id):
    invoice = invoice_service.get_invoice(invoice_id)
    if not invoice:
        flash("Invoice not found.", "error")
        return redirect(url_for("invoices.list_invoices"))
    _guard_invoice(invoice)
    items = invoice_service.get_invoice_items(invoice_id)
    payments = invoice_service.get_invoice_payments(invoice_id)
    stock_status = (invoice_service.check_stock_for_issue(invoice_id)
                    if invoice["status"] == "draft" else [])
    # Build pcs-per-carton map for the pcs/box toggle on the line items table
    from ..database import get_db
    db = get_db()
    item_ppc = {}
    for it in items:
        ppc = 0
        if it["sub_product_id"]:
            try:
                row = db.execute("SELECT pcs_per_carton FROM sub_products WHERE id=?",
                                 (it["sub_product_id"],)).fetchone()
                if row and row["pcs_per_carton"]:
                    ppc = int(row["pcs_per_carton"] or 0)
            except Exception:
                ppc = 0
        if not ppc and it["product_id"]:
            try:
                row = db.execute("SELECT pcs_per_carton FROM products WHERE id=?",
                                 (it["product_id"],)).fetchone()
                if row and row["pcs_per_carton"]:
                    ppc = int(row["pcs_per_carton"] or 0)
            except Exception:
                ppc = 0
        item_ppc[it["id"]] = ppc
    prev_id, next_id = invoice_service.get_adjacent_invoices(invoice_id)
    client_lock = (client_service.get_client_lock_status(invoice["client_id"])
                   if invoice["status"] == "draft"
                   else {"locked": False, "reasons": []})
    return render_template("invoices/detail.html", invoice=invoice, items=items,
                           payments=payments, stock_status=stock_status,
                           item_ppc=item_ppc, prev_id=prev_id, next_id=next_id,
                           client_lock=client_lock)


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("invoices", "edit")
def edit_invoice(invoice_id):
    invoice = invoice_service.get_invoice(invoice_id)
    if not invoice:
        flash("Invoice not found.", "error")
        return redirect(url_for("invoices.list_invoices"))
    _guard_invoice(invoice)
    scope = _scope()
    clients        = client_service.get_all_clients()
    if scope is not None:
        clients = [c for c in clients if c["id"] in scope]
    products       = _build_product_choices()
    all_companies  = client_service.get_all_companies_with_client()
    locked_clients = client_service.get_locked_clients()
    if request.method == "POST":
        data = request.form.to_dict()
        items = _parse_items(request.form)
        if not data.get("client_id"):
            flash("Please select a client.", "error")
            existing_items = invoice_service.get_invoice_items(invoice_id)
            return render_template("invoices/form.html", invoice=data, items=existing_items, clients=clients, products=products, all_companies=all_companies, locked_clients=locked_clients, action="edit", invoice_id=invoice_id)
        if not items:
            flash("Add at least one line item.", "error")
            existing_items = invoice_service.get_invoice_items(invoice_id)
            return render_template("invoices/form.html", invoice=data, items=existing_items, clients=clients, products=products, all_companies=all_companies, locked_clients=locked_clients, action="edit", invoice_id=invoice_id)
        if scope is not None and int(data["client_id"]) not in scope:
            abort(403)
        ok, errors = invoice_service.update_invoice(invoice_id, data, items)
        if not ok:
            for e in errors:
                if e.get("type") == "locked":
                    flash("Saved as draft — cannot issue: client is locked ("
                          + "; ".join(e["reasons"]) + ").", "error")
                else:
                    flash(
                        f"Saved as draft — cannot issue: insufficient stock for '{e['name']}': "
                        f"need {e['required']:.0f}, have {e['available']:.0f} in warehouse.",
                        "error",
                    )
        else:
            flash("Invoice updated.", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    items = invoice_service.get_invoice_items(invoice_id)
    return render_template("invoices/form.html", invoice=dict(invoice), items=[dict(i) for i in items], clients=clients, products=products, all_companies=all_companies, locked_clients=locked_clients, action="edit", invoice_id=invoice_id)


@bp.route("/<int:invoice_id>/status", methods=["POST"])
@login_required
@permission_required("invoices", "edit")
def update_status(invoice_id):
    _guard_invoice(invoice_service.get_invoice(invoice_id))
    status = request.form.get("status")
    valid = {"draft", "issued", "paid", "cancelled", "partial"}
    if status not in valid:
        flash("Invalid status.", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    ok, errors = invoice_service.update_invoice_status(invoice_id, status)
    if not ok:
        for e in errors:
            if e.get("type") == "locked":
                flash("Cannot issue — client is locked ("
                      + "; ".join(e["reasons"]) + ").", "error")
            else:
                flash(
                    f"Cannot issue — insufficient stock for '{e['name']}': "
                    f"need {e['required']:.0f}, have {e['available']:.0f} in warehouse.",
                    "error",
                )
    else:
        flash(f"Status updated to {status}.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/delete", methods=["POST"])
@login_required
@permission_required("invoices", "delete")
def delete_invoice(invoice_id):
    _guard_invoice(invoice_service.get_invoice(invoice_id))
    invoice_service.delete_invoice(invoice_id)
    flash("Invoice deleted.", "success")
    return redirect(url_for("invoices.list_invoices"))

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from ..services import client_service, region_service
from ..services.payment_service import recalculate_client_balance, reconcile_all_clients
from ..services.auth_service import permission_required, get_scoped_client_ids, get_own_scoped_client_ids
from ..database import get_db

bp = Blueprint("clients", __name__, url_prefix="/clients")


def _scope():
    """Client ids the current user may see, or None for unrestricted.
    Sales managers get their team's clients; anyone else with clients
    assigned directly to them (clients.sales_rep_id) gets just those.
    (Not `or` — an empty-but-not-None team scope must stay empty, not
    fall through to the individual check.)"""
    scope = get_scoped_client_ids(current_user)
    return scope if scope is not None else get_own_scoped_client_ids(current_user)


def _guard_client(client_id):
    """403 when a row-scoped user (sales role) reaches for a foreign client."""
    scope = _scope()
    if scope is not None and client_id not in scope:
        abort(403)


@bp.route("/")
@login_required
@permission_required("clients", "view")
def list_clients():
    clients = client_service.get_all_clients_with_companies()
    scope = _scope()
    if scope is not None:
        clients = [c for c in clients if c["id"] in scope]
    can_financials = current_user.has_permission("clients", "financials")
    can_locks_view = (current_user.has_permission("clients", "locks_view")
                      or current_user.has_permission("clients", "locks_edit"))
    locked_clients = client_service.get_locked_clients() if can_locks_view else {}
    return render_template("clients/list.html", clients=clients, can_financials=can_financials,
                           locked_clients=locked_clients)


def _parse_companies(form):
    """Extract companies[N][field] entries from a flat form dict."""
    companies = []
    i = 0
    while True:
        name = form.get(f"companies[{i}][name]", "").strip()
        if not name and f"companies[{i}][name]" not in form:
            break
        if name:
            companies.append({
                "id":                   form.get(f"companies[{i}][id]", "").strip() or None,
                "name":                 name,
                "tax_id":               form.get(f"companies[{i}][tax_id]", "").strip() or None,
                "opening_balance_amt":  form.get(f"companies[{i}][opening_balance_amt]", "0"),
                "opening_balance_type": form.get(f"companies[{i}][opening_balance_type]", "debt"),
            })
        i += 1
    return companies


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("clients", "create")
def new_client():
    can_locks = current_user.has_permission("clients", "locks_edit")
    if request.method == "POST":
        data = request.form.to_dict()
        companies = _parse_companies(request.form)
        if not data.get("name"):
            flash("Client name is required.", "error")
            return render_template("clients/form.html", client=data, companies=companies, action="new", can_locks=can_locks)
        if not companies:
            flash("At least one company is required.", "error")
            return render_template("clients/form.html", client=data, companies=companies, action="new", can_locks=can_locks)
        client_id = client_service.create_client(data, companies, include_locks=can_locks)
        flash("Client created successfully.", "success")
        return redirect(url_for("clients.detail", client_id=client_id))
    return render_template("clients/form.html", client={}, companies=[], action="new", can_locks=can_locks)


@bp.route("/<int:client_id>")
@login_required
@permission_required("clients", "view")
def detail(client_id):
    import os
    _guard_client(client_id)
    client = client_service.get_client(client_id)
    if not client:
        flash("Client not found.", "error")
        return redirect(url_for("clients.list_clients"))
    invoices = client_service.get_client_invoices(client_id)
    can_financials = current_user.has_permission("clients", "financials")
    balance = client_service.get_client_balance(client_id) if can_financials else None
    companies_raw = client_service.get_companies(client_id)
    if can_financials:
        companies = [
            {**dict(c), "balance": client_service.get_company_balance(c["id"], client_id)}
            for c in companies_raw
        ]
    else:
        companies = [dict(c) for c in companies_raw]
    product_breakdown = client_service.get_client_product_breakdown(client_id)
    lock = client_service.get_client_lock_status(client_id)
    can_locks = current_user.has_permission("clients", "locks_edit")
    # Edit ability implies seeing the lock card
    can_locks_view = can_locks or current_user.has_permission("clients", "locks_view")
    rep_name = client_service.get_client_rep_name(client_id)
    return render_template("clients/detail.html", client=client, invoices=invoices,
                           companies=companies, balance=balance, can_financials=can_financials,
                           product_breakdown=product_breakdown, lock=lock, can_locks=can_locks,
                           can_locks_view=can_locks_view, rep_name=rep_name,
                           ola_maps_api_key=os.environ.get("OLA_MAPS_API_KEY", ""))


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("clients", "edit")
def edit_client(client_id):
    _guard_client(client_id)
    client = client_service.get_client(client_id)
    if not client:
        flash("Client not found.", "error")
        return redirect(url_for("clients.list_clients"))
    can_locks = current_user.has_permission("clients", "locks_edit")
    if request.method == "POST":
        data = request.form.to_dict()
        companies = _parse_companies(request.form)
        if not data.get("name"):
            flash("Client name is required.", "error")
            existing = client_service.get_companies(client_id)
            return render_template("clients/form.html", client=data, companies=[dict(c) for c in existing],
                                   action="edit", client_id=client_id, can_locks=can_locks)
        if not companies:
            flash("At least one company is required.", "error")
            existing = client_service.get_companies(client_id)
            return render_template("clients/form.html", client=data, companies=[dict(c) for c in existing],
                                   action="edit", client_id=client_id, can_locks=can_locks)
        client_service.update_client(client_id, data, companies, include_locks=can_locks)
        flash("Client updated successfully.", "success")
        return redirect(url_for("clients.detail", client_id=client_id))
    companies = [dict(c) for c in client_service.get_companies(client_id)]
    return render_template("clients/form.html", client=dict(client), companies=companies,
                           action="edit", client_id=client_id, can_locks=can_locks)


@bp.route("/<int:client_id>/tally-lock", methods=["POST"])
@login_required
@permission_required("clients", "locks_edit")
def toggle_tally_lock(client_id):
    """Quick on/off for the manual tally lock from the client detail page."""
    _guard_client(client_id)
    client = client_service.get_client(client_id)
    if not client:
        return jsonify({"error": "Client not found."}), 404
    new_state = 0 if client["tally_lock"] else 1
    client_service.set_lock_settings(client_id, tally_lock=new_state)
    return jsonify({"ok": True, "tally_lock": bool(new_state)})


@bp.route("/<int:client_id>/ledger")
@login_required
@permission_required("clients", "financials")
def ledger(client_id):
    _guard_client(client_id)
    client = client_service.get_client(client_id)
    if not client:
        return jsonify({"error": "Not found"}), 404

    date_from  = request.args.get("from")        # YYYY-MM-DD or None
    date_to    = request.args.get("to")          # YYYY-MM-DD or None
    company_id = request.args.get("company_id", type=int)

    db = get_db()

    if company_id:
        co_row  = db.execute("SELECT * FROM client_companies WHERE id=?", (company_id,)).fetchone()
        opening = float(co_row["opening_balance"] or 0) if co_row else 0.0
        invoices = db.execute(
            "SELECT i.id, i.invoice_number, i.issue_date, i.total, i.amount_paid, i.status, "
            "cc.name AS company_name "
            "FROM invoices i LEFT JOIN client_companies cc ON i.company_id = cc.id "
            "WHERE i.client_id=? AND i.company_id=? AND i.status != 'cancelled' "
            "ORDER BY i.issue_date, i.id",
            (client_id, company_id),
        ).fetchall()
        payments = db.execute(
            "SELECT p.id, p.amount, p.payment_date, p.method, p.reference, p.notes, "
            "p.is_opening_balance, "
            "cc.name AS company_name, "
            "(SELECT GROUP_CONCAT(i.invoice_number, ', ') "
            "   FROM payment_allocations pa JOIN invoices i ON i.id=pa.invoice_id "
            "  WHERE pa.payment_id=p.id) AS invoice_numbers "
            "FROM payments p LEFT JOIN client_companies cc ON p.company_id = cc.id "
            "WHERE p.client_id=? AND p.company_id=? ORDER BY p.payment_date, p.id",
            (client_id, company_id),
        ).fetchall()
        manual = []
    else:
        opening  = float(client["opening_balance"] or 0)
        invoices = db.execute(
            "SELECT i.id, i.invoice_number, i.issue_date, i.total, i.amount_paid, i.status, "
            "cc.name AS company_name "
            "FROM invoices i LEFT JOIN client_companies cc ON i.company_id = cc.id "
            "WHERE i.client_id=? AND i.status != 'cancelled' ORDER BY i.issue_date, i.id",
            (client_id,),
        ).fetchall()
        payments = db.execute(
            "SELECT p.id, p.amount, p.payment_date, p.method, p.reference, p.notes, "
            "p.is_opening_balance, "
            "cc.name AS company_name, "
            "(SELECT GROUP_CONCAT(i.invoice_number, ', ') "
            "   FROM payment_allocations pa JOIN invoices i ON i.id=pa.invoice_id "
            "  WHERE pa.payment_id=p.id) AS invoice_numbers "
            "FROM payments p LEFT JOIN client_companies cc ON p.company_id = cc.id "
            "WHERE p.client_id=? ORDER BY p.payment_date, p.id",
            (client_id,),
        ).fetchall()
        manual = db.execute(
            "SELECT * FROM ledger_entries WHERE client_id=? ORDER BY entry_date, id",
            (client_id,),
        ).fetchall()

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
                label = r["invoice_number"]
                if r.get("company_name"):
                    label += f" ({r['company_name']})"
                entries.append({"date": r["issue_date"] or "", "type": "invoice",
                                "label": label, "company": r.get("company_name") or "",
                                "debit": r["total"], "credit": 0, "running": running})
            elif item["kind"] == "payment":
                running += float(r["amount"])
                parts = [r["method"]] if r["method"] else []
                if r["reference"]: parts.append(r["reference"])
                if r["notes"]:     parts.append(r["notes"])
                if r.get("is_opening_balance"):
                    parts.append("opening balance")
                elif r.get("invoice_numbers"):
                    parts.append(f"applied to {r['invoice_numbers']}")
                label = " — ".join(parts) if parts else "Payment"
                if r.get("company_name"):
                    label += f" ({r['company_name']})"
                entries.append({"date": r["payment_date"] or "", "type": "payment",
                                "label": label, "company": r.get("company_name") or "",
                                "invoice_numbers": r.get("invoice_numbers"),
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
    _guard_client(client_id)
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


@bp.route("/<int:client_id>/recalculate", methods=["POST"])
@login_required
@permission_required("clients", "financials")
def recalculate(client_id):
    _guard_client(client_id)
    recalculate_client_balance(client_id)
    return jsonify({"ok": True})


@bp.route("/reconcile-all", methods=["POST"])
@login_required
@permission_required("clients", "financials")
def reconcile_all():
    """Apply every client's unallocated payments to their oldest unpaid invoices."""
    if _scope() is not None:
        abort(403)   # global operation — not for row-scoped (sales) users
    summary = reconcile_all_clients()
    return jsonify({"ok": True, **summary})


@bp.route("/<int:client_id>/delete", methods=["POST"])
@login_required
@permission_required("clients", "delete")
def delete_client(client_id):
    _guard_client(client_id)
    client_service.delete_client(client_id)
    flash("Client deleted.", "success")
    return redirect(url_for("clients.list_clients"))


# ── Regions (operating areas) ─────────────────────────────────────────────────

@bp.route("/regions/all")
@login_required
@permission_required("clients", "view")
def regions_all():
    """Every in-scope client's region geometries, for the dashboard's
    consolidated coverage map. Scoped exactly like the rest of the client
    pages — sales managers see their team's clients, individually-assigned
    reps see their own, everyone else sees all."""
    return jsonify(region_service.get_client_region_shapes(_scope()))


@bp.route("/<int:client_id>/regions")
@login_required
@permission_required("clients", "view")
def regions_list(client_id):
    _guard_client(client_id)
    return jsonify(region_service.get_client_regions(client_id))


@bp.route("/regions/search")
@login_required
@permission_required("clients", "view")
def regions_search():
    return jsonify(region_service.search_areas(request.args.get("q", "")))


@bp.route("/regions/reverse")
@login_required
@permission_required("clients", "view")
def regions_reverse():
    """Boundary candidates (state/district/city/town/suburb, from Nominatim
    + BharatMaps) covering a dropped map pin, for the click-to-pick UI."""
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates."}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates."}), 400
    return jsonify(region_service.reverse_candidates(lat, lon))


@bp.route("/<int:client_id>/regions", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def regions_add(client_id):
    _guard_client(client_id)
    data = request.get_json(silent=True) or {}
    if data.get("boundary_id"):
        # Pin-drop picker — candidate boundary already resolved & cached.
        try:
            boundary_id = region_service.add_region_by_boundary_id(client_id, int(data["boundary_id"]))
        except (TypeError, ValueError):
            boundary_id = None
    else:
        boundary_id = region_service.add_region(
            client_id, data.get("osm_type"), data.get("osm_id"), data.get("name"),
        )
    if boundary_id is None:
        return jsonify({"error": "No boundary data found for that area."}), 404
    return jsonify({"ok": True})


@bp.route("/<int:client_id>/regions/<int:link_id>/delete", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def regions_remove(client_id, link_id):
    _guard_client(client_id)
    region_service.remove_region(client_id, link_id)
    return jsonify({"ok": True})


def _valid_trim_geometry(geometry):
    return (isinstance(geometry, dict) and geometry.get("type") in ("Polygon", "MultiPolygon")
            and isinstance(geometry.get("coordinates"), list))


@bp.route("/<int:client_id>/regions/custom", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def regions_save_custom(client_id):
    """Save a brush/erase-drawn custom area — creates a new one, or updates
    an existing one in place when boundary_id is given (a client can have
    several independent custom areas, each edited/extended separately)."""
    _guard_client(client_id)
    data = request.get_json(silent=True) or {}
    geometry = data.get("geometry")
    if not _valid_trim_geometry(geometry):
        return jsonify({"error": "Invalid or empty shape."}), 400
    name = (data.get("name") or "Custom Area").strip()[:200]
    try:
        boundary_id = int(data["boundary_id"]) if data.get("boundary_id") else None
    except (TypeError, ValueError):
        boundary_id = None
    result = region_service.save_custom_region(client_id, geometry, name, boundary_id)
    if result is None:
        return jsonify({"error": "Custom area not found."}), 404
    return jsonify({"ok": True, "boundary_id": result})


@bp.route("/<int:client_id>/regions/<int:link_id>/trim", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def regions_trim(client_id, link_id):
    """Apply an erase-tool trim to an existing (named or custom) region.
    geometry: null means the erase removed it entirely — unlinked instead."""
    _guard_client(client_id)
    data = request.get_json(silent=True) or {}
    geometry = data.get("geometry")
    if geometry is not None and not _valid_trim_geometry(geometry):
        return jsonify({"error": "Invalid shape."}), 400
    ok = region_service.trim_region(client_id, link_id, geometry)
    if not ok:
        return jsonify({"error": "Region not found."}), 404
    return jsonify({"ok": True})


@bp.route("/<int:client_id>/regions/<int:link_id>/rename", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def regions_rename(client_id, link_id):
    """Rename a custom area (named places can't be renamed — see
    region_service.rename_region)."""
    _guard_client(client_id)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    ok = region_service.rename_region(client_id, link_id, name)
    if not ok:
        return jsonify({"error": "Only custom areas can be renamed."}), 404
    return jsonify({"ok": True})


# ── Company endpoints ─────────────────────────────────────────────────────────

@bp.route("/api/<int:client_id>/companies")
@login_required
def api_companies(client_id):
    _guard_client(client_id)
    companies = client_service.get_companies(client_id)
    return jsonify([dict(c) for c in companies])


@bp.route("/<int:client_id>/companies", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def add_company(client_id):
    _guard_client(client_id)
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "Company name is required"}), 400
    # Accept either signed opening_balance or amt+type
    if "opening_balance_amt" not in data and "opening_balance" in data:
        data["opening_balance_amt"] = abs(float(data.get("opening_balance") or 0))
    company_id = client_service.create_company(client_id, data)
    return jsonify({"ok": True, "id": company_id, "name": data["name"]})


@bp.route("/<int:client_id>/companies/<int:company_id>", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def edit_company(client_id, company_id):
    _guard_client(client_id)
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "Company name is required"}), 400
    if "opening_balance_amt" not in data and "opening_balance" in data:
        data["opening_balance_amt"] = abs(float(data.get("opening_balance") or 0))
    client_service.update_company(company_id, data)
    return jsonify({"ok": True})


@bp.route("/<int:client_id>/companies/<int:company_id>/delete", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def delete_company(client_id, company_id):
    _guard_client(client_id)
    client_service.delete_company(company_id)
    return jsonify({"ok": True})


# ── Sales rep assignments ─────────────────────────────────────────────────────

@bp.route("/assignments")
@login_required
@permission_required("clients", "edit")
def assignments():
    if _scope() is not None:
        abort(403)   # rep assignment is an owner/admin function
    reps    = [dict(r) for r in client_service.get_sales_reps()]
    clients = [dict(c) for c in client_service.get_clients_with_reps()]
    return render_template("clients/assignments.html", reps=reps, clients=clients)


@bp.route("/<int:client_id>/assign-rep", methods=["POST"])
@login_required
@permission_required("clients", "edit")
def assign_rep(client_id):
    if _scope() is not None:
        abort(403)   # rep assignment is an owner/admin function
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if user_id in ("", None):
        user_id = None
    else:
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid staff member."}), 400
    if not client_service.set_sales_rep(client_id, user_id):
        return jsonify({"error": "Client not found or staff member not active."}), 404
    return jsonify({"ok": True})

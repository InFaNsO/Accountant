import json
import os
from datetime import date

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..services import lead_service, overlap_service, product_service, region_service
from ..services.auth_service import get_scoped_client_ids, get_own_scoped_client_ids, permission_required
from .region_routes import register_region_routes

bp = Blueprint("leads", __name__, url_prefix="/leads")


def _scope():
    return lead_service.visible_lead_ids(current_user)


def _guard_lead(lead_id):
    """403 when a row-scoped user (sales role) reaches for a lead outside
    their team."""
    scope = _scope()
    if scope is not None and lead_id not in scope:
        abort(403)


register_region_routes(bp, owner="lead", module="leads", guard=_guard_lead)


def _client_scope():
    scope = get_scoped_client_ids(current_user)
    return scope if scope is not None else get_own_scoped_client_ids(current_user)


def _redact(overlaps):
    """Overlaps the current user may see in full. A client or lead outside
    their scope still counts — a clash is a clash — but shows no name or link."""
    client_scope = _client_scope() if current_user.has_permission("clients", "view") else set()
    lead_scope = _scope()
    out = []
    for o in overlaps:
        if o["kind"] == "client":
            visible = client_scope is None or o["id"] in client_scope
        else:
            visible = lead_scope is None or o["id"] in lead_scope
        if not visible:
            o = {**o, "id": None, "name": "Another client" if o["kind"] == "client" else "Another lead",
                 "their_regions": [], "hidden": True}
        out.append(o)
    return out


def _interests_from(form):
    return {key: lead_service.parse_ids(form.getlist(key))
            for key in ("category_ids", "product_ids", "segment_ids")}


def _apply_area_changes(lead_id, form):
    """The form's quick area picker: add_areas holds JSON place candidates
    from the search box, remove_regions holds link ids to drop. Returns the
    names that couldn't be resolved to a boundary."""
    for link_id in form.getlist("remove_regions"):
        try:
            region_service.remove_region(lead_id, int(link_id), "lead")
        except (TypeError, ValueError):
            pass
    missing = []
    for raw in form.getlist("add_areas"):
        try:
            place = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if place.get("boundary_id"):
            ok = region_service.add_region_by_boundary_id(lead_id, int(place["boundary_id"]), "lead")
        else:
            ok = region_service.add_region(lead_id, place.get("osm_type"), place.get("osm_id"),
                                           place.get("name"), "lead")
        if ok is None:
            missing.append(place.get("name") or "an area")
    return missing


def _form_context(lead, interest_ids, regions, action, lead_id=None):
    products = product_service.get_all_products(active_only=True)
    return dict(
        lead=lead, interest_ids=interest_ids, regions=regions, action=action, lead_id=lead_id,
        statuses=lead_service.STATUSES,
        categories=product_service.get_all_categories(),
        products=[{"id": p["id"], "name": p["name"], "category": p["category_name"] or "Uncategorised"}
                  for p in products],
        segments=lead_service.get_segments(),
        sources=lead_service.get_sources(),
        users=lead_service.get_assignable_users(),
        can_manage_segments=current_user.has_permission("leads", "edit"),
        ola_maps_api_key=os.environ.get("OLA_MAPS_API_KEY", ""),
    )


# ── List ──────────────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
@permission_required("leads", "view")
def list_leads():
    leads = lead_service.list_leads(_scope())
    overlap = overlap_service.lead_overlap_summary([l["id"] for l in leads])
    for l in leads:
        o = overlap.get(l["id"], {"clients": [], "leads": []})
        l["overlap_clients"], l["overlap_leads"] = o["clients"], o["leads"]
    counts = {k: 0 for k, *_ in lead_service.STATUSES}
    for l in leads:
        counts[l["status"]] = counts.get(l["status"], 0) + 1
    states = sorted({(l["state"] or "").strip() for l in leads if l["state"]}, key=str.lower)
    return render_template(
        "leads/list.html", leads=leads, statuses=lead_service.STATUSES,
        status_map=lead_service.STATUS_MAP, status_counts=counts, states=states,
        open_statuses=lead_service.OPEN_STATUSES, segments=lead_service.get_segments(),
        categories=product_service.get_all_categories(), users=lead_service.get_assignable_users(),
        overlap_ready=overlap_service.available(), today_iso=date.today().isoformat(),
    )


# ── Create / edit ─────────────────────────────────────────────────────────────

@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("leads", "create")
def new_lead():
    if request.method == "POST":
        data = request.form.to_dict()
        interests = _interests_from(request.form)
        if not (data.get("company_name") or "").strip():
            flash("Company name is required.", "error")
            return render_template("leads/form.html", **_form_context(data, interests, [], "new"))
        lead_id = lead_service.create_lead(data, interests, current_user.id)
        missing = _apply_area_changes(lead_id, request.form)
        if missing:
            flash("No boundary found for: " + ", ".join(missing) + ". Add it on the map instead.", "error")
        if request.form.get("submit") == "add_another":
            flash(f"Saved {data['company_name'].strip()}. Next lead:", "success")
            keep = {k: data.get(k) for k in ("source", "assigned_to", "state", "status") if data.get(k)}
            return redirect(url_for("leads.new_lead", **keep))
        flash("Lead added.", "success")
        return redirect(url_for("leads.detail", lead_id=lead_id))
    # "Save & add another" carries the batch's source/assignee/state forward.
    lead = {k: request.args.get(k) for k in ("source", "assigned_to", "state", "status") if request.args.get(k)}
    empty = {"category_ids": [], "product_ids": [], "segment_ids": []}
    return render_template("leads/form.html", **_form_context(lead, empty, [], "new"))


@bp.route("/<int:lead_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("leads", "edit")
def edit_lead(lead_id):
    _guard_lead(lead_id)
    lead = lead_service.get_lead(lead_id)
    if not lead:
        abort(404)
    if request.method == "POST":
        data = request.form.to_dict()
        interests = _interests_from(request.form)
        if not (data.get("company_name") or "").strip():
            flash("Company name is required.", "error")
            return render_template("leads/form.html", **_form_context(
                data, interests, region_service.get_regions(lead_id, "lead"), "edit", lead_id))
        lead_service.update_lead(lead_id, data, interests, current_user.id)
        missing = _apply_area_changes(lead_id, request.form)
        if missing:
            flash("No boundary found for: " + ", ".join(missing) + ". Add it on the map instead.", "error")
        flash("Lead updated.", "success")
        return redirect(url_for("leads.detail", lead_id=lead_id))
    return render_template("leads/form.html", **_form_context(
        dict(lead), lead_service.get_interest_ids(lead_id),
        region_service.get_regions(lead_id, "lead"), "edit", lead_id))


# ── Detail ────────────────────────────────────────────────────────────────────

@bp.route("/<int:lead_id>")
@login_required
@permission_required("leads", "view")
def detail(lead_id):
    _guard_lead(lead_id)
    lead = lead_service.get_lead(lead_id)
    if not lead:
        flash("Lead not found.", "error")
        return redirect(url_for("leads.list_leads"))
    duplicates = lead_service.find_duplicates(lead["phone"], lead["company_name"], lead_id, lead["alt_phone"])
    if lead["client_id"]:
        duplicates = [d for d in duplicates if not (d["kind"] == "client" and d["id"] == lead["client_id"])]
    can_convert = (not lead["client_id"]
                   and current_user.has_permission("clients", "create")
                   and current_user.has_permission("leads", "edit"))
    digits = lead_service.normalize_phone(lead["phone"])
    return render_template(
        "leads/detail.html", lead=lead, interests=lead_service.get_interests(lead_id),
        wa_number=("91" + digits) if len(digits) == 10 else None,
        activities=lead_service.get_activities(lead_id), statuses=lead_service.STATUSES,
        status_map=lead_service.STATUS_MAP, duplicates=duplicates,
        overlaps=_redact(overlap_service.overlaps_for("lead", lead_id, with_geometry=True)),
        overlap_ready=overlap_service.available(), can_convert=can_convert,
        today_iso=date.today().isoformat(), open_statuses=lead_service.OPEN_STATUSES,
        ola_maps_api_key=os.environ.get("OLA_MAPS_API_KEY", ""),
    )


@bp.route("/<int:lead_id>/overlaps")
@login_required
@permission_required("leads", "view")
def overlaps(lead_id):
    """Fresh overlap list (with shapes) — the lead page re-asks after every
    change on the map."""
    _guard_lead(lead_id)
    return jsonify(_redact(overlap_service.overlaps_for("lead", lead_id, with_geometry=True)))


@bp.route("/duplicates")
@login_required
@permission_required("leads", "view")
def duplicates():
    """Live check from the lead form: clients/leads sharing this phone or
    company name."""
    try:
        exclude = int(request.args.get("exclude")) if request.args.get("exclude") else None
    except ValueError:
        exclude = None
    found = lead_service.find_duplicates(request.args.get("phone"), request.args.get("company"),
                                         exclude, request.args.get("alt_phone"))
    can_clients = current_user.has_permission("clients", "view")
    client_scope = _client_scope() if can_clients else set()
    lead_scope = _scope()
    out = []
    for d in found:
        if d["kind"] == "client":
            visible = client_scope is None or d["id"] in client_scope
        else:
            visible = lead_scope is None or d["id"] in lead_scope
        out.append(d if visible else {**d, "id": None,
                                      "name": "another client" if d["kind"] == "client" else "another lead"})
    return jsonify(out)


# ── Status, follow-up, notes ──────────────────────────────────────────────────

@bp.route("/<int:lead_id>/status", methods=["POST"])
@login_required
@permission_required("leads", "edit")
def set_status(lead_id):
    _guard_lead(lead_id)
    data = request.get_json(silent=True) or request.form
    status = data.get("status")
    if not lead_service.set_status(lead_id, status, current_user.id, data.get("note")):
        if request.is_json:
            return jsonify({"error": "Unknown lead or status."}), 400
        abort(400)
    if request.is_json:
        s = lead_service.STATUS_MAP[status]
        return jsonify({"ok": True, "status": status, "label": s["label"], "color": s["color"],
                        "dark": s["dark"], "bg": s["bg"]})
    return redirect(url_for("leads.detail", lead_id=lead_id))


@bp.route("/<int:lead_id>/follow-up", methods=["POST"])
@login_required
@permission_required("leads", "edit")
def set_follow_up(lead_id):
    _guard_lead(lead_id)
    if not lead_service.set_follow_up(lead_id, (request.form.get("next_follow_up") or "").strip()):
        flash("Couldn't set that follow-up date.", "error")
    return redirect(url_for("leads.detail", lead_id=lead_id))


@bp.route("/<int:lead_id>/notes", methods=["POST"])
@login_required
@permission_required("leads", "edit")
def add_note(lead_id):
    _guard_lead(lead_id)
    body = request.form.get("body") or request.form.get("note")
    if not lead_service.add_note(lead_id, current_user.id, body):
        flash("Write something first.", "error")
    return redirect(url_for("leads.detail", lead_id=lead_id) + "#activity")


@bp.route("/<int:lead_id>/notes/<int:activity_id>/delete", methods=["POST"])
@login_required
@permission_required("leads", "edit")
def delete_note(lead_id, activity_id):
    _guard_lead(lead_id)
    lead_service.delete_note(lead_id, activity_id)
    return redirect(url_for("leads.detail", lead_id=lead_id) + "#activity")


@bp.route("/<int:lead_id>/delete", methods=["POST"])
@login_required
@permission_required("leads", "delete")
def delete_lead(lead_id):
    _guard_lead(lead_id)
    lead_service.delete_lead(lead_id)
    flash("Lead deleted.", "success")
    return redirect(url_for("leads.list_leads"))


# ── Segments ──────────────────────────────────────────────────────────────────

@bp.route("/segments")
@login_required
@permission_required("leads", "view")
def segments():
    return render_template("leads/segments.html", segments=lead_service.get_segments())


@bp.route("/segments", methods=["POST"])
@login_required
def create_segment():
    """From the segments page, or inline from the lead form (JSON) — so
    adding a lead is enough to add a segment for it."""
    if not (current_user.has_permission("leads", "create") or current_user.has_permission("leads", "edit")):
        abort(403)
    data = request.get_json(silent=True) or request.form
    segment_id, created = lead_service.create_segment(data.get("name"))
    if request.is_json:
        if not segment_id:
            return jsonify({"error": "Name is required."}), 400
        return jsonify({"ok": True, "id": segment_id, "name": " ".join(data.get("name").split()),
                        "created": created})
    if not segment_id:
        flash("Name is required.", "error")
    elif not created:
        flash("That segment already exists.", "info")
    return redirect(url_for("leads.segments"))


@bp.route("/segments/<int:segment_id>/rename", methods=["POST"])
@login_required
@permission_required("leads", "edit")
def rename_segment(segment_id):
    if not lead_service.rename_segment(segment_id, request.form.get("name")):
        flash("Couldn't rename — the name is empty or already used.", "error")
    return redirect(url_for("leads.segments"))


@bp.route("/segments/<int:segment_id>/delete", methods=["POST"])
@login_required
@permission_required("leads", "delete")
def delete_segment(segment_id):
    lead_service.delete_segment(segment_id)
    flash("Segment deleted.", "success")
    return redirect(url_for("leads.segments"))

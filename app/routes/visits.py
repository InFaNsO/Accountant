from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from ..services import visit_service, geocoding_service
from ..services.auth_service import (
    permission_required, get_all_users, get_team_user_ids, get_scoped_client_ids,
)

bp = Blueprint("visits", __name__, url_prefix="/visits")


def _team_scope():
    """(team_user_ids, scoped_client_ids) for a sales manager, else (None, None)."""
    if getattr(current_user, "role", None) == "sales":
        return get_team_user_ids(current_user.id), get_scoped_client_ids(current_user)
    return None, None


@bp.route("/")
@login_required
def index():
    """Admins land on the map; staff with create-only go to check-in."""
    if current_user.has_permission("visits", "view"):
        return redirect(url_for("visits.map_page"))
    if current_user.has_permission("visits", "create"):
        return redirect(url_for("visits.check_in_page"))
    flash("You don't have permission to do that.", "error")
    return redirect(url_for("dashboard.index"))


# ── Staff: check-in ───────────────────────────────────────────────────────────

@bp.route("/check-in")
@login_required
@permission_required("visits", "create")
def check_in_page():
    clients = [dict(c) for c in visit_service.get_checkin_clients()]
    open_visit = visit_service.get_open_visit(current_user.id)
    today = [dict(v) for v in visit_service.get_my_visits_today(current_user.id)]
    return render_template(
        "visits/check_in.html",
        clients=clients,
        open_visit=dict(open_visit) if open_visit else None,
        today_visits=today,
        purposes=visit_service.PURPOSES,
        outcomes=visit_service.OUTCOMES,
    )


@bp.route("/check-in", methods=["POST"])
@login_required
@permission_required("visits", "create")
def check_in():
    data = request.get_json(silent=True) or {}
    if data.get("latitude") in (None, "") or data.get("longitude") in (None, ""):
        return jsonify({"error": "Location is required — allow GPS access and try again."}), 400
    if not data.get("client_id") and not (data.get("prospect_name") or "").strip():
        return jsonify({"error": "Pick a client or enter a prospect name."}), 400
    try:
        lat, lng = float(data["latitude"]), float(data["longitude"])
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates."}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "Invalid coordinates."}), 400
    visit_id = visit_service.create_visit(current_user.id, data)
    return jsonify({"ok": True, "id": visit_id})


@bp.route("/<int:visit_id>/check-out", methods=["POST"])
@login_required
@permission_required("visits", "create")
def check_out(visit_id):
    data = request.get_json(silent=True) or {}
    if data.get("outcome"):
        visit_service.update_outcome(
            visit_id, current_user.id, data["outcome"],
            notes=data.get("notes"), is_god=current_user.is_god(),
        )
    updated = visit_service.check_out(visit_id, current_user.id, is_god=current_user.is_god())
    if not updated:
        return jsonify({"error": "Visit not found or already checked out."}), 404
    return jsonify({"ok": True})


# ── Admin: map + APIs ─────────────────────────────────────────────────────────

@bp.route("/map")
@login_required
@permission_required("visits", "view")
def map_page():
    import os
    team_ids, _ = _team_scope()
    if team_ids is not None:
        # Sales manager: only their own team appears in the staff filter.
        staff = [dict(u) for u in get_all_users()
                 if u["is_active"] and u["id"] in team_ids]
    else:
        staff = [dict(u) for u in get_all_users() if u["is_active"]]
    return render_template("visits/map.html", staff=staff,
                           ola_maps_api_key=os.environ.get("OLA_MAPS_API_KEY", ""))


@bp.route("/api/visits")
@login_required
@permission_required("visits", "view")
def api_visits():
    team_ids, _ = _team_scope()
    rows = visit_service.get_visits(
        user_id=request.args.get("user_id", type=int),
        client_id=request.args.get("client_id", type=int),
        date_from=request.args.get("from"),
        date_to=request.args.get("to"),
        state=request.args.get("state"),
        user_ids=team_ids,
    )
    return jsonify([dict(r) for r in rows])


@bp.route("/api/clients/geo")
@login_required
@permission_required("visits", "view")
def api_clients_geo():
    _, client_ids = _team_scope()
    return jsonify(visit_service.get_clients_geo(client_ids=client_ids))


@bp.route("/api/boundary")
@login_required
@permission_required("visits", "view")
def api_boundary():
    """Locality/administrative boundary outline for a place name, e.g. 'Preet
    Vihar, New Delhi'. Used to draw an area outline on the map."""
    query = request.args.get("q", "")
    boundary = geocoding_service.get_area_boundary(query)
    if not boundary:
        return jsonify({"error": "No boundary found for that place."}), 404
    return jsonify(boundary)

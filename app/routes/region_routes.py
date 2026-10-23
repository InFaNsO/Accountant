"""The region-editor endpoints, shared by every page that has operating
regions (clients, leads). Each blueprint mounts the same set under its own
prefix — /clients/<id>/regions…, /leads/<id>/regions… — which is all the
shared editor (templates/_region_editor.html) needs to know.
"""
from flask import jsonify, request
from flask_login import login_required

from ..services import region_service
from ..services.auth_service import permission_required


def _valid_trim_geometry(geometry):
    return (isinstance(geometry, dict) and geometry.get("type") in ("Polygon", "MultiPolygon")
            and isinstance(geometry.get("coordinates"), list))


def register_region_routes(bp, *, owner, module, guard):
    """Mount the region endpoints on bp for one kind of owner.

    owner:  "client" | "lead" (see region_service._OWNERS)
    module: permission module gating the routes (view to read, edit to change)
    guard:  guard(owner_id) aborts when the current user may not reach that
            owner (row scoping); called before anything is read or written.
    """
    def _route(rule, endpoint, action, methods=("GET",)):
        def wrap(fn):
            view = login_required(permission_required(module, action)(fn))
            bp.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=list(methods))
            return fn
        return wrap

    @_route("/<int:owner_id>/regions", "regions_list", "view")
    def regions_list(owner_id):
        guard(owner_id)
        return jsonify(region_service.get_regions(owner_id, owner))

    @_route("/regions/search", "regions_search", "view")
    def regions_search():
        return jsonify(region_service.search_areas(request.args.get("q", "")))

    @_route("/regions/reverse", "regions_reverse", "view")
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

    @_route("/regions/resolve", "regions_resolve", "view")
    def regions_resolve():
        """A place-search result -> its boundary polygon (cached on first use),
        so a form can show exactly which area a name means before saving it."""
        osm_type, osm_id = request.args.get("osm_type"), request.args.get("osm_id")
        if osm_type not in (None, "", "node", "way", "relation"):
            return jsonify({"error": "Invalid place."}), 400
        boundary_id = region_service.resolve_boundary(osm_type, osm_id, request.args.get("name"))
        if boundary_id is None:
            return jsonify({"error": "No boundary data found for that area."}), 404
        return jsonify(region_service.get_boundary(boundary_id))

    @_route("/<int:owner_id>/regions", "regions_add", "edit", methods=("POST",))
    def regions_add(owner_id):
        guard(owner_id)
        data = request.get_json(silent=True) or {}
        if data.get("boundary_id"):
            # Pin-drop picker — candidate boundary already resolved & cached.
            try:
                boundary_id = region_service.add_region_by_boundary_id(
                    owner_id, int(data["boundary_id"]), owner)
            except (TypeError, ValueError):
                boundary_id = None
        else:
            boundary_id = region_service.add_region(
                owner_id, data.get("osm_type"), data.get("osm_id"), data.get("name"), owner,
            )
        if boundary_id is None:
            return jsonify({"error": "No boundary data found for that area."}), 404
        return jsonify({"ok": True})

    @_route("/<int:owner_id>/regions/<int:link_id>/delete", "regions_remove", "edit", methods=("POST",))
    def regions_remove(owner_id, link_id):
        guard(owner_id)
        region_service.remove_region(owner_id, link_id, owner)
        return jsonify({"ok": True})

    @_route("/<int:owner_id>/regions/custom", "regions_save_custom", "edit", methods=("POST",))
    def regions_save_custom(owner_id):
        """Save a brush/erase-drawn custom area — creates a new one, or updates
        an existing one in place when boundary_id is given (an owner can have
        several independent custom areas, each edited/extended separately)."""
        guard(owner_id)
        data = request.get_json(silent=True) or {}
        geometry = data.get("geometry")
        if not _valid_trim_geometry(geometry):
            return jsonify({"error": "Invalid or empty shape."}), 400
        name = (data.get("name") or "Custom Area").strip()[:200]
        try:
            boundary_id = int(data["boundary_id"]) if data.get("boundary_id") else None
        except (TypeError, ValueError):
            boundary_id = None
        result = region_service.save_custom_region(owner_id, geometry, name, boundary_id, owner)
        if result is None:
            return jsonify({"error": "Custom area not found."}), 404
        return jsonify({"ok": True, "boundary_id": result})

    @_route("/<int:owner_id>/regions/<int:link_id>/trim", "regions_trim", "edit", methods=("POST",))
    def regions_trim(owner_id, link_id):
        """Apply an erase-tool trim to an existing (named or custom) region.
        geometry: null means the erase removed it entirely — unlinked instead."""
        guard(owner_id)
        data = request.get_json(silent=True) or {}
        geometry = data.get("geometry")
        if geometry is not None and not _valid_trim_geometry(geometry):
            return jsonify({"error": "Invalid shape."}), 400
        ok = region_service.trim_region(owner_id, link_id, geometry, owner)
        if not ok:
            return jsonify({"error": "Region not found."}), 404
        return jsonify({"ok": True})

    @_route("/<int:owner_id>/regions/<int:link_id>/rename", "regions_rename", "edit", methods=("POST",))
    def regions_rename(owner_id, link_id):
        """Rename a custom area (named places can't be renamed — see
        region_service.rename_region)."""
        guard(owner_id)
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name is required."}), 400
        ok = region_service.rename_region(owner_id, link_id, name, owner)
        if not ok:
            return jsonify({"error": "Only custom areas can be renamed."}), 404
        return jsonify({"ok": True})

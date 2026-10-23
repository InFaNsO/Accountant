"""Operating regions: cities/towns/states a client (or a lead) works in, drawn
as boundary outlines on their page. Boundary polygons are resolved once
(Nominatim primary, BharatMaps state/district fallback when Nominatim has no
polygon) and cached in area_boundaries, shared across every client and lead
that selects the same place — adding a region someone else already has costs
zero extra API calls.

Every function that reads or changes one owner's regions takes owner="client"
(client_regions) or owner="lead" (lead_regions); both link tables have the same
shape, so the logic is identical and only the table differs.
"""
import json
import uuid

from ..database import get_db
from . import geocoding_service

# owner -> (link table, owner id column). Fixed strings, never user input, so
# they are safe to format into SQL.
_OWNERS = {
    "client": ("client_regions", "client_id"),
    "lead":   ("lead_regions",   "lead_id"),
}


def _owner(owner):
    return _OWNERS[owner]


def search_areas(query):
    """Search-as-you-type candidates for the region picker."""
    return geocoding_service.search_places(query)


def _get_cached_boundary(db, source_ref):
    return db.execute(
        "SELECT * FROM area_boundaries WHERE source_ref=?", (source_ref,)
    ).fetchone()


def _cache_boundary(db, source_ref, boundary):
    cur = db.execute(
        """INSERT INTO area_boundaries (source_ref, name, area_type, geometry, source)
           VALUES (?,?,?,?,?)""",
        (source_ref, boundary["name"], boundary.get("area_type", ""),
         json.dumps(boundary["geometry"]), boundary["source"]),
    )
    db.commit()
    return cur.lastrowid


def _link(db, owner_id, boundary_id, owner="client"):
    table, col = _owner(owner)
    db.execute(
        f"INSERT OR IGNORE INTO {table} ({col}, boundary_id) VALUES (?,?)",
        (owner_id, boundary_id),
    )
    db.commit()
    return boundary_id


def add_region_by_boundary_id(owner_id, boundary_id, owner="client"):
    """Link an already-resolved boundary (from the pin-drop picker, where
    candidates are cached up front) — no geocoding call needed."""
    db = get_db()
    row = db.execute("SELECT id FROM area_boundaries WHERE id=?", (boundary_id,)).fetchone()
    if not row:
        return None
    return _link(db, owner_id, row["id"], owner)


def reverse_candidates(lat, lon):
    """Boundary options covering a dropped-pin point, at several
    administrative granularities (Nominatim's zoom-level trick, plus
    BharatMaps' authoritative state/district polygons) — every candidate is
    cached immediately since we already have its full geometry, so picking
    one is just a cheap link, not another geocoding round-trip."""
    db = get_db()
    seen_refs = set()
    out = []

    for cand in geocoding_service.reverse_boundary_levels(lat, lon):
        source_ref = f"nominatim:{cand['osm_type']}:{cand['osm_id']}"
        if source_ref in seen_refs:
            continue
        seen_refs.add(source_ref)
        cached = _get_cached_boundary(db, source_ref)
        boundary_id = cached["id"] if cached else _cache_boundary(db, source_ref, cand)
        out.append({"boundary_id": boundary_id, "name": cand["name"],
                     "area_type": cand["area_type"], "source": cand["source"],
                     "geometry": cand["geometry"]})

    for cand in geocoding_service.get_bharatmaps_boundaries_at_point(lat, lon):
        source_ref = f"bharatmaps:{cand['area_type']}:{cand['name'].strip().upper()}"
        if source_ref in seen_refs:
            continue
        seen_refs.add(source_ref)
        cached = _get_cached_boundary(db, source_ref)
        boundary_id = cached["id"] if cached else _cache_boundary(db, source_ref, cand)
        out.append({"boundary_id": boundary_id, "name": cand["name"],
                     "area_type": cand["area_type"], "source": cand["source"],
                     "geometry": cand["geometry"]})

    return out


def add_region(owner_id, osm_type, osm_id, name_hint=None, owner="client"):
    """Resolve (from cache, Nominatim, or BharatMaps fallback) and link a
    boundary to this client/lead. Returns the boundary_id, or None if no
    boundary could be found anywhere for this place."""
    boundary_id = resolve_boundary(osm_type, osm_id, name_hint)
    if boundary_id is None:
        return None
    return _link(get_db(), owner_id, boundary_id, owner)


def add_region_by_name(owner_id, place, owner="client"):
    """Link the best boundary for a free-text place name ('Ludhiana',
    'Ghaziabad, UP') — for the API, where nobody is looking at a map. Takes
    the first search result in India (a bare 'Delhi' also matches towns in the
    US), else the first result at all. Returns the linked boundary (name,
    area_type, …) so the caller can say what it matched, or None."""
    cands = search_areas(place)
    if not cands:
        return None
    pick = next((c for c in cands if (c.get("name") or "").endswith("India")), cands[0])
    boundary_id = resolve_boundary(pick.get("osm_type"), pick.get("osm_id"), pick.get("name"))
    if boundary_id is None:
        return None
    _link(get_db(), owner_id, boundary_id, owner)
    return get_boundary(boundary_id)


def get_boundary(boundary_id):
    """One cached boundary with its geometry, or None."""
    row = get_db().execute(
        "SELECT id, name, area_type, source, geometry FROM area_boundaries WHERE id=?", (boundary_id,)
    ).fetchone()
    if not row:
        return None
    return {"boundary_id": row["id"], "name": row["name"], "area_type": row["area_type"],
            "source": row["source"], "geometry": json.loads(row["geometry"])}


def resolve_boundary(osm_type, osm_id, name_hint=None):
    """A search result -> the id of its cached boundary polygon, fetching it
    (Nominatim, then BharatMaps by name) the first time. Links nothing — the
    lead form uses it to preview a place on the map before it's saved.
    None if no boundary exists anywhere for this place."""
    db = get_db()
    boundary_id = None

    if osm_type and osm_id:
        source_ref = f"nominatim:{osm_type}:{osm_id}"
        cached = _get_cached_boundary(db, source_ref)
        if cached:
            boundary_id = cached["id"]
        else:
            boundary = geocoding_service.lookup_boundary_by_osm_id(osm_type, osm_id)
            if boundary:
                boundary_id = _cache_boundary(db, source_ref, boundary)

    if boundary_id is None:
        # Nominatim had no polygon for this place (or wasn't given an osm
        # id at all) — try BharatMaps' state/district data as a fallback.
        bm = geocoding_service.get_bharatmaps_boundary(name_hint)
        if bm:
            bm_ref = f"bharatmaps:{bm['area_type']}:{bm['name'].strip().upper()}"
            cached = _get_cached_boundary(db, bm_ref)
            boundary_id = cached["id"] if cached else _cache_boundary(db, bm_ref, bm)

    return boundary_id


def save_custom_region(owner_id, geometry, name="Custom Area", boundary_id=None, owner="client"):
    """Create a new hand-drawn (brush/erase) area, or update an existing one
    in place when boundary_id is given — a client can have many independent
    custom areas, each with its own editable name. Returns the boundary_id,
    or None if boundary_id was given but isn't a custom area belonging to
    this owner (can't be used to edit someone else's shape, or a named
    place's shared boundary)."""
    db = get_db()
    table, col = _owner(owner)
    if boundary_id:
        owned = db.execute(
            f"""SELECT ab.id FROM area_boundaries ab JOIN {table} r ON r.boundary_id = ab.id
                WHERE ab.id=? AND ab.source='custom' AND r.{col}=?""",
            (boundary_id, owner_id),
        ).fetchone()
        if not owned:
            return None
        db.execute(
            "UPDATE area_boundaries SET geometry=?, name=?, fetched_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(geometry), name, boundary_id),
        )
        db.commit()
        return _link(db, owner_id, boundary_id, owner)

    # New area — source_ref just needs to be unique; custom shapes are never
    # looked up by content the way named places are, only by id from here on.
    boundary_id = _cache_boundary(db, f"custom:{uuid.uuid4()}", {
        "name": name, "area_type": "custom", "geometry": geometry, "source": "custom",
    })
    return _link(db, owner_id, boundary_id, owner)


def get_client_region_shapes(client_ids=None):
    """For the dashboard consolidated map: every client that has at least one
    region, with the list of their region geometries (each client's shapes
    get merged into one combined outline on the frontend via Turf). Uses the
    same per-client geometry_override / shared-boundary resolution as
    get_client_regions. client_ids=None → all clients; an iterable scopes to
    just those (empty → nothing)."""
    if client_ids is not None and not client_ids:
        return []
    where, params = "", []
    if client_ids is not None:
        ph = ",".join("?" * len(client_ids))
        where = f"AND cr.client_id IN ({ph})"
        params = list(client_ids)
    rows = get_db().execute(
        f"""SELECT cr.client_id, c.name AS client_name,
                   COALESCE(cr.geometry_override, ab.geometry) AS geometry
            FROM client_regions cr
            JOIN area_boundaries ab ON ab.id = cr.boundary_id
            JOIN clients c ON c.id = cr.client_id
            WHERE 1=1 {where}
            ORDER BY cr.client_id, ab.name""",
        params,
    ).fetchall()
    by_client = {}
    for r in rows:
        entry = by_client.setdefault(r["client_id"], {
            "client_id": r["client_id"], "name": r["client_name"], "geometries": [],
        })
        entry["geometries"].append(json.loads(r["geometry"]))
    return list(by_client.values())


def _point_in_ring(lon, lat, ring):
    """Ray-casting point-in-polygon test against one linear ring of
    [lon, lat] pairs (GeoJSON coordinate order)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_polygon_coords(lon, lat, poly_coords):
    """GeoJSON Polygon coordinates: [exterior_ring, hole_ring, ...]."""
    if not poly_coords or not _point_in_ring(lon, lat, poly_coords[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in poly_coords[1:])


def _point_in_geometry(lon, lat, geometry):
    """Same rough-accuracy philosophy as the rest of the region feature
    (see geocoding_service's simplification note) — plain ray-casting, no
    geometry library needed for these boundary sizes."""
    if not geometry:
        return False
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        return _point_in_polygon_coords(lon, lat, coords)
    if gtype == "MultiPolygon":
        return any(_point_in_polygon_coords(lon, lat, poly) for poly in coords)
    return False


def find_clients_covering_point(lat, lon):
    """Every client with a saved operating region that contains this point,
    each with the specific matched region(s)' name/area_type (checks each
    region individually rather than the dashboard map's per-client merged
    shape, so the result can say exactly which saved area matched)."""
    rows = get_db().execute(
        """SELECT cr.client_id, c.name AS client_name, ab.name AS region_name,
                  ab.area_type, COALESCE(cr.geometry_override, ab.geometry) AS geometry
           FROM client_regions cr
           JOIN area_boundaries ab ON ab.id = cr.boundary_id
           JOIN clients c ON c.id = cr.client_id"""
    ).fetchall()
    by_client = {}
    for r in rows:
        if not _point_in_geometry(lon, lat, json.loads(r["geometry"])):
            continue
        entry = by_client.setdefault(r["client_id"], {
            "client_id": r["client_id"], "client_name": r["client_name"], "matched_regions": [],
        })
        entry["matched_regions"].append({"name": r["region_name"], "area_type": r["area_type"]})
    return list(by_client.values())


def find_clients_by_place(query):
    """City/state (or any place name) -> clients whose saved operating
    regions cover that point. Geocodes via Ola Maps (same resolver used for
    client addresses). Returns (clients, lat, lon); lat/lon are None if the
    place couldn't be geocoded (no API key, no match, or an API error)."""
    lat, lon = geocoding_service.forward_geocode(query)
    if lat is None or lon is None:
        return [], None, None
    return find_clients_covering_point(lat, lon), lat, lon


def rename_region(owner_id, link_id, name, owner="client"):
    """Rename a custom area. Restricted to custom ones — a named place's
    `name` lives on the shared area_boundaries row, so renaming it would
    rename it for every other client and lead who's also picked that place."""
    db = get_db()
    table, col = _owner(owner)
    row = db.execute(
        f"""SELECT ab.id FROM {table} r JOIN area_boundaries ab ON ab.id = r.boundary_id
            WHERE r.id=? AND r.{col}=? AND ab.source='custom'""",
        (link_id, owner_id),
    ).fetchone()
    if not row:
        return False
    db.execute("UPDATE area_boundaries SET name=? WHERE id=?", (name.strip()[:200], row["id"]))
    db.commit()
    return True


def get_regions(owner_id, owner="client"):
    """Every region linked to this client/lead, with full geometry — one call
    feeds both the region list and the map. Uses this owner's trimmed
    geometry_override (from the erase tool) when present, falling back to
    the shared cached boundary shape otherwise."""
    table, col = _owner(owner)
    rows = get_db().execute(
        f"""SELECT r.id AS link_id, ab.id AS boundary_id, ab.name, ab.area_type,
                   COALESCE(r.geometry_override, ab.geometry) AS geometry, ab.source
            FROM {table} r JOIN area_boundaries ab ON ab.id = r.boundary_id
            WHERE r.{col}=? ORDER BY ab.name""",
        (owner_id,),
    ).fetchall()
    return [
        {
            "link_id": r["link_id"],
            "boundary_id": r["boundary_id"],
            "name": r["name"],
            "area_type": r["area_type"],
            "source": r["source"],
            "geometry": json.loads(r["geometry"]),
        }
        for r in rows
    ]


def get_client_regions(client_id):
    return get_regions(client_id, "client")


def trim_region(owner_id, link_id, geometry, owner="client"):
    """Apply an erase-tool trim to one of this owner's regions.
    Named-place boundaries are shared (area_boundaries is a cross-owner
    cache), so those get a per-owner geometry_override rather than mutating
    the shared row. Custom boundaries are never shared — each belongs to
    exactly one owner — so those are trimmed in place on area_boundaries
    itself; using the override there too would leave a stale shadow copy
    that silently overrides whatever the brush/save flow writes next.
    geometry=None means the erase removed the region entirely, so it's
    unlinked instead of saved as an empty shape."""
    db = get_db()
    table, col = _owner(owner)
    row = db.execute(
        f"""SELECT ab.id, ab.source FROM {table} r JOIN area_boundaries ab ON ab.id = r.boundary_id
            WHERE r.id=? AND r.{col}=?""",
        (link_id, owner_id),
    ).fetchone()
    if not row:
        return False
    if geometry is None:
        return remove_region(owner_id, link_id, owner)
    if row["source"] == "custom":
        db.execute("UPDATE area_boundaries SET geometry=? WHERE id=?", (json.dumps(geometry), row["id"]))
    else:
        db.execute(
            f"UPDATE {table} SET geometry_override=? WHERE id=? AND {col}=?",
            (json.dumps(geometry), link_id, owner_id),
        )
    db.commit()
    return True


def remove_region(owner_id, link_id, owner="client"):
    """Unlink a region from this owner (the cached boundary itself is left
    in place for others / future re-adds)."""
    db = get_db()
    table, col = _owner(owner)
    cur = db.execute(
        f"DELETE FROM {table} WHERE id=? AND {col}=?", (link_id, owner_id)
    )
    db.commit()
    return cur.rowcount > 0


def copy_regions(from_owner, from_id, to_owner, to_id):
    """Give one owner the same areas as another (a lead becoming a client).
    Named places link to the same shared boundary, keeping any trim; custom
    areas belong to exactly one owner, so each gets its own copy — editing
    the client's afterwards must not reshape the lead's."""
    db = get_db()
    src_table, src_col = _owner(from_owner)
    dst_table, dst_col = _owner(to_owner)
    rows = db.execute(
        f"""SELECT r.boundary_id, r.geometry_override, ab.source, ab.name, ab.area_type, ab.geometry
            FROM {src_table} r JOIN area_boundaries ab ON ab.id = r.boundary_id
            WHERE r.{src_col}=?""",
        (from_id,),
    ).fetchall()
    for r in rows:
        boundary_id = r["boundary_id"]
        if r["source"] == "custom":
            boundary_id = _cache_boundary(db, f"custom:{uuid.uuid4()}", {
                "name": r["name"], "area_type": r["area_type"] or "custom",
                "geometry": json.loads(r["geometry"]), "source": "custom",
            })
        db.execute(
            f"INSERT OR IGNORE INTO {dst_table} ({dst_col}, boundary_id, geometry_override) VALUES (?,?,?)",
            (to_id, boundary_id, r["geometry_override"]),
        )
    db.commit()
    return len(rows)

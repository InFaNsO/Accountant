"""Where areas of work overlap — a lead against existing clients and other leads.

Each client's or lead's regions are dissolved into one shape; two shapes
overlap when the shared part is at least MIN_SHARE of the smaller one.
Neighbouring districts share a border and, after the boundary simplification
in geocoding_service, overlap by slivers of up to ~1.5% — real shared
territory (same city, a district inside a state) on the live data is 10%+.

Shapely does the geometry. If it isn't installed the app still runs and
every overlap check just comes back empty (available() says so).
"""
import json
import math

from ..database import get_db

try:
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    _HAVE_SHAPELY = True
except ImportError:   # pragma: no cover — deploy installs it from requirements.txt
    _HAVE_SHAPELY = False

MIN_SHARE = 0.05


def available():
    return _HAVE_SHAPELY


def _parse(geometry_json):
    try:
        geom = shape(json.loads(geometry_json))
    except Exception:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    return None if geom.is_empty else geom


def _load():
    """Every client and lead that has at least one region, keyed (kind, id),
    plus a spatial index over them. Not cached across calls: the assistant
    runs several tools in one request, and an area changed by one must be
    seen by the next — callers checking many leads share one load instead
    (overlaps_for_many)."""
    db = get_db()
    rows = [("client",) + tuple(r) for r in db.execute(
        """SELECT cr.client_id, c.name, NULL, NULL, ab.name,
                  COALESCE(cr.geometry_override, ab.geometry)
           FROM client_regions cr
           JOIN area_boundaries ab ON ab.id = cr.boundary_id
           JOIN clients c ON c.id = cr.client_id""")]
    rows += [("lead",) + tuple(r) for r in db.execute(
        """SELECT lr.lead_id, l.company_name, l.status, l.client_id, ab.name,
                  COALESCE(lr.geometry_override, ab.geometry)
           FROM lead_regions lr
           JOIN area_boundaries ab ON ab.id = lr.boundary_id
           JOIN leads l ON l.id = lr.lead_id""")]
    entities = {}
    for kind, eid, name, status, client_id, region_name, geometry in rows:
        geom = _parse(geometry)
        if geom is None:
            continue
        e = entities.setdefault((kind, eid), {
            "kind": kind, "id": eid, "name": name, "status": status,
            "client_id": client_id, "regions": [],
        })
        e["regions"].append((region_name, geom))
    for e in entities.values():
        e["shape"] = unary_union([geom for _, geom in e["regions"]])
    keys = list(entities)
    tree = STRtree([entities[k]["shape"] for k in keys]) if keys else None
    return entities, keys, tree


def _km2(geom):
    """Rough area in km² from lon/lat degrees — good enough to say 'about
    3,400 km²', not for surveying."""
    lat = math.radians(geom.centroid.y)
    return geom.area * 111.32 * math.cos(lat) * 110.57


def _overlapping_names(regions, other_shape):
    names = []
    for name, geom in regions:
        if not geom.intersects(other_shape):
            continue
        shared = geom.intersection(other_shape).area
        if shared >= MIN_SHARE * min(geom.area, other_shape.area):
            names.append(name)
    return names


def overlaps_for(kind, entity_id, with_geometry=False, loaded=None):
    """Clients and leads whose area overlaps this one, biggest share first.

    Each item: kind, id, name, status (leads), share_here (fraction of THIS
    area that is shared), share_there (fraction of theirs), shared_km2,
    their_regions / our_regions (region names involved) and, with
    with_geometry, their whole area as GeoJSON for drawing.

    A lead and the client it was converted into are the same business, so
    they are never reported against each other.
    """
    if not _HAVE_SHAPELY:
        return []
    entities, keys, tree = loaded or _load()
    me = entities.get((kind, entity_id))
    if me is None or tree is None:
        return []
    skip = {(kind, entity_id)}
    if kind == "lead" and me["client_id"]:
        skip.add(("client", me["client_id"]))
    if kind == "client":
        skip |= {k for k, e in entities.items() if e["kind"] == "lead" and e["client_id"] == entity_id}

    mine = me["shape"]
    out = []
    for idx in tree.query(mine):
        key = keys[int(idx)]
        if key in skip:
            continue
        other = entities[key]
        theirs = other["shape"]
        if not mine.intersects(theirs):
            continue
        shared = mine.intersection(theirs)
        if shared.is_empty or shared.area < MIN_SHARE * min(mine.area, theirs.area):
            continue
        item = {
            "kind": other["kind"],
            "id": other["id"],
            "name": other["name"],
            "status": other["status"],
            "share_here": round(shared.area / mine.area, 4) if mine.area else 0,
            "share_there": round(shared.area / theirs.area, 4) if theirs.area else 0,
            "shared_km2": round(_km2(shared)),
            "their_regions": _overlapping_names(other["regions"], mine),
            "our_regions": _overlapping_names(me["regions"], theirs),
        }
        if with_geometry:
            item["geometry"] = mapping(theirs.simplify(0.002, preserve_topology=True))
        out.append(item)
    out.sort(key=lambda o: (-o["share_here"], o["kind"], (o["name"] or "").lower()))
    return out


def overlaps_for_many(kind, entity_ids, with_geometry=False):
    """{id: overlaps_for(...)} for many records, loading the areas once."""
    if not _HAVE_SHAPELY:
        return {i: [] for i in entity_ids}
    loaded = _load()
    return {i: overlaps_for(kind, i, with_geometry, loaded) for i in entity_ids}


def lead_overlap_summary(lead_ids):
    """{lead_id: {"clients": [names], "leads": [names]}} for the leads list —
    only leads that overlap someone appear."""
    summary = {}
    for lead_id, found in overlaps_for_many("lead", lead_ids).items():
        if found:
            summary[lead_id] = {
                "clients": [o["name"] for o in found if o["kind"] == "client"],
                "leads":   [o["name"] for o in found if o["kind"] == "lead"],
            }
    return summary

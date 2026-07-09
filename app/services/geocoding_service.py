"""Ola Maps geocoding: resolves a place name/address to lat/lng.

Used to auto-locate clients (city/state) for the visits map's coverage pins.
Fails soft — callers get (None, None) on any error so a geocoding outage
never blocks saving a client.
"""
import os
import requests

_BASE_URL = "https://api.olamaps.io/places/v1/geocode"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_LOOKUP_URL = "https://nominatim.openstreetmap.org/lookup"
_NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
_BHARATMAPS_URL = ("https://mapservice.gov.in/gismapservice/rest/services/"
                    "BharatMapService/Admin_Boundary_District/MapServer")
_TIMEOUT = 5
_UA = {"User-Agent": "Ledger-VisitsMap/1.0"}
# Nominatim place classes worth offering as a "region" — excludes roads, shops, etc.
_PLACE_CLASSES = {"place", "boundary"}
# Geometry simplification tolerance (~1.1km at Indian latitudes). Region
# outlines only need to convey a rough coverage area, not survey-grade
# accuracy — without this, a state/district's full coastline detail can
# balloon a single response past 10MB (confirmed: Maharashtra's un-simplified
# boundary is ~1.3MB from Nominatim / 2.7MB from BharatMaps; simplified,
# both drop to ~20-30KB with no visible loss at map scale).
_SIMPLIFY_DEGREES = 0.01


def forward_geocode(query):
    """Resolve a free-text place/address to (lat, lng), or (None, None) if it
    can't be resolved (no API key, no match, network/API error)."""
    api_key = os.environ.get("OLA_MAPS_API_KEY")
    if not api_key or not (query or "").strip():
        return None, None
    try:
        resp = requests.get(
            _BASE_URL,
            params={"address": query.strip(), "api_key": api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("geocodingResults") or []
        if not results:
            return None, None
        location = results[0].get("geometry", {}).get("location", {})
        return location.get("lat"), location.get("lng")
    except (requests.RequestException, ValueError, KeyError):
        return None, None


_POLYGON_TYPES = {"Polygon", "MultiPolygon"}


def _query_boundary(query):
    """Single Nominatim lookup. Returns the boundary dict or None."""
    resp = requests.get(
        _NOMINATIM_URL,
        params={
            "q": query,
            "format": "json",
            "polygon_geojson": 1,
            "polygon_threshold": _SIMPLIFY_DEGREES,
            "addressdetails": 1,
            "limit": 10,
        },
        headers={"User-Agent": "Ledger-VisitsMap/1.0"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json()
    best = next((r for r in results if r.get("geojson", {}).get("type") in _POLYGON_TYPES), None)
    if not best:
        return None
    return {
        "name": best.get("display_name", query),
        "geometry": best["geojson"],  # native GeoJSON, [lng,lat] order, Polygon or MultiPolygon
        "source": "nominatim",
    }


def get_area_boundary(query):
    """Look up an administrative/locality boundary polygon for a place name
    (e.g. a suburb or town) via OSM Nominatim — Ola Maps' geocoder only
    returns points, not outlines. Returns {"name": str, "geometry": <GeoJSON
    Polygon|MultiPolygon>, "source": "nominatim"} for the best-matching
    result, or None if nothing resolves.

    Adding city/state to the query (e.g. "Preet Vihar, New Delhi") often
    pulls in roads/POIs that outrank the actual locality boundary in
    Nominatim's top results, so if the full query comes up empty we retry
    with just its first comma-separated segment (the locality name alone).
    """
    query = (query or "").strip()
    if not query:
        return None
    try:
        result = _query_boundary(query)
        if result:
            return result
        first_segment = query.split(",")[0].strip()
        if first_segment and first_segment != query:
            return _query_boundary(first_segment)
        return None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def search_places(query, limit=8):
    """Live search-as-you-type candidates for the client-region picker.
    Lightweight — no polygon fetched yet, just enough to list & disambiguate.
    Returns [{"osm_type", "osm_id", "name", "type", "lat", "lon"}, ...]."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "json", "addressdetails": 1, "limit": limit},
            headers=_UA, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for r in results:
        if r.get("class") not in _PLACE_CLASSES:
            continue
        try:
            out.append({
                "osm_type": r["osm_type"],
                "osm_id": r["osm_id"],
                "name": r.get("display_name", query),
                "type": r.get("type", ""),
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
            })
        except (KeyError, ValueError):
            continue
    return out


_OSM_TYPE_PREFIX = {"node": "N", "way": "W", "relation": "R"}


def lookup_boundary_by_osm_id(osm_type, osm_id):
    """Fetch the polygon for one specific, already-identified Nominatim place
    (avoids re-ranking ambiguity from a fresh text search). Returns the same
    shape as get_area_boundary(), or None if that place has no polygon
    (e.g. it's a POI, not an administrative area)."""
    prefix = _OSM_TYPE_PREFIX.get(osm_type)
    if not prefix or not osm_id:
        return None
    try:
        resp = requests.get(
            _NOMINATIM_LOOKUP_URL,
            params={"osm_ids": f"{prefix}{osm_id}", "format": "json", "polygon_geojson": 1,
                    "polygon_threshold": _SIMPLIFY_DEGREES},
            headers=_UA, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
        best = next((r for r in results if r.get("geojson", {}).get("type") in _POLYGON_TYPES), None)
        if not best:
            return None
        return {
            "name": best.get("display_name", ""),
            "area_type": best.get("type", ""),
            "geometry": best["geojson"],
            "source": "nominatim",
        }
    except (requests.RequestException, ValueError, KeyError):
        return None


def _esc_sql_literal(s):
    """Escape a value for interpolation into an ArcGIS/SQL-92-style `where`
    clause (single-quote doubling) — BharatMaps' REST query has no
    parameterized-query option, so this is the only injection guard."""
    return (s or "").replace("'", "''")


# Nominatim reverse-geocode "zoom" controls which admin level is returned
# for a given point — querying the same point at several zoom values surfaces
# candidates at increasing granularity (state -> district -> city -> town ->
# suburb) for the map's "drop a pin" region picker.
_REVERSE_ZOOM_LEVELS = [5, 8, 10, 12, 14]


def reverse_boundary_levels(lat, lon):
    """Boundary candidates covering (lat, lon) at several administrative
    granularities. Returns a de-duplicated list of the same shape as
    get_area_boundary() (plus area_type), one entry per distinct place —
    adjacent zoom levels often resolve to the same feature (e.g. a small
    state), which are collapsed to a single candidate."""
    candidates = []
    seen = set()
    for zoom in _REVERSE_ZOOM_LEVELS:
        try:
            resp = requests.get(
                _NOMINATIM_REVERSE_URL,
                params={"lat": lat, "lon": lon, "format": "json", "zoom": zoom,
                        "polygon_geojson": 1, "polygon_threshold": _SIMPLIFY_DEGREES,
                        "addressdetails": 1},
                headers=_UA, timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            r = resp.json()
        except (requests.RequestException, ValueError):
            continue
        if not r or "osm_id" not in r:
            continue
        key = (r.get("osm_type"), r.get("osm_id"))
        if key in seen:
            continue
        geojson = r.get("geojson") or {}
        if geojson.get("type") not in _POLYGON_TYPES:
            continue
        seen.add(key)
        candidates.append({
            "osm_type": r["osm_type"],
            "osm_id": r["osm_id"],
            "name": r.get("display_name", ""),
            "area_type": r.get("addresstype") or r.get("type", ""),
            "geometry": geojson,
            "source": "nominatim",
        })
    return candidates


def get_bharatmaps_boundaries_at_point(lat, lon):
    """State + district BharatMaps candidates whose polygon contains this
    point (point-in-polygon spatial query) — extra options for the pin-drop
    picker alongside Nominatim's, since BharatMaps is the more authoritative
    source at these two levels."""
    candidates = []
    for layer, field, area_type in ((1, "dtname", "district"), (0, "STNAME", "state")):
        try:
            resp = requests.get(
                f"{_BHARATMAPS_URL}/{layer}/query",
                params={
                    "geometry": f"{lon},{lat}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "*",
                    "f": "geojson",
                    "resultRecordCount": 1,
                    "maxAllowableOffset": _SIMPLIFY_DEGREES,
                    "geometryPrecision": 4,
                },
                timeout=8,
            )
            resp.raise_for_status()
            features = resp.json().get("features") or []
            if not features:
                continue
            geometry, props = features[0]["geometry"], features[0]["properties"]
            candidates.append({
                "name": props.get(field) or area_type,
                "area_type": area_type,
                "geometry": geometry,
                "source": "bharatmaps",
            })
        except (requests.RequestException, ValueError, KeyError, IndexError):
            continue
    return candidates


def get_bharatmaps_boundary(name):
    """State/district boundary fallback via the free, unauthenticated
    BharatMaps (mapservice.gov.in) REST endpoint — used when Nominatim has
    no polygon for a place (common for Indian districts/states, which
    BharatMaps' official Survey-of-India-derived data covers well; it does
    NOT cover city/town/village level, only these two layers). Tries
    district first (more specific), then state. Returns the same shape as
    get_area_boundary(), or None if neither layer matches."""
    name = (name or "").strip()
    if not name:
        return None
    escaped = _esc_sql_literal(name)
    for layer, field, area_type in ((1, "dtname", "district"), (0, "STNAME", "state")):
        try:
            resp = requests.get(
                f"{_BHARATMAPS_URL}/{layer}/query",
                params={
                    "where": f"UPPER({field})=UPPER('{escaped}')",
                    "outFields": "*",
                    "f": "geojson",
                    "resultRecordCount": 1,
                    "maxAllowableOffset": _SIMPLIFY_DEGREES,
                    "geometryPrecision": 4,
                },
                timeout=8,
            )
            resp.raise_for_status()
            features = resp.json().get("features") or []
            if not features:
                continue
            geometry, props = features[0]["geometry"], features[0]["properties"]
            return {
                "name": props.get(field) or name,
                "area_type": area_type,
                "geometry": geometry,
                "source": "bharatmaps",
            }
        except (requests.RequestException, ValueError, KeyError, IndexError):
            continue
    return None

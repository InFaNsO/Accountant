"""Eco ⇄ Normal stock corrections.

Re-classifies stock between a main (Normal) product and its Eco twin without
changing the total on hand. Per grit/sub-product you pick a direction and a
quantity; whatever leaves the Normal side must be matched by what leaves the
Eco side, so both column totals stay equal:

        NORMAL                    ECO
    Grit 60    5  ──────►
    Grit 100      ◄──────          5
    ─────────────────────────────────
    Total      5      =            5

Unlike a stock tally (which changes stock to match a physical count), a
correction only moves quantity sideways — the warehouse total is untouched.
Nothing is stored as a draft: the popup applies straight to stock, and the
audit trail is the pair of stock_movements rows each transfer writes.
"""

from ..database import get_db


# bucket key → (qty column, label, movement_type prefix)
BUCKETS = {
    "warehouse":  ("stock_qty",      "Warehouse",  "correction"),
    "production": ("production_qty", "Production", "production_correction"),
    "dispatch":   ("in_transit_qty", "In Transit", "dispatch_correction"),
}

_EPS = 0.0001


def bucket_label(bucket):
    return BUCKETS.get(bucket, BUCKETS["warehouse"])[1]


def get_eco_pairs(product_id):
    """Pair each active main sub-product with its eco twin, carrying all three
    bucket quantities for both sides so the popup can switch bucket without a
    round trip.

    Returns (eco_product, pairs, error). A main sub with no eco twin is still
    listed (so the sheet shows the whole range) but is marked unpaired.
    """
    db   = get_db()
    main = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not main:
        return None, [], "Product not found."

    eco = db.execute(
        "SELECT * FROM products WHERE eco_parent_id=? AND is_active=1", (product_id,)
    ).fetchone()
    if not eco:
        return None, [], "This product has no eco range."

    def buckets_of(row):
        return {
            "warehouse":  row["stock_qty"]      or 0,
            "production": row["production_qty"] or 0,
            "dispatch":   row["in_transit_qty"] or 0,
        }

    main_subs = db.execute(
        "SELECT * FROM sub_products WHERE product_id=? AND is_active=1 "
        "ORDER BY COALESCE(NULLIF(sku,''), name)",
        (product_id,),
    ).fetchall()

    pairs = []
    if main_subs:
        eco_subs  = db.execute(
            "SELECT * FROM sub_products WHERE product_id=? AND is_active=1", (eco["id"],)
        ).fetchall()
        by_parent = {s["eco_parent_sub_id"]: s for s in eco_subs if s["eco_parent_sub_id"]}

        for ms in main_subs:
            es = by_parent.get(ms["id"])
            pairs.append({
                "main_sub_id": ms["id"],
                "eco_sub_id":  es["id"] if es else None,
                "name":        ms["name"],
                "sku":         ms["sku"] or "",
                "pcs":         ms["pcs_per_carton"] or main["pcs_per_carton"] or 0,
                "paired":      es is not None,
                "main":        buckets_of(ms),
                "eco":         buckets_of(es) if es else {"warehouse": 0, "production": 0, "dispatch": 0},
            })
    else:
        # Leaf product — the pair is the two product rows themselves.
        pairs.append({
            "main_sub_id": None,
            "eco_sub_id":  None,
            "name":        main["name"],
            "sku":         main["sku"] or "",
            "pcs":         main["pcs_per_carton"] or 0,
            "paired":      True,
            "main":        buckets_of(main),
            "eco":         buckets_of(eco),
        })

    return dict(eco), pairs, None


def allows_unbalanced(bucket):
    """Only production may go out of balance. Warehouse and in-transit stock is
    physically counted, so a one-sided move there is a miscount, not a re-grade."""
    return bucket == "production"


def apply_correction(product_id, bucket, moves, allow_unbalanced=False):
    """Validate the whole correction, then move stock in a single transaction.

    `moves` is a list of {"main_sub_id": int|None, "direction": "to_eco"|"to_main",
    "qty": float}. Nothing is written unless every check passes — a half-applied
    correction would break the very invariant this exists to protect.

    `allow_unbalanced` lets a PRODUCTION correction skip the balance rule (a run
    can be re-graded one way mid-production). It is ignored for every other
    bucket, so the UI checkbox alone cannot unbalance warehouse stock.

    Returns (ok, error, moved_count).
    """
    if bucket not in BUCKETS:
        return False, "Unknown stock bucket.", 0
    unbalanced_ok = bool(allow_unbalanced) and allows_unbalanced(bucket)

    db = get_db()
    qty_col, label, mv_prefix = BUCKETS[bucket]

    eco, pairs, err = get_eco_pairs(product_id)
    if err:
        return False, err, 0
    by_sub = {p["main_sub_id"]: p for p in pairs}

    clean = [m for m in moves
             if m.get("direction") in ("to_eco", "to_main") and (m.get("qty") or 0) > 0]
    if not clean:
        return False, "Nothing to move — enter a quantity and direction on at least one row.", 0

    out_normal  = sum(m["qty"] for m in clean if m["direction"] == "to_eco")
    out_eco     = sum(m["qty"] for m in clean if m["direction"] == "to_main")
    is_unbalanced = abs(out_normal - out_eco) >= _EPS
    if is_unbalanced and not unbalanced_ok:
        return False, (f"Not balanced — {_fmt(out_normal)} leaving Normal vs {_fmt(out_eco)} "
                       f"leaving Eco. The two totals must match."), 0

    # Resolve every leg and check live stock BEFORE touching anything.
    planned = []
    for m in clean:
        pair = by_sub.get(m.get("main_sub_id"))
        if not pair:
            return False, "A row no longer matches this product — reload and try again.", 0
        if not pair["paired"]:
            return False, (f"{pair['name']} has no eco counterpart — create the eco "
                           f"sub-product first."), 0

        if pair["main_sub_id"]:
            main_leg = ("sub_products", pair["main_sub_id"], product_id,  pair["main_sub_id"])
            eco_leg  = ("sub_products", pair["eco_sub_id"],  eco["id"],   pair["eco_sub_id"])
        else:
            main_leg = ("products", product_id, product_id, None)
            eco_leg  = ("products", eco["id"],  eco["id"],  None)

        src, dst = (main_leg, eco_leg) if m["direction"] == "to_eco" else (eco_leg, main_leg)

        live = db.execute(
            f"SELECT {qty_col} AS q, name FROM {src[0]} WHERE id=?", (src[1],)
        ).fetchone()
        if not live:
            return False, "A product row referenced by this correction no longer exists.", 0
        if (live["q"] or 0) + _EPS < m["qty"]:
            return False, (f"{live['name']} only has {_fmt(live['q'] or 0)} in {label} — "
                           f"cannot move {_fmt(m['qty'])}."), 0
        planned.append((m, src, dst, pair))

    for m, src, dst, pair in planned:
        arrow = "Normal → Eco" if m["direction"] == "to_eco" else "Eco → Normal"
        note  = f"Eco correction · {pair['name']} · {arrow} ({label})"
        if is_unbalanced:
            # The movement notes are the only record — say so plainly.
            note += " · unbalanced, acknowledged"

        db.execute(f"UPDATE {src[0]} SET {qty_col}={qty_col}-? WHERE id=?", (m["qty"], src[1]))
        db.execute(f"UPDATE {dst[0]} SET {qty_col}={qty_col}+? WHERE id=?", (m["qty"], dst[1]))

        out_id = db.execute(
            """INSERT INTO stock_movements (product_id, sub_product_id, movement_type, quantity, notes)
               VALUES (?,?,?,?,?)""",
            (src[2], src[3], f"{mv_prefix}_out", m["qty"], note),
        ).lastrowid
        in_id = db.execute(
            """INSERT INTO stock_movements
                   (product_id, sub_product_id, movement_type, quantity, notes, linked_movement_id)
               VALUES (?,?,?,?,?,?)""",
            (dst[2], dst[3], f"{mv_prefix}_in", m["qty"], note, out_id),
        ).lastrowid
        db.execute("UPDATE stock_movements SET linked_movement_id=? WHERE id=?", (in_id, out_id))

    db.commit()
    return True, None, len(planned)


def parse_moves(form_data):
    """Pull the popup's `dir_<key>` / `qty_<key>` fields into move dicts.
    The key is the main sub-product id, or the literal 'p' for a leaf product."""
    moves = []
    for field, raw in form_data.items():
        if not field.startswith("qty_"):
            continue
        key = field[4:]
        try:
            qty = float((raw or "").strip().replace(",", "") or 0)
        except ValueError:
            continue
        direction = (form_data.get(f"dir_{key}") or "").strip()
        if qty <= 0 or direction not in ("to_eco", "to_main"):
            continue
        moves.append({
            "main_sub_id": None if key == "p" else int(key),
            "direction":   direction,
            "qty":         qty,
        })
    return moves


def _fmt(v):
    v = v or 0
    return str(int(v)) if abs(v - int(v)) < _EPS else f"{v:.2f}"

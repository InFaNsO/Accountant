# Accountant — Project Notes for Claude

## Stack
- Flask app (`app/` package, `app:create_app` factory).
- SQLite database at `data/ledger.db` (gitignored). `accountant.db` at repo root is a legacy/leftover and not in active use.
- Templates in `app/templates/`, services in `app/services/`, routes in `app/routes/`.

## Running the dev server
Use the preview tool with `.claude/launch.json` (configured for `flask-app` on port **6879**):
- `mcp__Claude_Preview__preview_start({ name: "flask-app" })`
- URL: `http://localhost:6879`

Notes:
- The `port` in launch.json is informational; the actual port is passed to Flask via `runtimeArgs: ["--port=6879"]`. Keep them in sync.
- The scheduler warning "No module named 'apscheduler'" in logs is benign — unrelated optional feature.

## Production server (SSH)
A passwordless SSH shortcut is already set up:

```
ssh ledger-prod
```

- Host: `143.244.128.26`
- User: `root`
- Hostname (remote): `Admin-Apples-Tree`
- Key: `~/.ssh/id_ed25519` (configured in `~/.ssh/config` under `Host ledger-prod`)

### Pulling the production DB to local
Live DB path on the server: `/home/ledger/app/data/ledger.db`.

The server does **not** have `sqlite3` CLI installed — use Python's `sqlite3` module for the snapshot. Snapshot first so you don't grab a half-written file:

```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$py = "import sqlite3; s=sqlite3.connect('/home/ledger/app/data/ledger.db'); d=sqlite3.connect('/tmp/ledger.snapshot.db'); s.backup(d); d.close(); s.close(); print('snapshot ok')"
$py | ssh ledger-prod "python3 -"
Copy-Item D:\Code\Claude\Accountant\data\ledger.db "D:\Code\Claude\Accountant\data\ledger.db.bak-$ts"
scp ledger-prod:/tmp/ledger.snapshot.db D:\Code\Claude\Accountant\data\ledger.db
ssh ledger-prod "rm /tmp/ledger.snapshot.db"
```
Restart the Flask preview server afterwards so SQLite reopens the new file.

## Database schema
Schema and additive column migrations live in `app/database.py` — see `_create_schema()` and the trailing `_add_column(...)` calls. To add a new column to an existing table, append an `_add_column(db, "table", "col", "TYPE DEFAULT …")` line; it's idempotent.

Key tables: `clients`, `client_companies`, `products`, `sub_products`, `invoices`, `invoice_items`, `payments`, `stock_movements`, `purchase_orders`, `dispatches`, `users`, `stock_tallies`.

## Palm Purchase domain
"Palm Purchase" is the **instant warehouse stock-in** flow — direct cash/spot buys land straight in `stock_qty` (unlike production POs which sit in `production_qty` until dispatched and arrived).

- Tables: `palm_purchases`, `palm_purchase_items` (`app/database.py`).
- Service: `app/services/palm_purchase_service.py` — `create_palm_purchase()` increments `stock_qty` and logs `stock_movements` with `movement_type = 'palm_purchase'`. Deletion reverses via `'palm_purchase_reversed'`.
- Route: `app/routes/palm_purchase.py`, mounted at `/palm-purchase/`. Permission module: `palm_purchase`.
- Templates: `app/templates/palm_purchase/{list,form,detail}.html`.
- Nav entry appears after Transit in `base.html` (only if the user has `palm_purchase:view`).
- API (used by MCP): `GET/POST /api/palm-purchases`, `GET/DELETE /api/palm-purchases/<id>`.

## Stock history API
`GET /api/products/<product_id>/stock-history` — chronological `stock_movements` for a product. Query params:
- `sub_product_id` (optional) — restrict to a variant.
- `bucket` (optional) — `warehouse`, `production`, or `transit`; filters by the movement_types that belong to that bucket (see `_BUCKET_MOVEMENT_TYPES` in `app/routes/api.py`).
- `limit` (default 100, max 500).

Response also includes the current warehouse/production/transit levels for quick context.

## MCP server (`mcp_server.py`)
HTTP proxy to the Flask `/api/*` endpoints. Auth via `X-MCP-Key` (`MCP_API_KEY` env var on the server). Pattern: `@mcp.tool()` decorator on each function; body is built with `_call(method, path, params=, body=)`.

Recently added tools:
- `list_palm_purchases`, `get_palm_purchase`, `create_palm_purchase`, `delete_palm_purchase`
- `get_product_stock_history(product_id, sub_product_id?, bucket?, limit?)`

To add a new MCP tool: write the matching Flask `/api/...` endpoint with `@require_auth`, then add the `@mcp.tool()` wrapper in `mcp_server.py` that calls `_call(...)`.

## Invoice domain — recent changes
- **Discounts:** both invoice-level and per-line-item carry `discount_type` (`'value'` or `'percent'`) + `discount_value`. The resolved ₹ figure is stored in `invoices.discount_amount` for backward compatibility. See `_compute_totals()` and `_item_net()` in `app/services/invoice_service.py`.
- **Drafts** never deduct stock and may be saved with qty > stock. The "Issue Invoice" button on the detail page is server-gated AND disabled in the UI when `stock_status` reports shortages.
- **Stock refresh** endpoint: `GET /invoices/api/stock-refresh` returns current `stock_qty` for all active products + sub-products. The form's "Refresh Stock" button uses it.
- **Pcs/Box toggle** on the invoice detail line items uses each product's `pcs_per_carton` (sub-product preferred, falls back to parent). Items without a configured `pcs_per_carton` stay in pieces when the user toggles to boxes.

## Conventions
- All routes use `@login_required` + `@permission_required("module", "action")`.
- SQL goes through `app.database.get_db()` which returns a per-request `sqlite3.Connection` (row_factory=Row).
- Templates extend `base.html`; use the `inr` / `indian` Jinja filters for currency / Indian-style number formatting.
- API endpoints under `/invoices/api/...` should return JSON via `jsonify`.

## Things to know about the working tree
- `accountant.db` (root) is staged-deleted but the deletion isn't committed yet. It's a leftover from when the DB was moved into `data/ledger.db`.
- `data/` is gitignored; the live local DB and its `.bak` snapshots live there.
- `.claude/worktrees/` contains agent-isolated copies of the repo — ignore them, they're not the working tree.

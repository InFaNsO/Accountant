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

## Draft mode (production orders / dispatches / palm purchases)
Each of these three can be **saved as a draft** (no stock side-effects) and later **activated** (which applies them). Mirrors the invoice draft pattern: draft = nothing moves until you commit.

- **Status values:** production `draft → open` (`closed` later); dispatch `draft → in_transit` (`partially_received`/`received` later); palm `draft → active`. Palm gained a `palm_purchases.status` column (`_add_column`, default `'active'` so historical rows stay applied).
- **Create** services branch on `data.get("status") == "draft"`: a draft inserts the header + items but skips the stock work — production skips `production_qty` bumps, dispatch skips FIFO/`production→in_transit`/movements, palm skips `stock_qty` bumps/movements.
- **Activate** functions (`activate_production_order`, `activate_dispatch`, `activate_palm_purchase`) apply the deferred stock work and flip status; they no-op (return False / `(False, errors, [])`) if the row isn't a draft. **`activate_dispatch` re-checks production availability** and refuses (returns errors) if any line exceeds current `production_qty` — the same gate the create route applies. Dispatch's per-item stock work is factored into `_apply_dispatch_item_stock(...)`, shared by create + activate.
- **Delete** is draft-safe: a draft never moved stock, so `delete_*` skips the reversal when status is `draft`.
- **Edit** (production only) skips `production_qty` deltas while the PO is a draft.
- **Routes:** `POST /production/<id>/activate`, `POST /transit/<id>/activate`, `POST /palm-purchase/<id>/activate` — all gated on the module's `edit` permission. The transit create route skips its qty-vs-production validation for drafts (re-checked on activate).
- **UI:** every form has a secondary **Save as Draft** submit button (`name="status" value="draft"` vs the primary active value). Detail pages show a draft banner + **Activate** button and hide stock actions (transit hides the Receive UI for drafts). List pages carry a **Draft** filter chip and badge (palm list gained the full tick/cross chip system + an inline Activate button).

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
- `reconcile_client_payments(client_id?)` — applies unallocated payments to oldest unpaid invoices (one client, or all if omitted). Endpoint: `POST /api/clients/reconcile`. Backed by `payment_service.reconcile_all_clients()` / `recalculate_client_balance()`. The "Reconcile Payments" button on the clients list page (`POST /clients/reconcile-all`) uses the same service.
- `get_company_ledger(client_id, company_id, date_from?, date_to?)` — structured ledger for ONE company under a client. Thin wrapper over `GET /api/clients/<id>/ledger?company_id=&format=json` (which already supports `company_id`).
- `get_client_full(client_id, invoice_days=30, invoice_limit=500)` — **everything about a client in one call**: details, invoices (with line items incl. box size + `quantity_boxes`, prices, discounts, and per-invoice payment status), companies (each with its own ledger), and the complete client ledger JSON. `invoice_days=-1` = all invoices. Endpoint: `GET /api/clients/<id>/full`. Helper `_ledger_payload()` in `api.py` builds the ledgers.
- `get_category_products(category_id, include_inactive=False)` — all products in a category with nested sub-products, each showing box size, bucket quantities (warehouse/production/transit) and min_quantity. Endpoint: `GET /api/categories/<id>/products`.
- `get_sub_product_stock_history(product_id, sub_product_id, bucket='warehouse', limit=100)` — history for ONE sub-product (defaults to warehouse; `bucket=production|transit|all`). Reuses `GET /api/products/<id>/stock-history`.
- `get_product_stock_history_by_sub(product_id, bucket='warehouse', limit=50)` — history for ALL sub-products of a product, grouped per sub-product; products with no sub-products return one product-level group. Endpoint: `GET /api/products/<id>/stock-history-grouped`.
- `product_stock_action(product_id, action, quantity, sub_product_id?, expected_arrival?, notes?)` — semantic stock **moves** (`add_stock`, `send_to_production`, `dispatch_from_production` [needs `expected_arrival`], `mark_arrived`). Endpoint: `POST /api/products/<id>/stock-action`, dispatches to the matching `product_service` mover. (Raw single-bucket +/- stays `adjust_stock`.)
- `get_received_transit(date_from?, date_to?, include?, limit?)` / `get_upcoming_transit(...)` — dispatches that have arrived vs still arriving. No date range → single latest / next; with a range → all in window. Filters/orders by `expected_arrival`. Endpoints: `GET /api/transit/received`, `GET /api/transit/upcoming` (`include=items`).
- `set_payment_opening_balance(payment_id, is_opening_balance=True)` — mark/unmark a payment as **opening-balance** (see below); re-reconciles the client. Endpoint: `POST /api/payments/<id>/opening-balance`. `record_payment` also takes `is_opening_balance`.

### Opening-balance payments (`payments.is_opening_balance`)
A payment flagged `is_opening_balance=1` is **assigned to the client's old/opening balance only** — it is never allocated to invoices and is **skipped entirely by reconciliation**. This prevents a large "for the old balance" payment from leaking onto invoices and wrongly marking them paid.
- Allocation engine (`payment_service.py`): `recalculate_client_balance` computes `flagged_sum` (sum of flagged payments), sets `ob_budget = max(0, opening_balance_debt − flagged_sum)`, and `continue`s past flagged payments in the allocation loop. `create_payment` inserts flagged payments with **no** allocations.
- Set on existing payments via `set_payment_opening_balance(payment_id, flag)` (re-reconciles the client).
- UI: checkbox on the payment form; **Mark OB / Unmark OB** button + badge on the payments list (`POST /payments/<id>/toggle-opening-balance`). Ledger entries show a `(opening balance)` label + `is_opening_balance` field.

To add a new MCP tool: write the matching Flask `/api/...` endpoint with `@require_auth`, then add the `@mcp.tool()` wrapper in `mcp_server.py` that calls `_call(...)`.

## Bulk MCP endpoints (for multi-entity analysis)
A family of structured-JSON endpoints lets the LLM answer cross-entity questions in 1–3 calls instead of looping single-id tools. **Reach for these first** when planning purchase orders, reviewing collections, or analyzing sales across many products / clients.

Shape: every endpoint returns `{"result": {"items": [...], "count": N, "truncated": bool, "limit": L}}`. When `truncated=true`, raise the limit or tighten filters.

Conventions:
- **Filter via query params.** Most accept `category_id`, `product_ids` / `sub_product_ids` (CSV), `client_id`, `supplier_id`, `status` (CSV), `date_from` / `date_to` (`YYYY-MM-DD`).
- **`include=` CSV** expands optional fields. Default omits expensive joins to keep payloads small.
- **Hard caps.** Each endpoint has a default + max `limit`. Page by narrowing filters; cursor pagination is not implemented (see plan file in `~/.claude/plans/` for follow-up).

| MCP tool | Endpoint | Use for |
|---|---|---|
| `products_snapshot` | `/api/products/snapshot` | Per-SKU stock + optional `velocity`, `last_purchase`. **Marquee tool for purchase planning.** |
| `products_stock_history_bulk` | `/api/products/stock-history-bulk` | Stock movements across many SKUs in one call. |
| `products_sales_velocity` | `/api/products/sales-velocity` | Qty sold + revenue per product over a date range. |
| `invoices_bulk` | `/api/invoices/bulk` | Many invoices + optional `items`, `payments`. |
| `payments_bulk` | `/api/payments/bulk` | Many payments + optional `allocations`. |
| `clients_outstanding` | `/api/clients/outstanding` | Aged-bucket outstanding per client. **Marquee tool for collections.** |
| `clients_bulk` | `/api/clients/bulk` | Structured client list + optional `balance`, `companies`, `recent_invoices`. |
| `sales_by_client` | `/api/sales/by-client` | Invoiced/paid per client over a date range. |
| `purchase_orders_bulk` | `/api/purchase-orders/bulk` | POs + optional `items`. |
| `dispatches_bulk` | `/api/dispatches/bulk` | Dispatches + optional `items`, `allocations`. |
| `palm_purchases_bulk` | `/api/palm-purchases/bulk` | Palm purchases + optional `items`. |
| `supply_pipeline` | `/api/supply-pipeline` | Per-SKU open PO qty + in-transit qty + next arrival. **Pair with `products_snapshot` before placing new POs.** |
| `get_client_ledger_json` | `/api/clients/<id>/ledger?format=json` | Structured ledger with date range. |

Adding a new bulk endpoint: keep the response shape `{"result": {"items": [...], "truncated": bool, ...}}`, reuse helpers (`_f`, `_inr`, `_csv_ints`, `_csv_set`, `_arg_date`, `_arg_int`, `_BUCKET_MOVEMENT_TYPES`), and write an MCP wrapper that returns `dict` (FastMCP serializes it).

## Production edit — line-item editing & Eco switch
The production **edit** page (`production/edit.html`) edits existing line items (quantity floored at `qty_dispatched`, plus price) and adds new lines via the combobox picker. `update_production_order(po_id, data, item_updates, new_items)` applies the `production_qty` deltas — **skipped entirely when the PO is a draft** (drafts hold no stock).

- **Eco switch:** each existing line that has an eco counterpart shows a **Switch → Eco/Std** button → a small popover takes a qty (`0 < v ≤ ordered − dispatched`); **Confirm** reduces the source line and adds the counterpart line (merging into it if already present, else a new picker row). It's purely client-side form manipulation — the normal **Save** persists it (so for an open PO the `production_qty` moves from the standard SKU to the eco SKU automatically; a draft moves nothing).
- The route builds the counterpart data in `_build_product_choices()` (each choice gets an `eco` field) and `_eco_map(choices)` → `{'pid:sid': counterpart}` passed to the template as `eco_map` (and to JS as `ECO_MAP`). Eco pairing is bidirectional (main↔eco) via `products.eco_parent_id` / `sub_products.eco_parent_sub_id` (see `app/routes/products.py::create_eco_range`).

## Dispatch costing (transit detail)
The dispatch detail page has a **Costing** card — a **client-side** shipping-cost calculator (no persistence). Two methods × two bases:
- **Rate × weight/CBM:** one rate × each line's weight (or CBM).
- **Full container split:** container cost + duty + other charges, split across lines by their **% share of total weight (or CBM)**.
- **Calculate** renders a per-line table: product, basis value, qty, shipping cost, and per-piece cost (+ totals). Reads raw `quantity`/`cbm`/`gross_weight` from `data-*` attributes on each `tr.cost-line`. Lines with no weight/CBM contribute 0 (a note prompts entering those values).

## Invoice domain — recent changes
- **Discounts:** both invoice-level and per-line-item carry `discount_type` (`'value'` or `'percent'`) + `discount_value`. The resolved ₹ figure is stored in `invoices.discount_amount` for backward compatibility. See `_compute_totals()` and `_item_net()` in `app/services/invoice_service.py`.
- **Drafts** never deduct stock and may be saved with qty > stock. The "Issue Invoice" button on the detail page is server-gated AND disabled in the UI when `stock_status` reports shortages.
- **Stock refresh** endpoint: `GET /invoices/api/stock-refresh` returns current `stock_qty` for all active products + sub-products. The form's "Refresh Stock" button uses it.
- **Pcs/Box toggle** on the invoice detail line items uses each product's `pcs_per_carton` (sub-product preferred, falls back to parent). Items without a configured `pcs_per_carton` stay in pieces when the user toggles to boxes.

## Line-item form conventions (production / dispatch / palm)
- **Picker excludes already-added items:** each combobox filters `CATALOG` through `_availableCatalog(input)` / `_selectedKeys(input)` so a product/sub already chosen on another line (keyed `pid:sid`) is hidden — same idea as the invoice form (`getSelectedKeys`/`cbBuildOptions`). Production **edit** also counts the server-rendered existing-item rows (`tr[data-pid]`), not just new picker rows.
- **Dispatch "→" button:** on the new-dispatch form, selecting a product with production stock renders a `→` button in the In-Production cell; `useProductionQty(btn)` sets that line's quantity to the available `production_qty`.
- **Number inputs:** spinners are removed globally (base.html `<style>`) and the scroll-wheel is prevented from changing a focused number input (global `wheel` listener in base.html) — values change only by typing.
- **Unsaved-changes guard (`window.__dirtyCheck`)** returns `{isDirty, label, save, saveDraft}`. The base.html modal shows **Save Changes and Leave** when `save` is set and **Save as Draft and Leave** when `saveDraft` is set. The create forms (production/transit/palm `new`) provide `saveDraft`, which clicks the form's `button[value="draft"]`. The transit submit handler skips its over-quantity gate when `e.submitter.value === 'draft'` (drafts may exceed production stock).

## Conventions
- All routes use `@login_required` + `@permission_required("module", "action")`.
- SQL goes through `app.database.get_db()` which returns a per-request `sqlite3.Connection` (row_factory=Row).
- Templates extend `base.html`; use the `inr` / `indian` Jinja filters for currency / Indian-style number formatting.
- API endpoints under `/invoices/api/...` should return JSON via `jsonify`.

## Things to know about the working tree
- `accountant.db` (root) is staged-deleted but the deletion isn't committed yet. It's a leftover from when the DB was moved into `data/ledger.db`.
- `data/` is gitignored; the live local DB and its `.bak` snapshots live there.
- `.claude/worktrees/` contains agent-isolated copies of the repo — ignore them, they're not the working tree.

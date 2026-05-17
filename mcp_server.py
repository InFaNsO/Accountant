"""
Ledger MCP Server — HTTP proxy to the Ledger API
──────────────────────────────────────────────────
Connects to the Ledger Flask app API (local or production).

Configuration — set these environment variables:
  LEDGER_API_URL = https://admin.applestreeabrasives.com   (or http://127.0.0.1:5000 for local)
  LEDGER_API_KEY = your-secret-key                         (must match MCP_API_KEY on the server)

Setup (Claude Desktop) — edit %APPDATA%/Claude/claude_desktop_config.json:
{
  "mcpServers": {
    "ledger": {
      "command": "C:/Users/Bhavil/miniconda3/python.exe",
      "args": ["D:/Code/Claude/Accountant/mcp_server.py"],
      "env": {
        "LEDGER_API_URL": "https://admin.applestreeabrasives.com",
        "LEDGER_API_KEY": "your-secret-key-here"
      }
    }
  }
}
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from mcp.server.fastmcp import FastMCP

# ── Config ───────────────────────────────────────────────────────────────────
LEDGER_API_URL = os.environ.get("LEDGER_API_URL", "http://127.0.0.1:5000").rstrip("/")
LEDGER_API_KEY = os.environ.get("LEDGER_API_KEY", "")

mcp = FastMCP("Ledger", instructions=(
    "You are an AI assistant for Ledger, a small-business accounting app used in India. "
    "Currency is always Indian Rupees (₹ INR). Use Indian number formatting (lakhs/crores) for large amounts. "
    f"Today's date is {date.today().isoformat()}. "
    "Always use tools to fetch live data — never guess figures. "
    "For ALL write operations (create / update / delete) state exactly what you will do and wait for user confirmation before calling the tool. "
    "For DELETE operations always warn: 'This permanently deletes [entity] and cannot be undone. Confirm?' "
    "Never call a delete tool unless the user responds with an explicit yes/confirm in their message."
))


def _call(method: str, path: str, params: dict = None, body: dict = None) -> str:
    """Make an authenticated HTTP call to the Ledger API and return the result string."""
    url = f"{LEDGER_API_URL}/api/{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-MCP-Key": LEDGER_API_KEY}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read())
            return r.get("result", r.get("error", "Unknown response"))
    except urllib.error.HTTPError as e:
        try:
            r = json.loads(e.read())
            return r.get("error", f"HTTP {e.code}")
        except Exception:
            return f"HTTP error {e.code}"
    except Exception as ex:
        return f"Error connecting to Ledger API: {ex}"


# ═════════════════════════════════════════════════════════════════════════════
# CLIENTS — READ
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_clients(query: str) -> str:
    """Search clients by name or company. Returns matching IDs and names."""
    return _call("GET", "clients/search", params={"q": query})


@mcp.tool()
def get_all_clients_summary() -> str:
    """All clients with outstanding balances sorted by most owed first."""
    return _call("GET", "clients/summary")


@mcp.tool()
def get_client_details(client_id: int) -> str:
    """Get full contact info and current balance for a client."""
    return _call("GET", f"clients/{client_id}")


@mcp.tool()
def get_client_ledger(client_id: int) -> str:
    """Full chronological ledger for a client: opening balance, invoices, payments, running balance."""
    return _call("GET", f"clients/{client_id}/ledger")


@mcp.tool()
def get_client_invoices(client_id: int) -> str:
    """List all invoices for a client with amounts and status."""
    return _call("GET", f"clients/{client_id}/invoices")


# ═════════════════════════════════════════════════════════════════════════════
# CLIENTS — WRITE
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_client(
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    country: str = "",
    tax_id: str = "",
    notes: str = "",
    opening_balance_amt: float = 0.0,
    opening_balance_type: str = "debt",
) -> str:
    """
    Create a new client. ONLY call after explicit user confirmation.
    opening_balance_type: 'debt' (client owes us) | 'credit' (client pre-paid)
    """
    return _call("POST", "clients", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_client(
    client_id: int,
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    country: str = "",
    tax_id: str = "",
    notes: str = "",
    opening_balance_amt: float = None,
    opening_balance_type: str = None,
) -> str:
    """
    Update an existing client. ONLY call after explicit user confirmation.
    Leave opening_balance_amt as None to keep the existing opening balance unchanged.
    opening_balance_type: 'debt' | 'credit'
    """
    return _call("PUT", f"clients/{client_id}", body={k: v for k, v in locals().items() if k not in ("self", "client_id")})


@mcp.tool()
def delete_client(client_id: int) -> str:
    """
    PERMANENTLY delete a client and ALL their invoices and payments.
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"clients/{client_id}")


# ═════════════════════════════════════════════════════════════════════════════
# INVOICES — READ
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_recent_invoices(limit: int = 10) -> str:
    """Get the most recent invoices across all clients."""
    return _call("GET", "invoices/recent", params={"limit": limit})


@mcp.tool()
def get_overdue_invoices() -> str:
    """List all invoices past their due date and not fully paid."""
    return _call("GET", "invoices/overdue")


@mcp.tool()
def get_invoice_details(invoice_number: str) -> str:
    """Get full details of a specific invoice by number (e.g. INV-0001)."""
    return _call("GET", f"invoices/{invoice_number}")


# ═════════════════════════════════════════════════════════════════════════════
# INVOICES — WRITE
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_invoice(
    client_id: int,
    items: list,
    issue_date: str = "",
    due_date: str = "",
    notes: str = "",
    discount_amount: float = 0.0,
    status: str = "issued",
) -> str:
    """
    Create a new invoice. ONLY call after explicit user confirmation.

    items: list of dicts, each with keys:
      description (required), quantity, unit_price, tax_rate (%), sku (optional)
      product_id (optional int), sub_product_id (optional int)

    Example item: {"description": "Widget A", "quantity": 2, "unit_price": 500, "tax_rate": 18}

    Deducts from warehouse stock automatically for catalog products.
    issue_date / due_date: YYYY-MM-DD format. Defaults to today.
    status: issued | sent | paid
    """
    return _call("POST", "invoices", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_invoice_status(invoice_id: int, status: str) -> str:
    """
    Change the status of an invoice. ONLY call after explicit user confirmation.
    status: issued | sent | partial | paid | cancelled
    Setting to 'cancelled' restores warehouse stock for catalog items.
    """
    return _call("PUT", f"invoices/{invoice_id}/status", body={"status": status})


@mcp.tool()
def delete_invoice(invoice_id: int) -> str:
    """
    PERMANENTLY delete an invoice and its line items.
    Only call after the user explicitly confirmed deletion.
    Does NOT reverse stock or payment allocations automatically.
    """
    return _call("DELETE", f"invoices/{invoice_id}")


# ═════════════════════════════════════════════════════════════════════════════
# PAYMENTS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_recent_payments(limit: int = 10) -> str:
    """Get the most recent payments received across all clients."""
    return _call("GET", "payments/recent", params={"limit": limit})


@mcp.tool()
def record_payment(
    client_id: int,
    amount: float,
    payment_date: str,
    method: str,
    invoice_id: int = None,
    reference: str = "",
    notes: str = "",
) -> str:
    """
    Record a payment from a client. ONLY call after explicit user confirmation.

    method: cash | bank_transfer | cheque | upi | other
    payment_date: YYYY-MM-DD
    invoice_id: optional — links payment to a specific invoice.
      If omitted, automatically allocates: opening balance → oldest invoices → unallocated surplus.
    """
    return _call("POST", "payments", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def delete_payment(payment_id: int) -> str:
    """
    PERMANENTLY delete a payment record.
    Only call after the user explicitly confirmed deletion.
    Invoice paid status is automatically recalculated.
    """
    return _call("DELETE", f"payments/{payment_id}")


@mcp.tool()
def add_ledger_entry(
    client_id: int,
    entry_date: str,
    description: str,
    debit: float = 0.0,
    credit: float = 0.0,
) -> str:
    """
    Add a manual debit or credit entry to a client's ledger.
    ONLY call after explicit user confirmation.

    debit: amount client owes us (increases their balance due)
    credit: amount we owe client (reduces their balance due)
    entry_date: YYYY-MM-DD
    """
    return _call("POST", "payments/ledger-entry", body={k: v for k, v in locals().items() if k != "self"})


# ═════════════════════════════════════════════════════════════════════════════
# BUSINESS STATS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_business_stats() -> str:
    """Overall business snapshot: revenue, outstanding, overdue, recent activity."""
    return _call("GET", "stats")


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORIES
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_categories() -> str:
    """List all product categories."""
    return _call("GET", "categories")


@mcp.tool()
def create_category(name: str, description: str = "") -> str:
    """Create a product category. ONLY call after explicit user confirmation."""
    return _call("POST", "categories", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_category(cat_id: int, name: str, description: str = "") -> str:
    """Update a product category. ONLY call after explicit user confirmation."""
    return _call("PUT", f"categories/{cat_id}", body={k: v for k, v in locals().items() if k not in ("self", "cat_id")})


@mcp.tool()
def delete_category(cat_id: int) -> str:
    """
    PERMANENTLY delete a category.
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"categories/{cat_id}")


# ═════════════════════════════════════════════════════════════════════════════
# PRODUCTS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_products(query: str) -> str:
    """Search products and sub-products by name or SKU."""
    return _call("GET", "products/search", params={"q": query})


@mcp.tool()
def get_stock_summary() -> str:
    """Current stock levels for all products and sub-products (warehouse / production / transit)."""
    return _call("GET", "products/stock")


@mcp.tool()
def get_low_stock_alerts() -> str:
    """List all products and sub-products currently below their minimum stock level."""
    return _call("GET", "products/low-stock")


@mcp.tool()
def create_product(
    name: str,
    unit_price: float = 0.0,
    tax_rate: float = 18.0,
    sku: str = "",
    description: str = "",
    min_quantity: float = 0.0,
    opening_stock: float = 0.0,
    category_id: int = None,
    pcs_per_carton: int = 0,
) -> str:
    """
    Create a new product. ONLY call after explicit user confirmation.
    Use list_categories() to get valid category_id values.
    pcs_per_carton: pieces per carton (inherited by all sub-products).
    """
    return _call("POST", "products", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_product(
    product_id: int,
    name: str,
    unit_price: float = None,
    tax_rate: float = None,
    sku: str = "",
    description: str = "",
    min_quantity: float = None,
    category_id: int = None,
    is_active: bool = True,
    pcs_per_carton: int = None,
) -> str:
    """
    Update an existing product. ONLY call after explicit user confirmation.
    Pass None for numeric fields to keep existing values.
    pcs_per_carton: pieces per carton (inherited by all sub-products). Pass None to keep existing.
    """
    return _call("PUT", f"products/{product_id}", body={k: v for k, v in locals().items() if k not in ("self", "product_id")})


@mcp.tool()
def delete_product(product_id: int) -> str:
    """
    PERMANENTLY delete a product and all its sub-products and stock movements.
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"products/{product_id}")


@mcp.tool()
def adjust_stock(
    product_id: int,
    bucket: str,
    direction: str,
    quantity: float,
    notes: str = "",
    sub_product_id: int = None,
) -> str:
    """
    Directly adjust one stock bucket. ONLY call after explicit user confirmation.
    bucket: warehouse | production | dispatch
    direction: increase | decrease
    """
    return _call("POST", f"products/{product_id}/adjust-stock", body={k: v for k, v in locals().items() if k not in ("self", "product_id")})


@mcp.tool()
def create_sub_product(
    product_id: int,
    name: str,
    sku: str = "",
    description: str = "",
    unit_price: float = 0.0,
    use_parent_price: bool = True,
    min_quantity: float = 0.0,
    opening_stock: float = 0.0,
) -> str:
    """
    Create a sub-product (variant) under an existing product.
    ONLY call after explicit user confirmation.
    use_parent_price=True: sub-product inherits the parent's unit_price.
    """
    return _call("POST", f"products/{product_id}/sub-products", body={k: v for k, v in locals().items() if k not in ("self", "product_id")})


@mcp.tool()
def update_sub_product(
    sub_id: int,
    name: str,
    sku: str = "",
    description: str = "",
    unit_price: float = None,
    use_parent_price: bool = None,
    min_quantity: float = None,
    is_active: bool = True,
) -> str:
    """
    Update an existing sub-product. ONLY call after explicit user confirmation.
    Pass None for numeric fields to keep existing values.
    """
    return _call("PUT", f"sub-products/{sub_id}", body={k: v for k, v in locals().items() if k not in ("self", "sub_id")})


@mcp.tool()
def delete_sub_product(sub_id: int) -> str:
    """
    PERMANENTLY delete a sub-product and its stock movements.
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"sub-products/{sub_id}")


# ═════════════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_suppliers() -> str:
    """List all active suppliers."""
    return _call("GET", "suppliers")


@mcp.tool()
def create_supplier(
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    notes: str = "",
) -> str:
    """Create a new supplier. ONLY call after explicit user confirmation."""
    return _call("POST", "suppliers", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_supplier(
    supplier_id: int,
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    address: str = "",
    notes: str = "",
) -> str:
    """Update an existing supplier. ONLY call after explicit user confirmation."""
    return _call("PUT", f"suppliers/{supplier_id}", body={k: v for k, v in locals().items() if k not in ("self", "supplier_id")})


@mcp.tool()
def delete_supplier(supplier_id: int) -> str:
    """
    PERMANENTLY delete a supplier.
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"suppliers/{supplier_id}")


# ═════════════════════════════════════════════════════════════════════════════
# PRODUCTION ORDERS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_purchase_orders(status: str = "open") -> str:
    """List production/purchase orders. status: open | closed | all"""
    return _call("GET", "purchase-orders", params={"status": status})


@mcp.tool()
def get_purchase_order_details(po_id: int) -> str:
    """Get full details of a production order including line items."""
    return _call("GET", f"purchase-orders/{po_id}")


@mcp.tool()
def create_production_order(
    name: str,
    items: list,
    supplier_id: int = None,
    expected_completion: str = "",
    notes: str = "",
) -> str:
    """
    Create a production/purchase order. ONLY call after explicit user confirmation.

    items: list of dicts, each with:
      product_id (int, optional), sub_product_id (int, optional),
      quantity (float, required), price (float, optional)

    Example: [{"product_id": 5, "quantity": 100, "price": 250}]

    Adds the ordered quantities to production_qty for each item.
    """
    return _call("POST", "production-orders", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_production_order(
    po_id: int,
    name: str,
    supplier_id: int = None,
    expected_completion: str = "",
    notes: str = "",
) -> str:
    """
    Update the header of a production order (name, supplier, due date, notes).
    ONLY call after explicit user confirmation.
    Note: line items cannot be changed after creation.
    """
    return _call("PUT", f"production-orders/{po_id}", body={k: v for k, v in locals().items() if k not in ("self", "po_id")})


@mcp.tool()
def close_production_order(po_id: int) -> str:
    """Mark a production order as closed. ONLY call after explicit user confirmation."""
    return _call("PUT", f"production-orders/{po_id}/close", body={})


@mcp.tool()
def delete_production_order(po_id: int) -> str:
    """
    PERMANENTLY delete a production order and reverse its production quantities.
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"production-orders/{po_id}")


# ═════════════════════════════════════════════════════════════════════════════
# DISPATCHES
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_dispatches(status: str = "in_transit") -> str:
    """List dispatches. status: in_transit | partially_received | received | all"""
    return _call("GET", "dispatches", params={"status": status})


@mcp.tool()
def get_dispatch_details(dispatch_id: int) -> str:
    """Get full details of a dispatch including items and received quantities."""
    return _call("GET", f"dispatches/{dispatch_id}")


@mcp.tool()
def create_dispatch(
    name: str,
    items: list,
    supplier_id: int = None,
    dispatch_date: str = "",
    expected_arrival: str = "",
    notes: str = "",
) -> str:
    """
    Create a new dispatch. ONLY call after explicit user confirmation.

    items: list of dicts, each with:
      product_id (int, optional), sub_product_id (int, optional),
      quantity (float, required), price (float, optional)

    Moves qty from production_qty → in_transit_qty.
    Automatically allocates against open PO items (FIFO).
    Requires sufficient production stock — check get_stock_summary() first.
    """
    return _call("POST", "dispatches", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def receive_dispatch_items(dispatch_id: int, received_items: list) -> str:
    """
    Record receipt of items from a dispatch. ONLY call after explicit user confirmation.

    received_items: list of dicts: [{"dispatch_item_id": 12, "quantity": 50}, ...]
    Use get_dispatch_details() to find dispatch_item_ids.
    Moves qty from in_transit_qty → stock_qty (warehouse).
    """
    return _call("POST", f"dispatches/{dispatch_id}/receive", body={"received_items": received_items})


@mcp.tool()
def delete_dispatch(dispatch_id: int) -> str:
    """
    PERMANENTLY delete a dispatch and reverse all stock movements.
    Only call after the user explicitly confirmed deletion.
    Moves unreceived qty back from in_transit → production.
    """
    return _call("DELETE", f"dispatches/{dispatch_id}")


# ═════════════════════════════════════════════════════════════════════════════
# STOCK TALLIES
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_stock_tallies() -> str:
    """
    List all stock tallies (physical inventory counts).
    Returns id, name, status (draft/applied), created_at, total_items, pending_items.
    """
    return _call("GET", "tallies")


@mcp.tool()
def get_stock_tally_detail(tally_id: int) -> str:
    """
    Get full detail of a stock tally including all line items grouped by category.
    Each item shows: item_id, product/sub name, digital_qty (system stock),
    physical_qty (counted), and diff (physical - digital, null if not yet counted).
    Use item_id values with update_tally_item_count to record physical counts.
    """
    return _call("GET", f"tallies/{tally_id}")


@mcp.tool()
def create_stock_tally(name: str, notes: str = "") -> str:
    """
    Create a new stock tally (physical inventory count session).
    Snapshots current warehouse stock quantities for all active products.
    Returns the new tally_id. Status starts as 'draft'.
    """
    return _call("POST", "tallies", body={"name": name, "notes": notes})


@mcp.tool()
def update_tally_item_count(tally_id: int, item_id: int, physical_qty: float) -> str:
    """
    Record the physical counted quantity for one item in a stock tally.
    tally_id: the tally being worked on (must be in draft status).
    item_id: the item_id from get_stock_tally_detail response.
    physical_qty: the physically counted quantity (use 0 if item is out of stock).
    """
    return _call("PUT", f"tallies/{tally_id}/items/{item_id}", body={"physical_qty": physical_qty})


@mcp.tool()
def apply_stock_tally(tally_id: int) -> str:
    """
    Apply a completed stock tally — adjusts all warehouse stock quantities to match
    physical counts and marks the tally as applied. All items must have physical_qty
    filled in before applying. This action is irreversible — confirm with user first.
    """
    return _call("POST", f"tallies/{tally_id}/apply")


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()

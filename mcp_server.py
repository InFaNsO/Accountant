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
    """Search clients by name or any of their company names. Returns matching IDs, names, and companies."""
    return _call("GET", "clients/search", params={"q": query})


@mcp.tool()
def get_all_clients_summary() -> str:
    """All clients with outstanding balances sorted by most owed first."""
    return _call("GET", "clients/summary")


@mcp.tool()
def get_client_details(client_id: int) -> str:
    """Get full contact info, current balance, and list of companies for a client."""
    return _call("GET", f"clients/{client_id}")


@mcp.tool()
def get_client_ledger(client_id: int, company_id: int = None) -> str:
    """Full chronological ledger for a client: opening balance, invoices, payments, running balance.
    Pass company_id to filter the ledger to a specific company under the client."""
    return _call("GET", f"clients/{client_id}/ledger", params={"company_id": company_id})


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
    email: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    country: str = "",
    tax_id: str = "",
    notes: str = "",
    opening_balance_amt: float = 0.0,
    opening_balance_type: str = "debt",
    companies: list = None,
) -> str:
    """
    Create a new client. ONLY call after explicit user confirmation.
    opening_balance_type: 'debt' (client owes us) | 'credit' (client pre-paid)
    companies: optional list of company dicts, each with:
      name (required), tax_id (optional), opening_balance_amt (float), opening_balance_type ('debt'|'credit')
    Example: [{"name": "Acme Pvt Ltd", "tax_id": "22ABCDE1234F1Z5", "opening_balance_amt": 5000, "opening_balance_type": "debt"}]
    """
    return _call("POST", "clients", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_client(
    client_id: int,
    name: str,
    email: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    country: str = "",
    tax_id: str = "",
    notes: str = "",
    opening_balance_amt: float = None,
    opening_balance_type: str = None,
    companies: list = None,
    tally_lock: bool = None,
    balance_lock_enabled: bool = None,
    balance_lock_limit: float = None,
) -> str:
    """
    Update an existing client. ONLY call after explicit user confirmation.
    Leave opening_balance_amt as None to keep the existing opening balance unchanged.
    opening_balance_type: 'debt' | 'credit'
    companies: optional list of company updates. Each dict should have:
      id (int, to update existing) or omit id (to add new), plus name, tax_id, opening_balance_amt, opening_balance_type.
      Pass None to skip company changes entirely.
    Invoice locks (None = leave unchanged). While locked, drafts are allowed but
    invoices cannot be issued:
      tally_lock: manual lock on/off.
      balance_lock_enabled + balance_lock_limit (₹): auto-locks while the client's
      outstanding debt exceeds the limit; releases automatically once it drops back.
    """
    return _call("PUT", f"clients/{client_id}", body={k: v for k, v in locals().items() if k not in ("self", "client_id")})


@mcp.tool()
def get_client_lock_status(client_id: int) -> str:
    """
    Get a client's invoice-lock status: whether they are currently locked (and why),
    the manual tally lock state, the balance lock settings (max debt limit), and
    their outstanding debt. While locked, drafts are allowed but invoices cannot
    be issued.
    """
    return _call("GET", f"clients/{client_id}/locks")


@mcp.tool()
def set_client_lock(
    client_id: int,
    tally_lock: bool = None,
    balance_lock_enabled: bool = None,
    balance_lock_limit: float = None,
) -> str:
    """
    Enable/disable a client's invoice locks. ONLY call after explicit user confirmation.
    Leave a parameter as None to keep it unchanged; at least one must be provided.

    tally_lock: manual lock on/off — stays on until switched off.
    balance_lock_enabled: auto-lock that engages while outstanding debt (issued
      invoices only) exceeds balance_lock_limit and releases automatically once
      it drops back under.
    balance_lock_limit: max debt in ₹ for the balance lock (must be > 0 for the
      balance lock to have any effect).

    While locked, drafts can still be created but invoices cannot be issued.
    Returns the updated lock status.
    """
    return _call("PUT", f"clients/{client_id}/locks", body={
        "tally_lock": tally_lock,
        "balance_lock_enabled": balance_lock_enabled,
        "balance_lock_limit": balance_lock_limit,
    })


@mcp.tool()
def delete_client(client_id: int) -> str:
    """
    PERMANENTLY delete a client and ALL their invoices, payments, and companies.
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"clients/{client_id}")


# ═════════════════════════════════════════════════════════════════════════════
# CLIENT COMPANIES
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_client_companies(client_id: int) -> str:
    """List all companies under a client with their current balances."""
    return _call("GET", f"clients/{client_id}/companies")


@mcp.tool()
def create_company(
    client_id: int,
    name: str,
    tax_id: str = "",
    opening_balance_amt: float = 0.0,
    opening_balance_type: str = "debt",
) -> str:
    """
    Add a new company under an existing client. ONLY call after explicit user confirmation.
    opening_balance_type: 'debt' (company owes us) | 'credit' (company pre-paid)
    """
    return _call("POST", f"clients/{client_id}/companies", body={
        "name": name,
        "tax_id": tax_id,
        "opening_balance_amt": opening_balance_amt,
        "opening_balance_type": opening_balance_type,
    })


@mcp.tool()
def update_company(
    client_id: int,
    company_id: int,
    name: str,
    tax_id: str = "",
    opening_balance_amt: float = None,
    opening_balance_type: str = None,
) -> str:
    """
    Update an existing company. ONLY call after explicit user confirmation.
    Leave opening_balance_amt as None to keep the existing opening balance unchanged.
    """
    return _call("PUT", f"clients/{client_id}/companies/{company_id}", body={
        "name": name,
        "tax_id": tax_id,
        "opening_balance_amt": opening_balance_amt,
        "opening_balance_type": opening_balance_type,
    })


@mcp.tool()
def delete_company(client_id: int, company_id: int) -> str:
    """
    PERMANENTLY delete a company record (does not delete invoices/payments linked to it).
    Only call after the user explicitly confirmed deletion.
    """
    return _call("DELETE", f"clients/{client_id}/companies/{company_id}")


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
    company_id: int = None,
) -> str:
    """
    Create a new invoice. ONLY call after explicit user confirmation.

    items: list of dicts, each with keys:
      description (required), quantity, unit_price, tax_rate (%), sku (optional)
      product_id (optional int), sub_product_id (optional int)

    Example item: {"description": "Widget A", "quantity": 2, "unit_price": 500, "tax_rate": 18}

    company_id: optional — links invoice to a specific company under the client.
      Use get_client_companies(client_id) to find company IDs.
    Deducts from warehouse stock automatically for catalog products.
    issue_date / due_date: YYYY-MM-DD format. Defaults to today.
    status: issued | sent | paid
    If the client is locked (manual tally lock, or auto balance lock over their debt
    limit), the invoice is saved as a DRAFT instead — the result message says so.
    """
    return _call("POST", "invoices", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def update_invoice_status(invoice_id: int, status: str) -> str:
    """
    Change the status of an invoice. ONLY call after explicit user confirmation.
    status: draft | issued | sent | partial | paid | cancelled
    draft→issued: blocked with an error if the client is locked (tally lock or
    balance lock over debt limit), then checks warehouse stock — fails with error
    if any item is short.
    Setting to 'cancelled' restores warehouse stock (only if invoice was previously issued, not draft).
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
    company_id: int = None,
    reference: str = "",
    notes: str = "",
    is_opening_balance: bool = False,
) -> str:
    """
    Record a payment from a client. ONLY call after explicit user confirmation.

    method: cash | bank_transfer | cheque | upi | other
    payment_date: YYYY-MM-DD
    invoice_id: optional — links payment to a specific invoice.
      If omitted, automatically allocates: opening balance → oldest invoices → unallocated surplus.
    company_id: optional — associates payment with a specific company under the client.
      Use get_client_companies(client_id) to find company IDs.
    is_opening_balance: set True if this payment is for the client's OPENING (old) balance.
      Such payments are assigned to the old balance only — never allocated to invoices and
      skipped by reconciliation (invoice_id is ignored when this is True).
    """
    return _call("POST", "payments", body={k: v for k, v in locals().items() if k != "self"})


@mcp.tool()
def set_payment_opening_balance(payment_id: int, is_opening_balance: bool = True) -> str:
    """
    Mark (or unmark) an EXISTING payment as an opening-balance payment. When marked, the
    payment is assigned to the client's old balance — excluded from invoice allocation and
    skipped by reconciliation. The client is automatically re-reconciled afterward.

    Use this to fix payments that were meant for the opening balance but got allocated to
    invoices. ONLY call after explicit user confirmation.
    """
    return _call("POST", f"payments/{payment_id}/opening-balance",
                 body={"is_opening_balance": is_opening_balance})


@mcp.tool()
def delete_payment(payment_id: int) -> str:
    """
    PERMANENTLY delete a payment record.
    Only call after the user explicitly confirmed deletion.
    Invoice paid status is automatically recalculated.
    """
    return _call("DELETE", f"payments/{payment_id}")


@mcp.tool()
def reconcile_client_payments(client_id: int = None, detail: bool = True) -> dict:
    """
    Reconcile payments by applying each client's UNALLOCATED payment money to their
    OLDEST unpaid invoices (oldest-first). Fixes invoices left unpaid because a payment
    was recorded before its invoice existed, or was never allocated to it.

    client_id: optional — reconcile just this client. Omit to reconcile ALL clients.
    detail:    default True — return a structured breakdown of exactly what changed:
               per affected client, the newly_paid_invoices, no_longer_paid_invoices,
               and allocation_changes (each payment→invoice delta with before/after).
               Set False for a short one-line text summary instead.

    Rewrites payment allocations and invoice paid-status only. It does NOT create, edit,
    or delete any payment or invoice. Idempotent. ONLY call after explicit user confirmation.
    """
    return _call("POST", "clients/reconcile", body={"client_id": client_id, "detail": detail})


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
    """Search products and sub-products by name or SKU. Returns name, SKU, price, stock, and pcs_per_carton (pieces per carton)."""
    return _call("GET", "products/search", params={"q": query})


@mcp.tool()
def get_stock_summary() -> str:
    """Current stock levels for all products and sub-products (warehouse / production / transit).
    Also shows pcs_per_carton (pieces per carton) for each product/sub-product.
    Eco-paired products are shown merged with their main product, displaying combined stock figures."""
    return _call("GET", "products/stock")


@mcp.tool()
def get_low_stock_alerts() -> str:
    """List all products and sub-products currently below their minimum stock level.
    For eco-paired products, combined (main + eco) stock is compared against the shared minimum.
    If combined stock is short, BOTH the main and eco product/sub-product are listed as alerts."""
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
    If the product has an eco range, changing min_quantity automatically cascades to the eco product.
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
def create_eco_range(
    product_id: int,
    unit_price: float,
    sku: str = "",
) -> str:
    """
    Create an eco range for an existing main product. ONLY call after explicit user confirmation.
    Creates an eco variant product named "Eco <product name>" (and mirrors all sub-products if the
    main has sub-products, each named "Eco <sub-product name>").
    Eco range always starts with zero stock — use adjust_stock per eco product/sub-product to add stock.
    The eco product shares the same minimum stock threshold as the main product.
    Stock alerts compare combined (main + eco) stock against the minimum — if short, BOTH are flagged.
    For products with sub-products, update eco sub-product pricing separately via update_sub_product.
    Updating min_quantity on the main product automatically cascades to the eco product.
    """
    return _call("POST", f"products/{product_id}/create-eco-range", body={
        "unit_price": unit_price, "sku": sku
    })


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
# PALM PURCHASES — direct cash/spot purchases that land straight in warehouse
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_palm_purchases() -> str:
    """List all palm purchases (instant warehouse stock-in records), most recent first.
    Each entry shows date, supplier, total qty added, and total cost."""
    return _call("GET", "palm-purchases")


@mcp.tool()
def get_palm_purchase(pp_id: int) -> str:
    """Get full details of a palm purchase including its line items (products, qty, unit cost)."""
    return _call("GET", f"palm-purchases/{pp_id}")


@mcp.tool()
def create_palm_purchase(
    items: list,
    supplier_id: int = None,
    purchase_date: str = None,
    name: str = "",
    notes: str = "",
) -> str:
    """
    Record a palm purchase — instant cash/spot buy. Warehouse stock_qty is immediately
    increased for each product/sub-product by the quantity provided, and a
    'palm_purchase' stock_movement is logged for the audit trail.

    items: list of dicts, each with:
       product_id (int, required),
       sub_product_id (int, optional — required when buying a variant),
       quantity (number, required, must be > 0),
       unit_cost (number, optional),
       notes (str, optional)
    supplier_id: optional supplier; omit for walk-in / unknown.
    purchase_date: YYYY-MM-DD; defaults to today.

    ONLY call after explicit user confirmation — this physically updates warehouse stock.
    """
    body = {"items": items}
    if supplier_id is not None:   body["supplier_id"]   = supplier_id
    if purchase_date is not None: body["purchase_date"] = purchase_date
    if name:                      body["name"]          = name
    if notes:                     body["notes"]         = notes
    return _call("POST", "palm-purchases", body=body)


@mcp.tool()
def delete_palm_purchase(pp_id: int) -> str:
    """
    Delete a palm purchase — REVERSES the warehouse stock added by it.
    Permanent and cannot be undone. ONLY call after explicit user confirmation.
    """
    return _call("DELETE", f"palm-purchases/{pp_id}")


# ═════════════════════════════════════════════════════════════════════════════
# STOCK HISTORY — audit trail for any product / sub-product across buckets
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_product_stock_history(
    product_id: int,
    sub_product_id: int = None,
    bucket: str = None,
    limit: int = 100,
) -> str:
    """
    Chronological stock movement history for a product (or sub-product).
    Returns current warehouse/production/transit levels + the most recent N movements.

    Each movement has a `type` such as:
      - opening, add, warehouse_add, warehouse_deduct, correction  (warehouse bucket)
      - production, production_add, production_deduct             (production bucket)
      - dispatch, transit_dispatch, dispatch_add, dispatch_deduct  (transit bucket)
      - transit_arrival, arrival                                   (transit → warehouse)
      - sale, sale_cancelled                                       (warehouse, via invoices)
      - palm_purchase, palm_purchase_reversed                      (warehouse, instant buys)

    Args:
      product_id:     required.
      sub_product_id: optional — restricts to that variant.
      bucket:         optional — one of "warehouse" | "production" | "transit".
      limit:          number of most recent movements (default 100, max 500).
    """
    params = {"limit": limit}
    if sub_product_id is not None: params["sub_product_id"] = sub_product_id
    if bucket:                     params["bucket"]         = bucket
    return _call("GET", f"products/{product_id}/stock-history", params=params)


# ═════════════════════════════════════════════════════════════════════════════
# BULK ENDPOINTS — single-call fan-outs (structured JSON, not text)
# ─────────────────────────────────────────────────────────────────────────────
# Use these BEFORE looping over single-id tools. Every one returns
#     {"items": [...], "truncated": bool, "count": int, ...}
# Filters (category_id, product_ids CSV, status CSV, date_from / date_to YYYY-MM-DD,
# include CSV) cut payload size; `truncated=true` means raise the limit or narrow filters.
# ═════════════════════════════════════════════════════════════════════════════


def _csv(value):
    """Pass through a Python list or CSV string; return CSV string for query params."""
    if value is None: return None
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v) for v in value)
    return str(value)


@mcp.tool()
def products_snapshot(
    category_id: int = None,
    product_ids: str = None,
    sub_product_ids: str = None,
    include: str = None,
    include_inactive: bool = False,
    limit: int = 200,
) -> dict:
    """One row per SKU with current stock (warehouse/production/transit) + optional 30/60/90-day
    sales velocity and last purchase cost. **Use this BEFORE looping `get_product_stock_history`
    for purchase-order planning** — replaces N per-product calls with one.

    Args:
      category_id:      filter to a category.
      product_ids:      CSV of parent product ids (also pulls their sub-products).
      sub_product_ids:  CSV of specific sub-product ids.
      include:          CSV of expansions — "velocity", "last_purchase".
      include_inactive: include retired SKUs (default false).
      limit:            default 200, max 500.
    """
    return _call("GET", "products/snapshot", params={
        "category_id":      category_id,
        "product_ids":      _csv(product_ids),
        "sub_product_ids":  _csv(sub_product_ids),
        "include":          _csv(include),
        "include_inactive": 1 if include_inactive else None,
        "limit":            limit,
    })


@mcp.tool()
def products_stock_history_bulk(
    product_ids: str = None,
    sub_product_ids: str = None,
    category_id: int = None,
    bucket: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 500,
) -> dict:
    """Stock movements for MANY products in one call. **Replaces N× get_product_stock_history.**
    At least one filter is required.

    Args:
      product_ids / sub_product_ids: CSV ids.
      category_id: filter via parent products.
      bucket: "warehouse" | "production" | "transit".
      date_from / date_to: YYYY-MM-DD.
      limit: default 500, max 2000.
    """
    return _call("GET", "products/stock-history-bulk", params={
        "product_ids":     _csv(product_ids),
        "sub_product_ids": _csv(sub_product_ids),
        "category_id":     category_id,
        "bucket":          bucket,
        "date_from":       date_from,
        "date_to":         date_to,
        "limit":           limit,
    })


@mcp.tool()
def products_sales_velocity(
    category_id: int = None,
    product_ids: str = None,
    sub_product_ids: str = None,
    date_from: str = None,
    date_to: str = None,
    group_by: str = "sub_product",
    limit: int = 500,
) -> dict:
    """Qty sold + revenue per product/sub-product over a date range (default last 90 days).
    Aggregated from invoice_items joined to invoices. Sorted by revenue DESC.

    Args:
      group_by: "product" or "sub_product".
      limit: default 500, max 1000.
    """
    return _call("GET", "products/sales-velocity", params={
        "category_id":     category_id,
        "product_ids":     _csv(product_ids),
        "sub_product_ids": _csv(sub_product_ids),
        "date_from":       date_from,
        "date_to":         date_to,
        "group_by":        group_by,
        "limit":           limit,
    })


@mcp.tool()
def invoices_bulk(
    client_id: int = None,
    company_id: int = None,
    status: str = None,
    date_from: str = None,
    date_to: str = None,
    include: str = None,
    limit: int = 100,
) -> dict:
    """Many invoices in one call. **Replaces `get_invoice_details` looped per id.**

    Args:
      status: CSV — any of paid,issued,partial,draft,cancelled.
      include: CSV — "items" (line items), "payments" (allocations + payment details).
      limit: default 100, max 500.
    """
    return _call("GET", "invoices/bulk", params={
        "client_id":  client_id,
        "company_id": company_id,
        "status":     _csv(status),
        "date_from":  date_from,
        "date_to":    date_to,
        "include":    _csv(include),
        "limit":      limit,
    })


@mcp.tool()
def payments_bulk(
    client_id: int = None,
    company_id: int = None,
    method: str = None,
    date_from: str = None,
    date_to: str = None,
    include: str = None,
    limit: int = 200,
) -> dict:
    """Many payments with allocation breakdown. **Replaces `get_recent_payments` + per-id allocation lookups.**

    Args:
      method: cash, bank_transfer, cheque, upi, balance, other.
      include: CSV — "allocations" (which invoices each payment hit).
      limit: default 200, max 1000.
    """
    return _call("GET", "payments/bulk", params={
        "client_id":  client_id,
        "company_id": company_id,
        "method":     method,
        "date_from":  date_from,
        "date_to":    date_to,
        "include":    _csv(include),
        "limit":      limit,
    })


@mcp.tool()
def clients_outstanding(
    min_amount: float = 0,
    min_age_days: int = 0,
    company_id: int = None,
    limit: int = 200,
) -> dict:
    """Per-client outstanding with aged buckets (0-30, 31-60, 61-90, 90+ days).
    Sorted by total_outstanding DESC. One call covers the entire collections review.

    Args:
      min_amount:  hide clients owing less than this.
      min_age_days: only include clients with at least one unpaid invoice older than N days.
      limit: default 200, max 500.
    """
    return _call("GET", "clients/outstanding", params={
        "min_amount":   min_amount,
        "min_age_days": min_age_days,
        "company_id":   company_id,
        "limit":        limit,
    })


@mcp.tool()
def clients_bulk(
    q: str = None,
    include: str = None,
    limit: int = 200,
) -> dict:
    """Structured client list. **Replaces `get_all_clients_summary` + per-id `get_client_details`.**

    Args:
      q: substring search on client / company name.
      include: CSV — "balance" (invoiced / paid / outstanding / unallocated),
                       "companies" (per-company OB), "recent_invoices" (5 latest).
      limit: default 200, max 500.
    """
    return _call("GET", "clients/bulk", params={
        "q":       q,
        "include": _csv(include),
        "limit":   limit,
    })


@mcp.tool()
def sales_by_client(
    date_from: str = None,
    date_to: str = None,
    min_invoiced: float = 0,
    limit: int = 200,
) -> dict:
    """Total invoiced + total paid per client over a date range (default last 90 days).
    Sorted by total_invoiced DESC.
    """
    return _call("GET", "sales/by-client", params={
        "date_from":    date_from,
        "date_to":      date_to,
        "min_invoiced": min_invoiced,
        "limit":        limit,
    })


@mcp.tool()
def purchase_orders_bulk(
    supplier_id: int = None,
    status: str = None,
    include: str = None,
    limit: int = 100,
) -> dict:
    """POs with optional line items. **Replaces `get_purchase_order_details` looped per PO.**

    Args:
      status: CSV — open, closed.
      include: CSV — "items" (po_items with pending qty).
      limit: default 100, max 300.
    """
    return _call("GET", "purchase-orders/bulk", params={
        "supplier_id": supplier_id,
        "status":      _csv(status),
        "include":     _csv(include),
        "limit":       limit,
    })


@mcp.tool()
def dispatches_bulk(
    supplier_id: int = None,
    status: str = None,
    date_from: str = None,
    date_to: str = None,
    include: str = None,
    limit: int = 100,
) -> dict:
    """Dispatches with optional line items + PO allocations. **Replaces `get_dispatch_details` per id.**

    Args:
      status: CSV — in_transit, partially_received, received.
      include: CSV — "items", "allocations".
      limit: default 100, max 300.
    """
    return _call("GET", "dispatches/bulk", params={
        "supplier_id": supplier_id,
        "status":      _csv(status),
        "date_from":   date_from,
        "date_to":     date_to,
        "include":     _csv(include),
        "limit":       limit,
    })


@mcp.tool()
def palm_purchases_bulk(
    supplier_id: int = None,
    date_from: str = None,
    date_to: str = None,
    include: str = None,
    limit: int = 100,
) -> dict:
    """Palm purchases (instant warehouse stock-ins) with optional items.

    Args:
      include: CSV — "items" (each line's product, qty, unit_cost).
      limit: default 100, max 300.
    """
    return _call("GET", "palm-purchases/bulk", params={
        "supplier_id": supplier_id,
        "date_from":   date_from,
        "date_to":     date_to,
        "include":     _csv(include),
        "limit":       limit,
    })


@mcp.tool()
def supply_pipeline(
    product_ids: str = None,
    sub_product_ids: str = None,
    category_id: int = None,
    limit: int = 300,
) -> dict:
    """Per product/sub-product: open PO pending qty, in-transit qty, next expected arrival,
    and last palm-purchase date. **Use BEFORE planning a purchase order to avoid double-ordering
    what's already in the supply pipeline.** At least one filter is required.
    """
    return _call("GET", "supply-pipeline", params={
        "product_ids":     _csv(product_ids),
        "sub_product_ids": _csv(sub_product_ids),
        "category_id":     category_id,
        "limit":           limit,
    })


@mcp.tool()
def get_client_ledger_json(
    client_id: int,
    company_id: int = None,
    date_from: str = None,
    date_to: str = None,
) -> dict:
    """Structured JSON variant of `get_client_ledger`. Returns entries (each with
    debit/credit/running) + final_balance. Supports date_from/date_to slicing
    (opening balance is omitted when date_from is set, so running balance starts from 0).
    Payment entries include their `allocations` (which invoices the payment hit).
    """
    return _call("GET", f"clients/{client_id}/ledger", params={
        "company_id": company_id,
        "date_from":  date_from,
        "date_to":    date_to,
        "format":     "json",
    })


@mcp.tool()
def get_company_ledger(
    client_id: int,
    company_id: int,
    date_from: str = None,
    date_to: str = None,
) -> dict:
    """Structured ledger for ONE company under a client (invoices + payments + running
    balance, with payment allocations). Use get_client_companies(client_id) to find
    company IDs. date_from/date_to (YYYY-MM-DD) optionally window the ledger.
    """
    return _call("GET", f"clients/{client_id}/ledger", params={
        "company_id": company_id,
        "date_from":  date_from,
        "date_to":    date_to,
        "format":     "json",
    })


@mcp.tool()
def get_client_full(client_id: int, invoice_days: int = 30, invoice_limit: int = 500) -> dict:
    """EVERYTHING about one client in a single call. Prefer this over chaining
    get_client_details + get_client_invoices + get_client_ledger_json.

    Returns: details, balance, invoices (each with line items — product/sub-product
    name, box size, quantity + quantity_boxes, unit price, discount, line total —
    plus payment_status/amount_paid/balance_due), companies (each with its own ledger),
    and the complete client ledger in JSON.

    Args:
      invoice_days:  invoices issued within the last N days (default 30; pass -1 for ALL).
      invoice_limit: cap on invoices returned (default 500, max 2000).
    """
    return _call("GET", f"clients/{client_id}/full", params={
        "invoice_days":  invoice_days,
        "invoice_limit": invoice_limit,
    })


@mcp.tool()
def get_client_product_breakdown(
    client_id: int,
    date_from: str = None,
    date_to: str = None,
) -> dict:
    """What a client DID and DID NOT buy in a date window — useful for spotting
    cross-sell gaps ('what isn't this client buying?').

    Args:
      date_from / date_to: 'YYYY-MM-DD' inclusive bounds. Both optional — omitting
                           them defaults to the last 2 months (… today).

    Returns:
      purchased: products bought in the window, each with total quantity in BOTH
                 `boxes` (null if the product has no box size configured) and
                 `pieces`, plus the `amount` invoiced — sorted by amount desc.
      not_purchased: every other ACTIVE catalogue product, grouped by category.
      Plus the effective date_from/date_to and counts.
    """
    return _call("GET", f"clients/{client_id}/product-breakdown", params={
        "date_from": date_from,
        "date_to":   date_to,
    })


@mcp.tool()
def get_category_products(category_id: int, include_inactive: bool = False) -> dict:
    """All products in a category with nested sub-products. Each product/sub-product
    reports box size and bucket quantities (warehouse / production / transit) plus
    min_quantity. One call to review a whole category's stock.
    """
    return _call("GET", f"categories/{category_id}/products", params={
        "include_inactive": "1" if include_inactive else "0",
    })


@mcp.tool()
def get_sub_product_stock_history(
    product_id: int,
    sub_product_id: int,
    bucket: str = "warehouse",
    limit: int = 100,
) -> dict:
    """Stock-movement history for ONE sub-product. Defaults to the WAREHOUSE bucket;
    pass bucket=production | transit | all. Returns current levels + recent movements.
    """
    params = {"sub_product_id": sub_product_id, "limit": limit}
    if bucket and bucket.lower() != "all":
        params["bucket"] = bucket.lower()
    return _call("GET", f"products/{product_id}/stock-history", params=params)


@mcp.tool()
def get_product_stock_history_by_sub(
    product_id: int,
    bucket: str = "warehouse",
    limit: int = 50,
) -> dict:
    """Stock-movement history for ALL sub-products of a product, grouped per sub-product.
    Defaults to the WAREHOUSE bucket; pass bucket=production | transit | all. A product
    with NO sub-products returns a single product-level group (edge case handled).

    Args:
      limit: movements per group (default 50, max 500).
    """
    return _call("GET", f"products/{product_id}/stock-history-grouped", params={
        "bucket": (bucket or "warehouse").lower(),
        "limit":  limit,
    })


@mcp.tool()
def product_stock_action(
    product_id: int,
    action: str,
    quantity: float,
    sub_product_id: int = None,
    expected_arrival: str = None,
    notes: str = "",
) -> str:
    """Perform a semantic stock MOVE between buckets (transfers, not raw adjustments).
    For raw single-bucket +/- corrections use `adjust_stock` instead.

    action:
      add_stock                → warehouse += quantity
      send_to_production       → warehouse → production
      dispatch_from_production → production → transit (requires expected_arrival YYYY-MM-DD)
      mark_arrived             → transit → warehouse

    Omit sub_product_id to act on the parent product. ONLY call after explicit user confirmation.
    """
    return _call("POST", f"products/{product_id}/stock-action", body={
        "action":           action,
        "quantity":         quantity,
        "sub_product_id":   sub_product_id,
        "expected_arrival": expected_arrival,
        "notes":            notes,
    })


@mcp.tool()
def get_received_transit(
    date_from: str = None,
    date_to: str = None,
    include: str = None,
    limit: int = None,
) -> dict:
    """Transit/dispatches that have ARRIVED (status received / partially_received).
    With NO date range → the single latest arrival. With a date range → all in that
    window. Ordering & date filters use expected_arrival.

    Args:
      date_from/date_to: YYYY-MM-DD window on expected_arrival.
      include: "items" to embed line items (with pending qty).
      limit: override (default 1 without a range, 50 with a range; max 300).
    """
    return _call("GET", "transit/received", params={
        "date_from": date_from, "date_to": date_to,
        "include": _csv(include), "limit": limit,
    })


@mcp.tool()
def get_upcoming_transit(
    date_from: str = None,
    date_to: str = None,
    include: str = None,
    limit: int = None,
) -> dict:
    """Transit/dispatches still ARRIVING (status in_transit / partially_received).
    With NO date range → the single next arrival (expected_arrival >= today).
    With a date range → all expected in that window. Ordered by expected_arrival ASC.

    Args:
      date_from/date_to: YYYY-MM-DD window on expected_arrival.
      include: "items" to embed line items (with pending qty).
      limit: override (default 1 without a range, 50 with a range; max 300).
    """
    return _call("GET", "transit/upcoming", params={
        "date_from": date_from, "date_to": date_to,
        "include": _csv(include), "limit": limit,
    })


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

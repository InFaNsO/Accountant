"""Which permission each chat tool needs, and which ones change data.

Every tool the model can see is listed here as (module, action). The module
names match ``auth_service.MODULES`` and the actions match the columns on
``user_permissions``, so a user's chat abilities are exactly their app
abilities — nothing is granted by prompt text.

Two pseudo-modules:
  "*"     — needs the action on *every* module (or god). Used by query_sql,
            which can read anything.
  "self"  — the user's own data (reminders, saved notes). Always allowed.

WRITE_ACTIONS decide which calls pause for confirmation in the UI. A tool
missing from TOOL_POLICY is refused and fails the startup check, so a new
tool in mcp_server.py can never appear in chat unnoticed.
"""

# Actions that change something and therefore need the user to confirm.
WRITE_ACTIONS = {"create", "edit", "delete", "locks_edit", "schedule"}

TOOL_POLICY = {
    # ── Clients ──────────────────────────────────────────────────────────
    "search_clients":               ("clients", "view"),
    "get_all_clients_summary":      ("clients", "financials"),
    "get_client_details":           ("clients", "view"),
    "get_client_invoices":          ("clients", "view"),
    "get_client_companies":         ("clients", "view"),
    "get_client_full":              ("clients", "view"),
    "get_client_product_breakdown": ("clients", "view"),
    "clients_by_region":            ("clients", "view"),
    "get_client_regions":           ("clients", "view"),
    "clients_bulk":                 ("clients", "view"),
    # Balances, ledgers and anything that exposes money owed.
    "get_client_ledger":            ("clients", "financials"),
    "get_client_ledger_json":       ("clients", "financials"),
    "get_company_ledger":           ("clients", "financials"),
    "clients_outstanding":          ("clients", "financials"),
    "get_business_stats":           ("clients", "financials"),
    "create_client":                ("clients", "create"),
    "create_company":               ("clients", "create"),
    "update_client":                ("clients", "edit"),
    "update_company":               ("clients", "edit"),
    # Recalculates and persists every balance for the client.
    "reconcile_client_payments":    ("clients", "edit"),
    "delete_client":                ("clients", "delete"),
    "delete_company":               ("clients", "delete"),
    "get_client_lock_status":       ("clients", "locks_view"),
    "set_client_lock":              ("clients", "locks_edit"),

    # ── Invoices ─────────────────────────────────────────────────────────
    "get_recent_invoices":          ("invoices", "view"),
    "get_overdue_invoices":         ("invoices", "view"),
    "get_invoice_details":          ("invoices", "view"),
    "invoices_bulk":                ("invoices", "view"),
    "sales_by_client":              ("invoices", "view"),
    "create_invoice":               ("invoices", "create"),
    "update_invoice_status":        ("invoices", "edit"),
    "delete_invoice":               ("invoices", "delete"),

    # ── Payments ─────────────────────────────────────────────────────────
    "get_recent_payments":          ("payments", "view"),
    "payments_bulk":                ("payments", "view"),
    "record_payment":               ("payments", "create"),
    "add_ledger_entry":             ("payments", "create"),
    "set_payment_opening_balance":  ("payments", "edit"),
    "delete_payment":               ("payments", "delete"),

    # ── Products, stock and tallies (tallies run on products perms) ───────
    "list_categories":                 ("products", "view"),
    "search_products":                 ("products", "view"),
    "get_stock_summary":               ("products", "view"),
    "get_low_stock_alerts":            ("products", "view"),
    "get_product_stock_history":       ("products", "view"),
    "get_sub_product_stock_history":   ("products", "view"),
    "get_product_stock_history_by_sub": ("products", "view"),
    "get_category_products":           ("products", "view"),
    "products_snapshot":               ("products", "view"),
    "products_stock_history_bulk":     ("products", "view"),
    "products_sales_velocity":         ("products", "view"),
    "list_stock_tallies":              ("products", "view"),
    "get_stock_tally_detail":          ("products", "view"),
    "create_category":                 ("products", "create"),
    "create_product":                  ("products", "create"),
    "create_eco_range":                ("products", "create"),
    "create_sub_product":              ("products", "create"),
    "create_stock_tally":              ("products", "create"),
    "update_category":                 ("products", "edit"),
    "update_product":                  ("products", "edit"),
    "update_sub_product":              ("products", "edit"),
    "adjust_stock":                    ("products", "edit"),
    "product_stock_action":            ("products", "edit"),
    "update_tally_item_count":         ("products", "edit"),
    "apply_stock_tally":               ("products", "edit"),
    "delete_category":                 ("products", "delete"),
    "delete_product":                  ("products", "delete"),
    "delete_sub_product":              ("products", "delete"),

    # ── Suppliers ────────────────────────────────────────────────────────
    "get_suppliers":                ("suppliers", "view"),
    "create_supplier":              ("suppliers", "create"),
    "update_supplier":              ("suppliers", "edit"),
    "delete_supplier":              ("suppliers", "delete"),

    # ── Production / purchase orders ─────────────────────────────────────
    "get_purchase_orders":          ("production", "view"),
    "get_purchase_order_details":   ("production", "view"),
    "purchase_orders_bulk":         ("production", "view"),
    "supply_pipeline":              ("production", "view"),
    "create_production_order":      ("production", "create"),
    "update_production_order":      ("production", "edit"),
    "close_production_order":       ("production", "edit"),
    "delete_production_order":      ("production", "delete"),

    # ── Transit / dispatches ─────────────────────────────────────────────
    "get_dispatches":               ("transit", "view"),
    "get_dispatch_details":         ("transit", "view"),
    "dispatches_bulk":              ("transit", "view"),
    "get_received_transit":         ("transit", "view"),
    "get_upcoming_transit":         ("transit", "view"),
    "create_dispatch":              ("transit", "create"),
    "receive_dispatch_items":       ("transit", "edit"),
    "delete_dispatch":              ("transit", "delete"),

    # ── Palm purchases ───────────────────────────────────────────────────
    "list_palm_purchases":          ("palm_purchase", "view"),
    "get_palm_purchase":            ("palm_purchase", "view"),
    "palm_purchases_bulk":          ("palm_purchase", "view"),
    "create_palm_purchase":         ("palm_purchase", "create"),
    "delete_palm_purchase":         ("palm_purchase", "delete"),

    # ── Chat-local tools (not from mcp_server) ───────────────────────────
    # Free-form SQL sees every table, so it needs view on every module.
    "describe_schema":              ("*", "view"),
    "query_sql":                    ("*", "view"),
    # The user's own inbox and schedule — not business data.
    "save_to_inbox":                ("self", "schedule"),
}


def is_write(tool_name):
    """True when calling this tool changes something and needs confirmation."""
    entry = TOOL_POLICY.get(tool_name)
    return bool(entry) and entry[1] in WRITE_ACTIONS

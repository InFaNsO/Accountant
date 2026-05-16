import os
import json
from datetime import date
import anthropic
from ..database import get_db
from ..services import client_service, payment_service

_client = None


def _get_client():
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")
        _client = anthropic.Anthropic(api_key=key)
    return _client


TOOLS = [
    {
        "name": "search_clients",
        "description": "Search for clients by name. Use this first when the user mentions a client by name to find their ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Client name or partial name to search for"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_client_details",
        "description": "Get full details and current balance for a specific client.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer", "description": "The client ID"}
            },
            "required": ["client_id"],
        },
    },
    {
        "name": "get_client_invoices",
        "description": "Get all invoices for a specific client, including amounts, dates, and payment status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer", "description": "The client ID"}
            },
            "required": ["client_id"],
        },
    },
    {
        "name": "get_all_clients_summary",
        "description": "Get a summary of all clients with their current balances. Use for questions like 'who owes me the most?' or 'list all clients'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_overdue_invoices",
        "description": "Get all invoices that are past their due date and not yet paid or cancelled.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_business_stats",
        "description": "Get overall business statistics: total revenue, outstanding balance, client count, overdue count.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_payment",
        "description": (
            "Record a payment from a client into the system. "
            "IMPORTANT: Only call this tool after the user has explicitly confirmed "
            "(said 'yes', 'confirm', 'go ahead', etc.). Always state what you are about "
            "to record and ask for confirmation before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client_id":    {"type": "integer", "description": "The client ID"},
                "amount":       {"type": "number",  "description": "Payment amount in INR"},
                "payment_date": {"type": "string",  "description": "Date in YYYY-MM-DD format"},
                "method": {
                    "type": "string",
                    "enum": ["cash", "bank_transfer", "cheque", "upi", "other"],
                    "description": "Payment method",
                },
                "invoice_id":   {"type": "integer", "description": "Optional: invoice ID to allocate the payment to"},
                "reference":    {"type": "string",  "description": "Optional reference number or note"},
            },
            "required": ["client_id", "amount", "payment_date", "method"],
        },
    },
]


def _execute_tool(name, tool_input, user):
    """Execute a tool call and return a JSON string result."""
    db = get_db()

    if name == "search_clients":
        q = f"%{tool_input.get('query', '').lower()}%"
        rows = db.execute(
            "SELECT id, name, company FROM clients WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(company,'')) LIKE ? ORDER BY name LIMIT 10",
            (q, q),
        ).fetchall()
        if not rows:
            return json.dumps({"results": [], "message": "No clients found matching that name."})
        return json.dumps({"results": [{"id": r["id"], "name": r["name"], "company": r["company"] or ""} for r in rows]})

    if name == "get_client_details":
        c = client_service.get_client(tool_input["client_id"])
        if not c:
            return json.dumps({"error": "Client not found."})
        balance = client_service.get_client_balance(tool_input["client_id"])
        invoices = client_service.get_client_invoices(tool_input["client_id"])
        total_invoiced = sum(i["total"] for i in invoices if i["status"] != "cancelled")
        paid_count   = sum(1 for i in invoices if i["status"] == "paid")
        pending_count = sum(1 for i in invoices if i["status"] in ("issued", "partial"))
        return json.dumps({
            "id": c["id"], "name": c["name"], "company": c["company"] or "",
            "email": c["email"] or "", "phone": c["phone"] or "",
            "balance_inr": round(balance, 2),
            "balance_note": "client owes this amount" if balance < 0 else ("client has credit" if balance > 0 else "fully settled"),
            "total_invoices": len(invoices),
            "paid_invoices": paid_count,
            "pending_invoices": pending_count,
            "total_invoiced_inr": round(total_invoiced, 2),
        })

    if name == "get_client_invoices":
        today_str = date.today().isoformat()
        invoices = client_service.get_client_invoices(tool_input["client_id"])
        result = []
        for inv in invoices:
            overdue = bool(inv["due_date"] and inv["due_date"] < today_str and inv["status"] not in ("paid", "cancelled"))
            result.append({
                "id": inv["id"],
                "invoice_number": inv["invoice_number"],
                "issue_date": inv["issue_date"],
                "due_date": inv["due_date"] or "—",
                "total_inr": round(inv["total"], 2),
                "paid_inr": round(inv["amount_paid"], 2),
                "remaining_inr": round(inv["total"] - inv["amount_paid"], 2),
                "status": inv["status"],
                "overdue": overdue,
            })
        return json.dumps({"invoices": result, "count": len(result)})

    if name == "get_all_clients_summary":
        clients = client_service.get_all_clients()
        result = []
        for c in clients:
            ob      = float(c["opening_balance"] or 0)
            ob_paid = float(c["ob_paid"] or 0)
            if ob >= 0:
                ob_remaining = max(ob - ob_paid, 0)
                ob_excess    = max(ob_paid - ob, 0)
            else:
                ob_remaining = 0
                ob_excess    = ob_paid + abs(ob)
            balance = -(ob_remaining + float(c["invoice_balance"] or 0)) + ob_excess
            result.append({
                "id": c["id"], "name": c["name"], "company": c["company"] or "",
                "balance_inr": round(balance, 2),
                "status": "owes" if balance < 0 else ("credit" if balance > 0 else "settled"),
            })
        result.sort(key=lambda x: x["balance_inr"])   # most owed first (negative = owes)
        return json.dumps({"clients": result, "count": len(result)})

    if name == "get_overdue_invoices":
        today_str = date.today().isoformat()
        rows = db.execute(
            """SELECT i.id, i.invoice_number, i.issue_date, i.due_date,
                      i.total, i.amount_paid, i.status, c.name AS client_name
               FROM invoices i
               JOIN clients c ON c.id = i.client_id
               WHERE i.due_date < ? AND i.status NOT IN ('paid','cancelled')
               ORDER BY i.due_date""",
            (today_str,),
        ).fetchall()
        result = []
        for r in rows:
            days = (date.today() - date.fromisoformat(r["due_date"])).days
            result.append({
                "id": r["id"],
                "invoice_number": r["invoice_number"],
                "client_name": r["client_name"],
                "due_date": r["due_date"],
                "total_inr": round(r["total"], 2),
                "remaining_inr": round(r["total"] - r["amount_paid"], 2),
                "days_overdue": days,
            })
        return json.dumps({"overdue_invoices": result, "count": len(result)})

    if name == "get_business_stats":
        stats = payment_service.get_dashboard_stats()
        return json.dumps({
            "total_revenue_inr":  round(float(stats.get("total_revenue", 0)), 2),
            "outstanding_inr":    round(float(stats.get("outstanding", 0)), 2),
            "total_clients":      stats.get("total_clients", 0),
            "overdue_invoice_count": stats.get("overdue_count", 0),
        })

    if name == "record_payment":
        if not user.has_permission("payments", "create"):
            return json.dumps({"error": "You don't have permission to record payments."})
        data = {
            "client_id":    str(tool_input["client_id"]),
            "amount":       str(tool_input["amount"]),
            "payment_date": tool_input["payment_date"],
            "method":       tool_input.get("method", "cash"),
            "reference":    tool_input.get("reference", ""),
            "notes":        "Recorded via AI assistant",
            "invoice_id":   str(tool_input["invoice_id"]) if tool_input.get("invoice_id") else "",
        }
        try:
            payment_service.create_payment(data)
            return json.dumps({"success": True, "message": f"Payment of ₹{tool_input['amount']:,.2f} recorded successfully."})
        except Exception as e:
            return json.dumps({"error": str(e)})

    return json.dumps({"error": f"Unknown tool: {name}"})


def chat(messages, user):
    """
    Run the Claude agentic loop.
    `messages` is a list of {role, content} dicts (the full conversation so far,
    including the latest user message).
    Returns the assistant's final text response.
    """
    system = f"""You are an AI assistant embedded in Ledger, a small business accounting app used in India.

Currency is always Indian Rupees (₹ INR). Format monetary values using Indian number style (e.g. ₹1,00,000.00).

Today's date: {date.today().isoformat()}
Current user: {user.name} ({'Administrator' if user.is_god() else 'User'})

You help with:
- Answering questions about clients, invoices, payments, and business performance
- Generating summaries and insights from live business data
- Recording payments on behalf of the user

Rules:
- Always use the tools to fetch live data before answering any data-specific question. Never guess.
- Before calling record_payment, clearly tell the user what you are about to record and ask them to confirm.
- Be concise. Use bullet points or tables for lists of data.
- If a client name is ambiguous, list the matches and ask the user to clarify."""

    api_messages = list(messages)

    # Agentic loop
    for _ in range(10):   # max 10 iterations to prevent infinite loops
        response = _get_client().messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=api_messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            api_messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _execute_tool(block.name, block.input, user)
                    tool_results.append({
                        "type": "tool_use_id" if False else "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            api_messages.append({"role": "user", "content": tool_results})
            continue

        break

    return "I wasn't able to complete that request. Please try again."

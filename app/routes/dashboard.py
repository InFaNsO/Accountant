from flask import Blueprint, render_template, request
from flask_login import login_required, current_user
from ..services.payment_service import get_dashboard_stats
from ..services.product_service import get_stock_alerts
from ..services.transit_service import get_dispatches_due_soon
from datetime import date, timedelta

bp = Blueprint("dashboard", __name__)

WINDOW_LABELS = {
    "today":         "Today",
    "yesterday":     "Yesterday",
    "this_week":     "This Week",
    "last_week":     "Last Week",
    "last_15":       "Last 15 Days",
    "this_month":    "This Month",
    "last_2_months": "Last 2 Months",
    "60_days":       "60 Days",
    "90_days":       "90 Days",
    "this_year":     "This Year",
    "current_fy":    "Current FY",
    "last_fy":       "Last FY",
    "custom":        "Custom Range",
    "all":           "All Time",
}


def _fy(today):
    """Returns (current_fy_start, last_fy_start, last_fy_end) for India (Apr-Mar)."""
    m, y = today.month, today.year
    if m >= 4:
        return date(y, 4, 1), date(y - 1, 4, 1), date(y, 3, 31)
    return date(y - 1, 4, 1), date(y - 2, 4, 1), date(y - 1, 3, 31)


def resolve_window(window, custom_from="", custom_to=""):
    """Return (date_from, date_to) as date objects, or (None, None) for all-time."""
    today = date.today()

    if window == "custom" and custom_from and custom_to:
        try:
            return date.fromisoformat(custom_from), date.fromisoformat(custom_to)
        except ValueError:
            pass

    if window == "all":
        return None, None

    dow = today.weekday()            # 0=Mon … 6=Sun
    cfy, lfy_s, lfy_e = _fy(today)

    table = {
        "today":         (today,                       today),
        "yesterday":     (today - timedelta(1),        today - timedelta(1)),
        "this_week":     (today - timedelta(dow),      today),
        "last_week":     (today - timedelta(dow + 7),  today - timedelta(dow + 1)),
        "last_15":       (today - timedelta(14),       today),
        "this_month":    (today.replace(day=1),        today),
        "last_2_months": (today - timedelta(60),       today),
        "60_days":       (today - timedelta(59),       today),
        "90_days":       (today - timedelta(89),       today),
        "this_year":     (date(today.year, 1, 1),      today),
        "current_fy":    (cfy,                         today),
        "last_fy":       (lfy_s,                       lfy_e),
    }
    return table.get(window, (today.replace(day=1), today))


@bp.route("/")
@login_required
def index():
    window      = request.args.get("window", "this_month")
    custom_from = request.args.get("from", "")
    custom_to   = request.args.get("to", "")

    d_from, d_to = resolve_window(window, custom_from, custom_to)
    date_from = d_from.isoformat() if d_from else None
    date_to   = d_to.isoformat()   if d_to   else None

    dash_sections = current_user.get_dashboard_sections()
    stats         = get_dashboard_stats(date_from=date_from, date_to=date_to)
    alerts        = get_stock_alerts()
    dispatches_due_soon  = get_dispatches_due_soon(days=14)

    return render_template(
        "dashboard.html",
        stats=stats,
        alerts=alerts,
        dispatches_due_soon=dispatches_due_soon,
        dash_sections=dash_sections,
        window=window,
        window_label=WINDOW_LABELS.get(window, "Custom Range"),
        date_from=date_from or "",
        date_to=date_to or "",
        custom_from=custom_from,
        custom_to=custom_to,
    )

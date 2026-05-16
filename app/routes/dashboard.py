from flask import Blueprint, render_template
from flask_login import login_required, current_user
from ..services.payment_service import get_dashboard_stats
from ..services.product_service import get_stock_alerts
from ..services.production_service import get_pos_due_soon

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    dash_sections = current_user.get_dashboard_sections()
    stats        = get_dashboard_stats()
    alerts       = get_stock_alerts()
    pos_due_soon = get_pos_due_soon(days=14)
    return render_template("dashboard.html",
                           stats=stats,
                           alerts=alerts,
                           pos_due_soon=pos_due_soon,
                           dash_sections=dash_sections)

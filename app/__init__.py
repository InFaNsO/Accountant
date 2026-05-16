import os
from datetime import date
from flask import Flask
from .database import init_db


def _format_inr(value):
    """Format a number in Indian currency style: ₹10,00,00,000.00"""
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        num = 0.0
    negative = num < 0
    integer_part, decimal_part = f"{abs(num):.2f}".split(".")
    if len(integer_part) <= 3:
        formatted = integer_part
    else:
        last_three = integer_part[-3:]
        remaining = integer_part[:-3]
        groups = []
        while remaining:
            groups.append(remaining[-2:])
            remaining = remaining[:-2]
        groups.reverse()
        formatted = ",".join(groups) + "," + last_three
    result = f"₹{formatted}.{decimal_part}"
    return f"-{result}" if negative else result


def create_app():
    app = Flask(__name__, instance_relative_config=False)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
    app.config["DATABASE"] = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "ledger.db"
    )

    app.jinja_env.filters["inr"]       = _format_inr
    app.jinja_env.filters["enumerate"] = enumerate

    @app.context_processor
    def inject_today():
        return {"today": date.today().isoformat()}

    with app.app_context():
        init_db(app)

        from .routes.dashboard import bp as dashboard_bp
        from .routes.clients import bp as clients_bp
        from .routes.products import bp as products_bp
        from .routes.invoices import bp as invoices_bp
        from .routes.payments import bp as payments_bp
        from .routes.suppliers import bp as suppliers_bp
        from .routes.purchases import bp as purchases_bp
        from .routes.transit import bp as transit_bp

        app.register_blueprint(dashboard_bp)
        app.register_blueprint(clients_bp)
        app.register_blueprint(products_bp)
        app.register_blueprint(invoices_bp)
        app.register_blueprint(payments_bp)
        app.register_blueprint(suppliers_bp)
        app.register_blueprint(purchases_bp)
        app.register_blueprint(transit_bp)

    return app

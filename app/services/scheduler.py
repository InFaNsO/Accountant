import logging

logger = logging.getLogger(__name__)


def start_scheduler(app):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from .notification_service import check_low_stock, check_overdue_invoices, check_transit_due

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            lambda: check_overdue_invoices(app),
            "interval",
            hours=1,
            id="overdue_invoices",
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: check_low_stock(app),
            "interval",
            hours=1,
            id="low_stock",
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: check_transit_due(app),
            "interval",
            hours=6,
            id="transit_due",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("Notification scheduler started")
    except Exception as e:
        logger.error("Failed to start scheduler: %s", e)

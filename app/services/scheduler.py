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
        # Reminders and scheduled reports. Every worker runs this; claiming a
        # due task is a compare-and-set, so exactly one of them fires it.
        from ..chat.scheduled import dispatch_due, purge_old
        scheduler.add_job(
            lambda: dispatch_due(app),
            "interval",
            minutes=1,
            id="chat_scheduled_tasks",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: purge_old(app),
            "interval",
            hours=24,
            id="chat_scheduled_purge",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("Notification and task scheduler started")
    except Exception as e:
        logger.error("Failed to start scheduler: %s", e)

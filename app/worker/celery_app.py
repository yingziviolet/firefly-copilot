from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "firefly_copilot",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks_ingest", "app.worker.tasks_sentinel"],
)

celery_app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    beat_schedule={
        "send-weekly-digest": {
            "task": "app.worker.tasks_sentinel.send_weekly_digest",
            "schedule": crontab(hour=9, minute=0, day_of_week=1),
        },
    },
)

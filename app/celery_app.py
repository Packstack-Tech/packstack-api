import os
import ssl

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
USE_SSL = REDIS_URL.startswith("rediss://")

celery_app = Celery(
    "packstack",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks.enrich_product"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

if USE_SSL:
    ssl_opts = {"ssl_cert_reqs": ssl.CERT_NONE}
    celery_app.conf.update(
        broker_use_ssl=ssl_opts,
        redis_backend_use_ssl=ssl_opts,
    )

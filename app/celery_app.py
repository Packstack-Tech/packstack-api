import os
import ssl

import sentry_sdk
from celery import Celery

SENTRY_DSN = "https://d794ed7cb82ca2c3e95cf1ceb96c3bd9@o313912.ingest.us.sentry.io/4510944527515648"
DEVELOPMENT = os.getenv("DEVELOPMENT", 0)

if not DEVELOPMENT:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=True,
    )

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
    worker_concurrency=1,
    worker_max_tasks_per_child=10,
    worker_max_memory_per_child=250_000,  # 250MB
)

if USE_SSL:
    ssl_opts = {"ssl_cert_reqs": ssl.CERT_NONE}
    celery_app.conf.update(
        broker_use_ssl=ssl_opts,
        redis_backend_use_ssl=ssl_opts,
    )

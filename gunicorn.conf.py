# gunicorn.conf.py
import os

worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("THREADS", "8"))
keepalive = 30
timeout = 60
graceful_timeout = 30
max_requests = 2000
max_requests_jitter = 200
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "warning")

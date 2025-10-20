# gunicorn.conf.py — seguro para 512 MB
workers = 1
threads = int(os.environ.get("THREADS", "2"))
worker_class = "gthread"
preload_app = False
max_requests = 800
max_requests_jitter = 200
timeout = 60
keepalive = 15

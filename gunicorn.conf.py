# gunicorn.conf.py
import os

# MUY IMPORTANTE: 1 solo proceso
workers = 1

# Usamos modelo "gthread":
#   - 1 proceso
#   - varias threads para concurrencia
worker_class = "gthread"

# Cuántas threads simultáneas puede servir ese único proceso
# (8 está bien para una app pequeña y evita más procesos = menos RAM)
threads = int(os.environ.get("THREADS", "8"))

# Mantener conexiones un rato vivas (keep-alive)
keepalive = 30

# No dejar una request colgada para siempre
timeout = 60
graceful_timeout = 30

# Reciclar el worker cada X peticiones para evitar fugas lentas de memoria
max_requests = 2000
max_requests_jitter = 200

# Menos ruido en logs = menos CPU
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "warning")

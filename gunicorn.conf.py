import multiprocessing
import os

bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")
workers = int(os.getenv("GUNICORN_WORKERS", str(min(4, multiprocessing.cpu_count() * 2 + 1))))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = 60
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
capture_output = True

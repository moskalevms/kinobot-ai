# src/monitoring.py
import logging
from prometheus_client import Counter, Histogram, generate_latest
from flask import Response

# Метрики для Prometheus
REQUEST_COUNT = Counter('requests_total', 'Total requests')
REQUEST_DURATION = Histogram('request_duration_seconds', 'Request duration')

@app.route('/metrics')
def metrics():
    return Response(generate_latest(), mimetype='text/plain')
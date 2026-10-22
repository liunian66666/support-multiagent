"""Shared in-process realtime hub for HTTP handlers and WebSocket endpoints."""

import logging

from metrics import Metrics
from realtime import Hub

ws_hub = Hub()
metrics = Metrics()
realtime_log = logging.getLogger("business.realtime")

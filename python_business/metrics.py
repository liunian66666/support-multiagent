"""Small Prometheus text exporter for the business-layer metric names."""

import threading
from collections import defaultdict


BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


def labels(**values):
    def escape(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return "{" + ",".join(f'{key}="{escape(value)}"' for key, value in values.items()) + "}"


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.http_counts = defaultdict(int)
        self.http_durations = {}
        self.agent_counts = defaultdict(int)
        self.agent_durations = {}

    @staticmethod
    def observe_histogram(target, key, duration):
        item = target.setdefault(key, [0, 0.0, [0] * len(BUCKETS)])
        item[0] += 1
        item[1] += duration
        for index, bound in enumerate(BUCKETS):
            if duration <= bound:
                item[2][index] += 1

    def observe_http(self, method, path, status, duration):
        with self.lock:
            self.http_counts[method, path, status] += 1
            self.observe_histogram(self.http_durations, (method, path), duration)

    def observe_agent(self, endpoint, success, duration):
        with self.lock:
            self.agent_counts[endpoint, "success" if success else "error"] += 1
            self.observe_histogram(self.agent_durations, (endpoint,), duration)

    @staticmethod
    def histogram(lines, name, samples, field_names):
        for fields, (count, total, buckets) in sorted(samples.items()):
            attrs = dict(zip(field_names, fields))
            for bound, seen in zip(BUCKETS, buckets):
                lines.append(f"{name}_bucket{labels(**attrs, le=bound)} "
                             f"{seen}")
            lines.append(f"{name}_bucket{labels(**attrs, le='+Inf')} {count}")
            lines.append(f"{name}_sum{labels(**attrs)} {total:.9f}")
            lines.append(f"{name}_count{labels(**attrs)} {count}")

    def render(self, *, ws_connections, active_sessions, queue=None, pool=None):
        with self.lock:
            http_counts = dict(self.http_counts)
            http_durations = {key: (item[0], item[1], tuple(item[2]))
                              for key, item in self.http_durations.items()}
            agent_counts = dict(self.agent_counts)
            agent_durations = {key: (item[0], item[1], tuple(item[2]))
                               for key, item in self.agent_durations.items()}
        lines = [
            "# HELP cs_http_requests_total Total number of HTTP requests",
            "# TYPE cs_http_requests_total counter",
        ]
        for (method, path, status), count in sorted(http_counts.items()):
            lines.append(f"cs_http_requests_total{labels(method=method, path=path, status=status)} {count}")
        lines += [
            "# HELP cs_http_request_duration_seconds HTTP request duration in seconds",
            "# TYPE cs_http_request_duration_seconds histogram",
        ]
        self.histogram(lines, "cs_http_request_duration_seconds", http_durations,
                       ("method", "path"))
        lines += [
            "# HELP cs_websocket_connections Current active WebSocket connections",
            "# TYPE cs_websocket_connections gauge",
            f"cs_websocket_connections {ws_connections}",
            "# HELP cs_active_sessions Current active user sessions",
            "# TYPE cs_active_sessions gauge",
            f"cs_active_sessions {active_sessions}",
            "# HELP cs_agent_calls_total Total number of Agent calls",
            "# TYPE cs_agent_calls_total counter",
        ]
        for (endpoint, status), count in sorted(agent_counts.items()):
            lines.append(f"cs_agent_calls_total{labels(endpoint=endpoint, status=status)} {count}")
        lines += [
            "# HELP cs_agent_call_duration_seconds Agent call duration in seconds",
            "# TYPE cs_agent_call_duration_seconds histogram",
        ]
        self.histogram(lines, "cs_agent_call_duration_seconds", agent_durations,
                       ("endpoint",))
        lines += self.queue_lines(queue or {})
        lines += self.pool_lines(pool or {})
        return "\n".join(lines) + "\n"

    @staticmethod
    def queue_lines(queue):
        """排队指标。``oldest_queued_seconds`` 是唯一值得告警的一行。

        采集失败时（DB 抖动）不写这几行，而不是写 0：把"没采到"报成 0 会让告警哑掉。
        """
        if not queue:
            return []
        return [
            "# HELP cs_chat_turns_queued Chat turns waiting for a worker",
            "# TYPE cs_chat_turns_queued gauge",
            f"cs_chat_turns_queued {queue.get('queued', 0)}",
            "# HELP cs_chat_turns_running Chat turns currently held by a worker",
            "# TYPE cs_chat_turns_running gauge",
            f"cs_chat_turns_running {queue.get('running', 0)}",
            "# HELP cs_chat_turns_stale_running Running turns whose lease already expired",
            "# TYPE cs_chat_turns_stale_running gauge",
            f"cs_chat_turns_stale_running {queue.get('stale_running', 0)}",
            "# HELP cs_chat_turns_retried Turns being worked on that have been claimed more than once",
            "# TYPE cs_chat_turns_retried gauge",
            f"cs_chat_turns_retried {queue.get('retried', 0)}",
            "# HELP cs_chat_turn_oldest_queued_seconds Age of the oldest queued turn",
            "# TYPE cs_chat_turn_oldest_queued_seconds gauge",
            f"cs_chat_turn_oldest_queued_seconds {queue.get('oldest_queued_seconds', 0.0)}",
        ]

    @staticmethod
    def pool_lines(pool):
        if not pool:
            return []
        return [
            "# HELP cs_mysql_pool_open MySQL connections opened by this process",
            "# TYPE cs_mysql_pool_open gauge",
            f"cs_mysql_pool_open {pool.get('open', 0)}",
            "# HELP cs_mysql_pool_free MySQL idle connections kept in the pool",
            "# TYPE cs_mysql_pool_free gauge",
            f"cs_mysql_pool_free {pool.get('free', 0)}",
            "# HELP cs_mysql_pool_overflow_total Connections opened beyond the pool size",
            "# TYPE cs_mysql_pool_overflow_total counter",
            f"cs_mysql_pool_overflow_total {pool.get('overflow', 0)}",
        ]

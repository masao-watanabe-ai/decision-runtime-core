"""Minimal Prometheus-compatible in-memory metrics registry.

Exports counters and summaries in Prometheus text format (version 0.0.4).
No external dependencies — pure Python with threading.Lock for safety.

Designed for observability only: never influences business logic.
Call reset() between tests for isolation.
"""
from __future__ import annotations

import threading
from typing import Optional

_lock = threading.Lock()

# Counter values: "metric_name" or "metric_name{k=\"v\",...}" → float
_counters: dict[str, float] = {}

# Summary values: name → {"count": N, "sum": N}
_summaries: dict[str, dict[str, float]] = {}

# Ordered declarations for consistent output
_declarations: list[tuple[str, str, str]] = []  # (name, type, help)


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def increment(name: str, labels: Optional[dict[str, str]] = None) -> None:
    """Increment a counter by 1."""
    key = name + _format_labels(labels or {})
    with _lock:
        _counters[key] = _counters.get(key, 0.0) + 1.0


def observe(name: str, value: float) -> None:
    """Record a value for a summary metric (count + sum, no quantiles)."""
    with _lock:
        if name not in _summaries:
            _summaries[name] = {"count": 0.0, "sum": 0.0}
        _summaries[name]["count"] += 1.0
        _summaries[name]["sum"] += value


def get_counter(name: str, labels: Optional[dict[str, str]] = None) -> float:
    """Return current counter value — for testing."""
    key = name + _format_labels(labels or {})
    with _lock:
        return _counters.get(key, 0.0)


def get_summary(name: str) -> dict[str, float]:
    """Return current summary dict — for testing."""
    with _lock:
        return dict(_summaries.get(name, {"count": 0.0, "sum": 0.0}))


def prometheus_text() -> str:
    """Return all metrics in Prometheus text exposition format (version 0.0.4)."""
    lines: list[str] = []

    with _lock:
        for metric_name, type_, help_text in _declarations:
            lines.append(f"# HELP {metric_name} {help_text}")
            lines.append(f"# TYPE {metric_name} {type_}")

            if type_ == "counter":
                for key in sorted(_counters):
                    base = key.split("{")[0]
                    if base == metric_name:
                        lines.append(f"{key} {_counters[key]:g}")
            elif type_ == "summary":
                data = _summaries.get(metric_name, {"count": 0.0, "sum": 0.0})
                lines.append(f"{metric_name}_count {data['count']:g}")
                lines.append(f"{metric_name}_sum {data['sum']:.6f}")

    return "\n".join(lines) + "\n" if lines else "\n"


def reset() -> None:
    """Reset all metric values. For test isolation only."""
    with _lock:
        _counters.clear()
        _summaries.clear()


# ------------------------------------------------------------------ #
# Metric declarations (output order matches declaration order)        #
# ------------------------------------------------------------------ #

_declarations.extend([
    (
        "decision_evaluate_total",
        "counter",
        "Total decision evaluations by outcome status",
    ),
    (
        "decision_evaluate_duration_seconds",
        "summary",
        "Duration of decision evaluations in seconds",
    ),
    (
        "human_gate_action_total",
        "counter",
        "Human gate approve and reject actions",
    ),
    (
        "ledger_append_total",
        "counter",
        "Ledger append operations by result status",
    ),
])

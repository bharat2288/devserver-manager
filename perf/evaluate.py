"""
DSM Performance Evaluation Harness (IMMUTABLE)

Measures API response times for the Dev Server Manager.
This file must NOT be modified during experiments — it is the fixed evaluation standard.

Usage:
    python perf/evaluate.py              # Print JSON to stdout
    python perf/evaluate.py --pretty     # Pretty-print JSON
    python perf/evaluate.py --csv        # Print single CSV row
"""

import json
import statistics
import sys
import time
from datetime import datetime, timezone

import httpx

# =============================================================================
# Configuration (FIXED — do not modify)
# =============================================================================

BASE_URL = "http://127.0.0.1:9000"
WARMUP_REQUESTS = 3
SAMPLE_REQUESTS = 10
REQUEST_TIMEOUT = 30.0  # seconds (git/status can be slow)

# Endpoint weights — reflect polling frequency impact on perceived performance
# projects + groups = every 3s (65%), git = every 30s (25%), logs = every 2s when selected (10%)
ENDPOINTS = {
    "/api/projects": {"weight": 0.50, "label": "projects"},
    "/api/groups": {"weight": 0.15, "label": "groups"},
    "/api/git/status": {"weight": 0.25, "label": "git"},
}

# logs endpoint uses a dynamic project ID — added at runtime
LOGS_WEIGHT = 0.10


# =============================================================================
# Measurement Functions
# =============================================================================

def percentile(data: list[float], p: float) -> float:
    """Compute the p-th percentile of a sorted list."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def measure_endpoint(client: httpx.Client, path: str, n: int = SAMPLE_REQUESTS) -> dict:
    """
    Measure response time for an endpoint.

    Returns dict with: mean_ms, p50_ms, p95_ms, min_ms, max_ms, n, errors
    """
    times = []
    errors = 0

    for _ in range(n):
        try:
            start = time.perf_counter()
            response = client.get(path, timeout=REQUEST_TIMEOUT)
            elapsed_ms = (time.perf_counter() - start) * 1000

            if response.status_code == 200:
                times.append(elapsed_ms)
            else:
                errors += 1
        except (httpx.RequestError, httpx.TimeoutException):
            errors += 1

    if not times:
        return {
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "n": n,
            "errors": errors,
        }

    return {
        "mean_ms": round(statistics.mean(times), 2),
        "p50_ms": round(percentile(times, 50), 2),
        "p95_ms": round(percentile(times, 95), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "n": n,
        "errors": errors,
    }


def get_first_project_id(client: httpx.Client) -> str | None:
    """Fetch the first project ID for log endpoint testing."""
    try:
        response = client.get("/api/projects", timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            projects = response.json()
            if projects:
                return projects[0].get("id")
    except (httpx.RequestError, httpx.TimeoutException):
        pass
    return None


def run_evaluation() -> dict:
    """
    Run the full evaluation suite.

    Returns a dict with:
        - timestamp
        - composite_ms (weighted p95 sum — THE metric)
        - endpoints (per-endpoint stats)
    """
    with httpx.Client(base_url=BASE_URL) as client:
        # Health check — verify DSM is running
        try:
            resp = client.get("/", timeout=5.0)
            if resp.status_code != 200:
                print(f"ERROR: DSM returned status {resp.status_code}", file=sys.stderr)
                sys.exit(1)
        except (httpx.RequestError, httpx.TimeoutException) as e:
            print(f"ERROR: DSM not reachable at {BASE_URL}: {e}", file=sys.stderr)
            sys.exit(1)

        # Warmup — prime caches, JIT, etc.
        for _ in range(WARMUP_REQUESTS):
            try:
                client.get("/api/projects", timeout=REQUEST_TIMEOUT)
            except (httpx.RequestError, httpx.TimeoutException):
                pass

        # Measure each fixed endpoint
        results = {}
        for path, config in ENDPOINTS.items():
            stats = measure_endpoint(client, path)
            results[config["label"]] = stats

        # Measure logs endpoint (dynamic project ID)
        project_id = get_first_project_id(client)
        if project_id:
            logs_stats = measure_endpoint(client, f"/api/projects/{project_id}/logs")
            results["logs"] = logs_stats
            logs_p95 = logs_stats["p95_ms"]
        else:
            # No projects — skip logs, redistribute weight
            results["logs"] = {
                "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0,
                "min_ms": 0.0, "max_ms": 0.0, "n": 0, "errors": 0,
                "note": "skipped — no projects found",
            }
            logs_p95 = 0.0

        # Compute composite metric: weighted sum of p95 values
        composite = 0.0
        for path, config in ENDPOINTS.items():
            label = config["label"]
            composite += config["weight"] * results[label]["p95_ms"]
        composite += LOGS_WEIGHT * logs_p95

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "composite_ms": round(composite, 2),
            "endpoints": results,
        }


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    result = run_evaluation()

    if "--csv" in sys.argv:
        # Single CSV row for quick inspection
        ep = result["endpoints"]
        print(
            f"{result['composite_ms']}\t"
            f"{ep['projects']['p95_ms']}\t"
            f"{ep['groups']['p95_ms']}\t"
            f"{ep['git']['p95_ms']}\t"
            f"{ep['logs']['p95_ms']}"
        )
    elif "--pretty" in sys.argv:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()

"""
DSM Performance Experiment Orchestrator

Runs the evaluation harness, compares against the last successful result,
and applies keep/discard logic following the autoresearch pattern.

Usage:
    python perf/run_experiment.py "description of what changed"
    python perf/run_experiment.py --baseline   # Record baseline (no comparison)
"""

import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# =============================================================================
# Configuration
# =============================================================================

PERF_DIR = Path(__file__).parent
RESULTS_FILE = PERF_DIR / "results.tsv"
BASELINE_FILE = PERF_DIR / "baseline.json"
EVALUATE_SCRIPT = PERF_DIR / "evaluate.py"

# Files that experiments can modify (for git operations)
EXPERIMENT_FILES = ["main.py", "index.html"]

# Keep threshold: experiment must improve composite by at least this fraction
KEEP_THRESHOLD = 0.01  # 1% improvement required

# Wait for uvicorn --reload to pick up changes
RELOAD_WAIT_SECONDS = 4


# =============================================================================
# Results File Management
# =============================================================================

TSV_HEADER = [
    "commit", "timestamp", "composite_ms",
    "projects_p95", "groups_p95", "git_p95", "logs_p95",
    "duration_s", "status", "description",
]


def ensure_results_file():
    """Create results.tsv with header if it doesn't exist."""
    if not RESULTS_FILE.exists():
        with open(RESULTS_FILE, "w", newline="") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(TSV_HEADER)


def get_last_kept_composite() -> float | None:
    """Read the last kept (or baseline) composite_ms from results.tsv."""
    if not RESULTS_FILE.exists():
        return None

    last_composite = None
    with open(RESULTS_FILE, "r") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            if row.get("status") in ("kept", "baseline"):
                try:
                    last_composite = float(row["composite_ms"])
                except (ValueError, KeyError):
                    pass
    return last_composite


def append_result(row: dict):
    """Append a result row to results.tsv."""
    ensure_results_file()
    with open(RESULTS_FILE, "a", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow([row.get(col, "") for col in TSV_HEADER])


# =============================================================================
# Git Operations
# =============================================================================

def get_current_commit() -> str:
    """Get short hash of current git commit."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def commit_experiment(description: str) -> str:
    """Stage and commit experiment files. Returns commit hash."""
    try:
        # Stage only experiment files that have changes
        subprocess.run(
            ["git", "add"] + EXPERIMENT_FILES,
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["git", "commit", "-m", f"perf: {description}"],
            capture_output=True, timeout=10,
        )
        return get_current_commit()
    except Exception as e:
        print(f"WARNING: Git commit failed: {e}", file=sys.stderr)
        return "no-commit"


def discard_changes():
    """Revert experiment files to last committed state."""
    try:
        subprocess.run(
            ["git", "checkout", "--"] + EXPERIMENT_FILES,
            capture_output=True, timeout=5,
        )
    except Exception as e:
        print(f"WARNING: Git checkout failed: {e}", file=sys.stderr)


# =============================================================================
# Evaluation
# =============================================================================

def run_evaluation() -> tuple[dict, float]:
    """Run evaluate.py and return (parsed JSON result, duration in seconds)."""
    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(EVALUATE_SCRIPT)],
            capture_output=True, text=True, timeout=300,
        )
        duration = time.time() - start
        if result.returncode != 0:
            print(f"ERROR: evaluate.py failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
        return json.loads(result.stdout), round(duration, 1)
    except json.JSONDecodeError:
        print(f"ERROR: evaluate.py output is not valid JSON:\n{result.stdout}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERROR: evaluate.py timed out after 5 minutes", file=sys.stderr)
        sys.exit(1)


# =============================================================================
# Main Logic
# =============================================================================

def record_baseline():
    """Capture baseline metrics — no comparison, no git operations."""
    print("Capturing baseline metrics...")
    print(f"  Waiting {RELOAD_WAIT_SECONDS}s for server to stabilize...")
    time.sleep(RELOAD_WAIT_SECONDS)

    metrics, duration = run_evaluation()
    ep = metrics["endpoints"]

    # Save baseline.json
    with open(BASELINE_FILE, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"  Saved baseline to {BASELINE_FILE}")

    # Append to results.tsv
    ensure_results_file()
    append_result({
        "commit": get_current_commit(),
        "timestamp": metrics["timestamp"],
        "composite_ms": metrics["composite_ms"],
        "projects_p95": ep["projects"]["p95_ms"],
        "groups_p95": ep["groups"]["p95_ms"],
        "git_p95": ep["git"]["p95_ms"],
        "logs_p95": ep["logs"]["p95_ms"],
        "duration_s": duration,
        "status": "baseline",
        "description": "initial measurement",
    })

    print(f"\n  BASELINE: composite = {metrics['composite_ms']:.1f} ms ({duration:.0f}s)")
    print(f"    projects p95 = {ep['projects']['p95_ms']:.1f} ms")
    print(f"    groups   p95 = {ep['groups']['p95_ms']:.1f} ms")
    print(f"    git      p95 = {ep['git']['p95_ms']:.1f} ms")
    print(f"    logs     p95 = {ep['logs']['p95_ms']:.1f} ms")
    print(f"\n  Results: {RESULTS_FILE}")
    print(f"  Dashboard: http://127.0.0.1:9000/perf")


def run_experiment(description: str):
    """Run a single experiment: measure, compare, keep/discard."""
    print(f"Experiment: {description}")
    print(f"  Waiting {RELOAD_WAIT_SECONDS}s for uvicorn reload...")
    time.sleep(RELOAD_WAIT_SECONDS)

    # Get last successful composite for comparison
    last_composite = get_last_kept_composite()
    if last_composite is None:
        print("ERROR: No baseline found. Run with --baseline first.", file=sys.stderr)
        sys.exit(1)

    print(f"  Last kept composite: {last_composite:.1f} ms")
    print(f"  Running evaluation ({10} requests × {4} endpoints)...")

    metrics, duration = run_evaluation()
    ep = metrics["endpoints"]
    current = metrics["composite_ms"]

    # Compute improvement
    improvement = (last_composite - current) / last_composite
    pct = improvement * 100

    print(f"\n  Current composite:  {current:.1f} ms ({duration:.0f}s eval)")
    print(f"  Improvement:        {pct:+.1f}%")

    # Decision
    if improvement >= KEEP_THRESHOLD:
        status = "kept"
        commit_hash = commit_experiment(description)
        print(f"\n  KEPT (>= {KEEP_THRESHOLD*100:.0f}% improvement)")
        print(f"  Committed as: {commit_hash}")
    else:
        status = "discarded"
        commit_hash = get_current_commit()
        discard_changes()
        if improvement > 0:
            print(f"\n  DISCARDED (improvement {pct:.1f}% below {KEEP_THRESHOLD*100:.0f}% threshold)")
        else:
            print(f"\n  DISCARDED (regression or no improvement: {pct:+.1f}%)")

    # Log to results.tsv
    append_result({
        "commit": commit_hash,
        "timestamp": metrics["timestamp"],
        "composite_ms": current,
        "projects_p95": ep["projects"]["p95_ms"],
        "groups_p95": ep["groups"]["p95_ms"],
        "git_p95": ep["git"]["p95_ms"],
        "logs_p95": ep["logs"]["p95_ms"],
        "duration_s": duration,
        "status": status,
        "description": description,
    })

    # Per-endpoint breakdown
    print(f"\n  Endpoint breakdown:")
    print(f"    projects p95 = {ep['projects']['p95_ms']:.1f} ms")
    print(f"    groups   p95 = {ep['groups']['p95_ms']:.1f} ms")
    print(f"    git      p95 = {ep['git']['p95_ms']:.1f} ms")
    print(f"    logs     p95 = {ep['logs']['p95_ms']:.1f} ms")
    print(f"\n  Dashboard: http://127.0.0.1:9000/perf")

    return status


# =============================================================================
# CLI
# =============================================================================

def main():
    if "--baseline" in sys.argv:
        record_baseline()
    elif len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        print("Usage:")
        print('  python perf/run_experiment.py "description of change"')
        print("  python perf/run_experiment.py --baseline")
        sys.exit(1)
    else:
        description = sys.argv[1]
        run_experiment(description)


if __name__ == "__main__":
    main()

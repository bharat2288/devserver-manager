# DSM Performance Experiments — Agent Instructions

Adapted from [karpathy/autoresearch](https://github.com/karpathy/autoresearch).
Real-time dashboard: **http://127.0.0.1:9000/perf**

## Rules

1. You may ONLY modify: `main.py`, `index.html`
2. You may NOT modify anything in `perf/` (measurement is immutable)
3. You may NOT change the API contract (endpoint paths, response shapes)
4. You may NOT remove functionality (all buttons, groups, DnD must still work)
5. You may NOT add new Python dependencies (stdlib + existing deps only)
6. Each experiment should be a single, isolated optimization
7. Wait for uvicorn `--reload` to pick up changes before measuring

## How to Run

```bash
# 1. Capture baseline (once, before any experiments)
python perf/run_experiment.py --baseline

# 2. Make your code change to main.py and/or index.html

# 3. Run the experiment (waits 4s for reload, measures, decides keep/discard)
python perf/run_experiment.py "description of what you changed"

# 4. Check the dashboard at http://127.0.0.1:9000/perf
```

## Metric

**Composite = weighted p95 latency** (lower is better)

```
composite = 0.50 × projects_p95
          + 0.15 × groups_p95
          + 0.25 × git_p95
          + 0.10 × logs_p95
```

**Keep threshold**: >= 1% improvement over last kept result.

## Bottlenecks (ordered by expected impact)

### B1: Serial port checking in list_projects() [CRITICAL]

- **Location**: `main.py` — `list_projects()` calls `get_project_status()` per project
- **Problem**: `is_port_in_use()` has 100ms socket timeout. 23 projects × 100ms = 2.3s worst case
- **Fix ideas**:
  - **B1a**: Reduce socket timeout from 100ms to 20ms (localhost responds in <1ms when port is open)
  - **B1b**: Use `concurrent.futures.ThreadPoolExecutor` to check all ports in parallel
  - **B1c**: Cache port status with short TTL (~500ms)
  - **B1d**: Combine B1a + B1b for maximum effect

### B2: load_config() reads disk on every API call [HIGH]

- **Location**: `main.py` — `load_config()` reads `projects.json` from disk
- **Problem**: Called by every `load_projects()` and `load_groups()` invocation. Multiple disk reads per poll cycle.
- **Fix ideas**:
  - **B2a**: In-memory cache with file mtime check (only re-read if file changed)
  - **B2b**: Cache with manual invalidation (clear on save_config)

### B3: Serial git status — 115 subprocess calls [HIGH]

- **Location**: `main.py` — `get_git_status()` loops over 23 projects, 5 git commands each
- **Problem**: Sequential subprocess calls dominate the /api/git/status response time
- **Fix ideas**:
  - **B3a**: `concurrent.futures.ThreadPoolExecutor` to parallelize across projects
  - **B3b**: Combine multiple git commands into a single shell call per project
  - **B3c**: Add `--no-optional-locks` to git commands to avoid contention

### B4: Blocking I/O in async endpoints [MEDIUM]

- **Location**: `main.py` — endpoints use `async def` but call sync `is_port_in_use()`, `subprocess.run()`, `load_config()`
- **Problem**: Sync calls in async handlers block the event loop
- **Fix ideas**:
  - **B4a**: Change `async def list_projects` → `def list_projects` (FastAPI auto-runs sync endpoints in a thread pool)
  - **B4b**: Use `asyncio.to_thread()` or `loop.run_in_executor()` for blocking calls

### B5: Full DOM rebuild every 3 seconds [MEDIUM — frontend]

- **Location**: `index.html` — `render()` sets `container.innerHTML` on every poll
- **Problem**: Rebuilds entire DOM tree even when nothing changed
- **Fix ideas**:
  - **B5a**: Hash the API response JSON; skip `render()` if unchanged
  - **B5b**: Diff the new HTML against current DOM and patch only changes
  - **B5c**: Use `requestAnimationFrame` to batch renders

### B6: Redundant groups fetch [LOW]

- **Location**: `index.html` — `fetchData()` fetches `/api/projects` AND `/api/groups` every 3s
- **Problem**: Groups rarely change; fetching them every 3s is wasteful
- **Fix ideas**:
  - **B6a**: Fetch groups once on load, re-fetch only after group mutation operations
  - **B6b**: Combined `/api/dashboard` endpoint returning projects + groups in one call

### B7: escapeHtml() creates DOM element per call [LOW]

- **Location**: `index.html` — creates `div` element, sets `textContent`, reads `innerHTML`
- **Problem**: DOM element creation is expensive when called 50+ times per render
- **Fix ideas**:
  - **B7a**: Replace with regex-based string escaping (replace &, <, >, ", ')

## Recommended Experiment Order

1. **B1a** — Reduce socket timeout 100ms → 20ms (1-line change, big impact)
2. **B1b** — Parallel port checks with ThreadPoolExecutor
3. **B2a** — Config caching with mtime check
4. **B3a** — Parallel git status with ThreadPoolExecutor
5. **B4a** — Convert async def → def for blocking endpoints
6. **B5a** — Frontend JSON change detection
7. **B6a** — Fetch groups once, not every 3s
8. **B7a** — Regex escapeHtml

## Results Log

All results are logged to `perf/results.tsv`. Each row records:

```
commit  timestamp  composite_ms  projects_p95  groups_p95  git_p95  logs_p95  status  description
```

Status values: `baseline`, `kept`, `discarded`

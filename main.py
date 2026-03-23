"""
Dev Server Manager — FastAPI Backend

A local dashboard for managing multiple development servers.
Provides REST API for project CRUD, process start/stop, log retrieval,
and server group management with auto-start on startup.
"""

import asyncio
import collections
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# =============================================================================
# Logging Configuration
# =============================================================================

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "server_manager.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("devserver-manager")

# =============================================================================
# Configuration
# =============================================================================

CONFIG_DIR = Path(__file__).parent / "config"
PROJECTS_FILE = CONFIG_DIR / "projects.json"
RUNNING_FILE = CONFIG_DIR / "running.json"  # Persisted runtime state
MANAGER_PORT = 9000
LOG_BUFFER_SIZE = 100
AUTO_START_DELAY_SECONDS = 2  # Delay between sequential auto-starts

# fnm (Fast Node Manager) — detect Node.js installation dynamically
def _detect_fnm_node_path() -> Optional[Path]:
    """Find the latest fnm-managed Node.js installation."""
    fnm_versions_dir = Path.home() / "AppData/Roaming/fnm/node-versions"
    if not fnm_versions_dir.exists():
        return None
    # Sort version directories descending so we pick the latest
    for version_dir in sorted(fnm_versions_dir.iterdir(), reverse=True):
        installation = version_dir / "installation"
        if installation.exists() and (installation / "node.exe").exists():
            return installation
    return None

FNM_NODE_PATH = _detect_fnm_node_path()

# =============================================================================
# Data Models
# =============================================================================

class ProjectCreate(BaseModel):
    """Request model for creating a new project."""
    name: str
    directory: str
    start_command: str
    port: int
    url: Optional[str] = None  # Optional URL for "Open in Browser"
    graph_declaration: Optional[str] = None  # Declaration filename in Impact Graph
    group: Optional[str] = None  # Group ID (defaults to first group)

class ProjectUpdate(BaseModel):
    """Request model for updating a project."""
    name: Optional[str] = None
    directory: Optional[str] = None
    start_command: Optional[str] = None
    port: Optional[int] = None
    url: Optional[str] = None
    graph_declaration: Optional[str] = None
    group: Optional[str] = None

class Project(BaseModel):
    """Full project model with all fields."""
    id: str
    name: str
    directory: str
    start_command: str
    port: int
    created_at: str
    url: Optional[str] = None
    graph_declaration: Optional[str] = None
    group: str = "active"
    position: int = 0

class GroupCreate(BaseModel):
    """Request model for creating a new group."""
    name: str
    auto_start: bool = False
    collapsed: bool = False

class GroupUpdate(BaseModel):
    """Request model for updating a group."""
    name: Optional[str] = None
    auto_start: Optional[bool] = None
    collapsed: Optional[bool] = None

class ProjectMove(BaseModel):
    """Request model for moving a project to a group at a position."""
    group: str
    position: int

# =============================================================================
# Runtime State
# =============================================================================

# Tracks running processes started by this manager
# Key: project_id, Value: dict with process, pid, started_at, logs, log_thread
# Note: 'process' and 'log_thread' are runtime-only, not persisted
running_processes: dict = {}

# Lock for thread-safe access to running_processes
# This prevents race conditions when multiple requests access/modify the dict
running_processes_lock = threading.Lock()

# =============================================================================
# Persistence Layer
# =============================================================================

def load_config() -> dict:
    """Load the full config (groups + projects) from JSON file."""
    if not PROJECTS_FILE.exists():
        return {"groups": [], "projects": []}
    with open(PROJECTS_FILE, "r") as f:
        data = json.load(f)
    # Migration: if no groups key, add defaults and assign all projects to "active"
    if "groups" not in data:
        data["groups"] = [
            {"id": "active", "name": "Active", "auto_start": True, "collapsed": False, "position": 0},
            {"id": "standby", "name": "Standby", "auto_start": True, "collapsed": False, "position": 1},
            {"id": "archive", "name": "Archive", "auto_start": False, "collapsed": True, "position": 2},
        ]
        for i, project in enumerate(data.get("projects", [])):
            if "group" not in project:
                project["group"] = "active"
            if "position" not in project:
                project["position"] = i
    return data

def save_config(config: dict) -> None:
    """Save the full config (groups + projects) to JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROJECTS_FILE, "w") as f:
        json.dump(config, f, indent=2)

def load_projects() -> list[dict]:
    """Load projects from JSON file."""
    return load_config().get("projects", [])

def save_projects(projects: list[dict]) -> None:
    """Save projects to JSON file, preserving groups."""
    config = load_config()
    config["projects"] = projects
    save_config(config)

def load_groups() -> list[dict]:
    """Load groups from JSON file."""
    return load_config().get("groups", [])

def save_groups(groups: list[dict]) -> None:
    """Save groups to JSON file, preserving projects."""
    config = load_config()
    config["groups"] = groups
    save_config(config)

def load_running_state() -> dict:
    """Load persisted running state (PIDs) from disk."""
    if not RUNNING_FILE.exists():
        return {}
    try:
        with open(RUNNING_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_running_state() -> None:
    """Persist running state (PIDs, log file paths) to disk. Thread-safe."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Only save the serializable parts (pid, started_at, log_file_path)
    # Take a snapshot while holding the lock to avoid iteration issues
    with running_processes_lock:
        state = {
            project_id: {
                "pid": info["pid"],
                "started_at": info["started_at"],
                "log_file_path": info.get("log_file_path", ""),
            }
            for project_id, info in running_processes.items()
        }
    try:
        with open(RUNNING_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except IOError as e:
        logger.error(f"Failed to save running state: {e}")

def restore_running_state() -> None:
    """
    On startup, restore running_processes from persisted state.
    Only restores entries where the PID is still alive.
    Also restores log streaming from log files.
    """
    saved_state = load_running_state()
    restored_count = 0
    processes_to_restore = []

    # First pass: identify which processes are still alive
    for project_id, info in saved_state.items():
        pid = info.get("pid")
        if pid and is_process_running(pid):
            processes_to_restore.append((project_id, info))
            logger.info(f"Restored running process for project {project_id} (PID: {pid})")
        else:
            logger.info(f"Discarded stale process entry for project {project_id} (PID: {pid})")

    # Second pass: restore processes and start log streaming threads
    for project_id, info in processes_to_restore:
        log_buffer = collections.deque(maxlen=LOG_BUFFER_SIZE)
        log_file_path = info.get("log_file_path", "")

        # Start log streaming thread if log file exists
        log_thread = None
        if log_file_path and Path(log_file_path).exists():
            log_thread = threading.Thread(
                target=stream_logs_from_file,
                args=(Path(log_file_path), log_buffer, project_id, True),  # start_from_end=True
                daemon=True,
                name=f"log-stream-{project_id}"
            )
            log_thread.start()

        with running_processes_lock:
            running_processes[project_id] = {
                "process": None,  # We don't have the Popen object
                "pid": info.get("pid"),
                "started_at": info.get("started_at", ""),
                "logs": log_buffer,
                "log_thread": log_thread,
                "log_file": None,  # File was opened by the previous run
                "log_file_path": log_file_path,
            }
        restored_count += 1

    logger.info(f"Restored {restored_count} running process(es) from saved state")
    # Clean up the persisted file to match actual state
    save_running_state()

def get_project_by_id(project_id: str) -> Optional[dict]:
    """Find a project by its ID."""
    projects = load_projects()
    for project in projects:
        if project["id"] == project_id:
            return project
    return None

# =============================================================================
# Port & Process Utilities
# =============================================================================

def is_port_in_use(port: int) -> bool:
    """Check if a port is currently in use. Handles socket errors gracefully."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.02)  # 20ms timeout - localhost responds in <1ms when open
            return s.connect_ex(("127.0.0.1", port)) == 0
    except (OSError, socket.error) as e:
        # Socket creation or operation failed - assume port is not in use
        # but log the error for debugging
        logger.warning(f"Socket error checking port {port}: {e}")
        return False


def get_process_using_port(port: int) -> Optional[psutil.Process]:
    """Find the process that is listening on a given port."""
    logger.info(f"[PORT LOOKUP] Searching for process on port {port}")

    # First try psutil (faster, but may not have PID on Windows without admin)
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                logger.info(f"[PORT LOOKUP] psutil found port {port}, pid={conn.pid}")
                if conn.pid is not None:
                    try:
                        return psutil.Process(conn.pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                        logger.warning(f"[PORT LOOKUP] psutil process lookup failed for PID {conn.pid}: {e}")
    except psutil.AccessDenied:
        logger.warning(f"[PORT LOOKUP] psutil.net_connections access denied for port {port}")

    # Fallback: parse netstat output (works without admin on Windows)
    logger.info(f"[PORT LOOKUP] Trying netstat fallback for port {port}")
    try:
        result = subprocess.run(
            "netstat -ano",
            capture_output=True,
            text=True,
            timeout=5,
            shell=True,
        )
        logger.info(f"[PORT LOOKUP] netstat returned code {result.returncode}, stdout len={len(result.stdout)}, stderr={result.stderr[:100] if result.stderr else 'none'}")

        found_listening = False
        for line in result.stdout.splitlines():
            if "LISTENING" in line and str(port) in line:
                found_listening = True
                logger.info(f"[PORT LOOKUP] Candidate line: {line.strip()}")
                parts = line.split()
                if len(parts) >= 5:
                    local_addr = parts[1]
                    logger.info(f"[PORT LOOKUP] local_addr={local_addr}, checking endswith :{port}")
                    if local_addr.endswith(f":{port}"):
                        try:
                            pid = int(parts[-1])
                            logger.info(f"[PORT LOOKUP] SUCCESS: port {port} -> PID {pid}")
                            return psutil.Process(pid)
                        except ValueError as e:
                            logger.warning(f"[PORT LOOKUP] Failed to parse PID from netstat: {e}")
                        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                            logger.warning(f"[PORT LOOKUP] Failed to get process {parts[-1]}: {e}")

        if not found_listening:
            logger.info(f"[PORT LOOKUP] No LISTENING lines found containing {port}")

    except Exception as e:
        logger.warning(f"[PORT LOOKUP] Netstat fallback failed for port {port}: {type(e).__name__}: {e}")

    logger.warning(f"[PORT LOOKUP] Could not find process for port {port}")
    return None


def kill_process_tree(pid: int) -> dict:
    """
    Kill a process and all its children (tree kill).

    Returns:
        Dict with 'killed' (list of PIDs) and 'failed' (list of PIDs)
    """
    killed = []
    failed = []

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)

        # Terminate children first, then parent
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass

        try:
            parent.terminate()
        except psutil.NoSuchProcess:
            pass

        # Wait briefly for graceful shutdown
        all_procs = [parent] + children
        gone, alive = psutil.wait_procs(all_procs, timeout=3)

        for p in gone:
            killed.append(p.pid)

        # Force kill any survivors
        for p in alive:
            try:
                p.kill()
                killed.append(p.pid)
                logger.warning(f"Force killed process {p.pid}")
            except psutil.NoSuchProcess:
                killed.append(p.pid)  # Already dead
            except psutil.AccessDenied:
                failed.append(p.pid)
                logger.error(f"Access denied killing process {p.pid}")

    except psutil.NoSuchProcess:
        logger.info(f"Process {pid} already terminated")
    except psutil.AccessDenied:
        failed.append(pid)
        logger.error(f"Access denied terminating process {pid}")

    return {"killed": killed, "failed": failed}

def is_process_running(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False

def get_project_status(project: dict) -> str:
    """
    Determine the display status of a project. Thread-safe.

    Returns:
        "running" - We started it and it's running
        "external" - Port is in use by something we didn't start
        "stopped" - Not running
    """
    project_id = project["id"]
    port = project["port"]

    # Check if we have a tracked process for this project
    with running_processes_lock:
        if project_id in running_processes:
            pid = running_processes[project_id]["pid"]
            if is_process_running(pid):
                return "running"
            else:
                # Process died, clean up our tracking
                del running_processes[project_id]
                logger.info(f"Cleaned up dead process for project {project_id} (PID: {pid})")

    # Check if port is in use by something external
    if is_port_in_use(port):
        return "external"

    return "stopped"

# =============================================================================
# Log Streaming
# =============================================================================

def stream_logs_from_file(
    log_file_path: Path,
    log_buffer: collections.deque,
    project_id: str,
    start_from_end: bool = False
) -> None:
    """
    Background thread function to tail a log file and buffer lines.
    Runs until the project is stopped (checks if project is still in running_processes).

    Args:
        log_file_path: Path to the log file to tail
        log_buffer: Deque to append log lines to
        project_id: Project ID for logging purposes
        start_from_end: If True, seek to end of file first (for restored processes)
    """
    import time

    try:
        # Wait for log file to exist
        wait_count = 0
        while not log_file_path.exists() and wait_count < 50:  # 5 second timeout
            time.sleep(0.1)
            wait_count += 1

        if not log_file_path.exists():
            logger.warning(f"Log file not created for project {project_id}")
            return

        with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
            if start_from_end:
                # For restored processes, read the last N lines first then tail
                # Read entire file to get last LOG_BUFFER_SIZE lines
                f.seek(0)
                all_lines = f.readlines()
                recent_lines = all_lines[-LOG_BUFFER_SIZE:] if len(all_lines) > LOG_BUFFER_SIZE else all_lines
                for line in recent_lines:
                    log_buffer.append(line.rstrip())
                # Now positioned at end of file for tailing

            while True:
                # Check if project is still running
                with running_processes_lock:
                    if project_id not in running_processes:
                        logger.debug(f"Log streaming stopped - project {project_id} no longer running")
                        break

                # Read any new lines
                line = f.readline()
                if line:
                    log_buffer.append(line.rstrip())
                else:
                    # No new data, wait a bit before checking again
                    time.sleep(0.1)

        logger.debug(f"Log streaming ended for project {project_id}")

    except Exception as e:
        logger.error(f"Error streaming logs for project {project_id}: {type(e).__name__}: {e}")

# =============================================================================
# Process Management
# =============================================================================

def start_project_process(project: dict) -> dict:
    """
    Start the server process for a project. Thread-safe.

    Returns:
        Dict with pid and started_at on success

    Raises:
        HTTPException on failure
    """
    project_id = project["id"]
    project_name = project.get("name", project_id)

    # Check if already running (thread-safe)
    with running_processes_lock:
        if project_id in running_processes:
            if is_process_running(running_processes[project_id]["pid"]):
                raise HTTPException(status_code=400, detail="Project is already running")
            else:
                # Clean up stale entry
                del running_processes[project_id]
                logger.info(f"Cleaned up stale entry for project {project_name}")

    # Check if port is already in use
    if is_port_in_use(project["port"]):
        raise HTTPException(
            status_code=400,
            detail=f"Port {project['port']} is already in use"
        )

    # Validate directory exists
    project_dir = Path(project["directory"])
    if not project_dir.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Directory does not exist: {project['directory']}"
        )

    try:
        # Start the process
        # shell=True needed for Windows commands like npm, also handles command parsing
        # CREATE_NEW_PROCESS_GROUP allows us to terminate the process tree

        # Build environment with fnm node path for npm commands
        env = os.environ.copy()
        if FNM_NODE_PATH and FNM_NODE_PATH.exists():
            env["PATH"] = str(FNM_NODE_PATH) + os.pathsep + env.get("PATH", "")

        logger.info(f"Starting project {project_name}: {project['start_command']}")

        # Windows process detachment strategy:
        # 1. CREATE_NEW_PROCESS_GROUP: Allows clean termination via taskkill
        # 2. CREATE_BREAKAWAY_FROM_JOB: Attempts to break from parent's job object
        # 3. CREATE_NO_WINDOW: Prevents console windows from popping up
        #
        # The key fix for persistence is file-based logging instead of pipes.
        # When stdout goes to a file instead of PIPE, the child process doesn't die
        # when the parent's pipe handle closes.
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP |
            subprocess.CREATE_BREAKAWAY_FROM_JOB |
            subprocess.CREATE_NO_WINDOW
        )

        # Create log file for this project
        project_log_dir = LOG_DIR / "projects"
        project_log_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = project_log_dir / f"{project_id}.log"

        # Open log file for writing (truncate on start)
        log_file = open(log_file_path, "w", encoding="utf-8")

        # Build command: expand relative paths like venv/Scripts/python to absolute paths
        # This is needed because shell=True with cwd doesn't resolve relative executables
        project_dir = Path(project["directory"].replace("/", "\\"))
        start_command = project["start_command"]

        # If command starts with a relative path (contains / or \ but doesn't start with drive letter)
        # expand it to absolute path based on project directory
        first_word = start_command.split()[0] if start_command else ""
        if ("/" in first_word or "\\" in first_word) and not (len(first_word) > 1 and first_word[1] == ":"):
            # It's a relative path like "venv/Scripts/python" - make it absolute
            relative_path = first_word.replace("/", "\\")
            absolute_path = project_dir / relative_path
            start_command = str(absolute_path) + start_command[len(first_word):]
            logger.debug(f"Expanded relative path: {first_word} -> {absolute_path}")

        process = subprocess.Popen(
            start_command,
            shell=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            cwd=str(project_dir),
            env=env,
        )

        # Set up log streaming from file
        log_buffer = collections.deque(maxlen=LOG_BUFFER_SIZE)
        log_thread = threading.Thread(
            target=stream_logs_from_file,
            args=(log_file_path, log_buffer, project_id),
            daemon=True,
            name=f"log-stream-{project_id}"
        )
        log_thread.start()

        started_at = datetime.utcnow().isoformat() + "Z"

        # Track the running process (thread-safe)
        with running_processes_lock:
            running_processes[project_id] = {
                "process": process,
                "pid": process.pid,
                "started_at": started_at,
                "logs": log_buffer,
                "log_thread": log_thread,
                "log_file": log_file,  # Keep handle to close on stop
                "log_file_path": str(log_file_path),
            }

        # Persist running state to survive manager restarts
        save_running_state()

        logger.info(f"Started project {project_name} with PID {process.pid}")
        return {"pid": process.pid, "started_at": started_at}

    except Exception as e:
        logger.error(f"Failed to start project {project_name}: {e}")
        raise HTTPException(status_code=500, detail="Failed to start process")

def stop_project_process(project_id: str) -> None:
    """
    Stop the server process for a project. Thread-safe.

    Terminates the process and all its children, then cleans up resources.

    Raises:
        HTTPException if project is not running
    """
    # Get process info while holding lock
    with running_processes_lock:
        if project_id not in running_processes:
            raise HTTPException(status_code=400, detail="Project is not running")
        process_info = running_processes[project_id]
        pid = process_info["pid"]
        process_obj = process_info.get("process")
        log_file = process_info.get("log_file")

    logger.info(f"Stopping project {project_id} (PID: {pid})")

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)

        # Terminate children first, then parent
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass

        parent.terminate()

        # Wait briefly for graceful shutdown
        gone, alive = psutil.wait_procs([parent] + children, timeout=3)

        # Force kill any survivors
        for p in alive:
            try:
                p.kill()
                logger.warning(f"Force killed process {p.pid}")
            except psutil.NoSuchProcess:
                pass

    except psutil.NoSuchProcess:
        logger.info(f"Process {pid} already terminated")

    # Clean up the Popen object to release resources
    if process_obj is not None:
        try:
            process_obj.wait(timeout=1)  # Reap the zombie process
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            logger.warning(f"Error waiting for process cleanup: {e}")

    # Close the log file handle
    if log_file is not None:
        try:
            log_file.close()
        except Exception as e:
            logger.warning(f"Error closing log file for project {project_id}: {e}")

    # Clean up tracking (thread-safe)
    with running_processes_lock:
        if project_id in running_processes:
            del running_processes[project_id]

    # Persist updated running state
    save_running_state()
    logger.info(f"Stopped project {project_id}")

# =============================================================================
# Auto-Start Logic
# =============================================================================

async def auto_start_groups() -> None:
    """
    Auto-start all servers in groups that have auto_start=True.
    Starts servers sequentially in position order within each group,
    with a delay between each to allow ports to bind.
    """
    config = load_config()
    groups = sorted(config.get("groups", []), key=lambda g: g.get("position", 0))
    projects = config.get("projects", [])

    auto_start_groups_list = [g for g in groups if g.get("auto_start", False)]
    if not auto_start_groups_list:
        logger.info("No groups with auto_start enabled")
        return

    total_started = 0
    total_failed = 0

    for group in auto_start_groups_list:
        group_id = group["id"]
        group_name = group["name"]

        # Get projects in this group, sorted by position
        group_projects = sorted(
            [p for p in projects if p.get("group") == group_id],
            key=lambda p: p.get("position", 0)
        )

        if not group_projects:
            logger.info(f"Auto-start group '{group_name}' has no projects, skipping")
            continue

        logger.info(f"Auto-starting {len(group_projects)} server(s) in group '{group_name}'")

        for project in group_projects:
            project_id = project["id"]
            project_name = project.get("name", project_id)

            # Skip if already running (e.g., restored from previous session)
            with running_processes_lock:
                if project_id in running_processes:
                    logger.info(f"  Skipping {project_name} — already running")
                    continue

            # Skip if port already in use
            if is_port_in_use(project["port"]):
                logger.info(f"  Skipping {project_name} — port {project['port']} already in use")
                continue

            try:
                result = start_project_process(project)
                logger.info(f"  Started {project_name} (PID: {result['pid']})")
                total_started += 1
            except Exception as e:
                logger.error(f"  Failed to auto-start {project_name}: {e}")
                total_failed += 1

            # Wait between starts so ports can bind before dependents start
            await asyncio.sleep(AUTO_START_DELAY_SECONDS)

    logger.info(f"Auto-start complete: {total_started} started, {total_failed} failed")

# =============================================================================
# FastAPI Application
# =============================================================================

_debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
app = FastAPI(
    title="Dev Server Manager", version="2.0.0",
    docs_url="/docs" if _debug else None,
    redoc_url="/redoc" if _debug else None,
    openapi_url="/openapi.json" if _debug else None,
)

@app.on_event("startup")
async def startup_event():
    """Restore running state and auto-start groups on startup."""
    logger.info("Dev Server Manager starting up...")
    restore_running_state()

    # Auto-start groups in background so the server is responsive immediately
    asyncio.create_task(auto_start_groups())

    logger.info(f"Dev Server Manager ready on port {MANAGER_PORT}")

# -----------------------------------------------------------------------------
# Static Files
# -----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the main dashboard HTML."""
    html_path = Path(__file__).parent / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Dashboard not found</h1><p>index.html is missing</p>")
    return FileResponse(html_path, media_type="text/html")

# -----------------------------------------------------------------------------
# Group CRUD
# -----------------------------------------------------------------------------

@app.get("/api/groups")
async def list_groups():
    """List all groups with project counts."""
    config = load_config()
    groups = sorted(config.get("groups", []), key=lambda g: g.get("position", 0))
    projects = config.get("projects", [])

    result = []
    for group in groups:
        group_projects = [p for p in projects if p.get("group") == group["id"]]
        result.append({
            **group,
            "project_count": len(group_projects),
        })
    return result

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    """Create a new group."""
    config = load_config()
    groups = config.get("groups", [])

    # Generate unique ID from name
    group_id = group.name.lower().replace(" ", "-")
    # Ensure unique
    existing_ids = {g["id"] for g in groups}
    if group_id in existing_ids:
        base_id = group_id
        counter = 1
        while group_id in existing_ids:
            group_id = f"{base_id}-{counter}"
            counter += 1

    # Position at the end (but before archive if it exists)
    max_position = max((g.get("position", 0) for g in groups), default=-1)
    new_group = {
        "id": group_id,
        "name": group.name,
        "auto_start": group.auto_start,
        "collapsed": group.collapsed,
        "position": max_position + 1,
    }

    groups.append(new_group)
    config["groups"] = groups
    save_config(config)

    return new_group

@app.put("/api/groups/{group_id}")
async def update_group(group_id: str, update: GroupUpdate):
    """Update a group's properties (name, auto_start, collapsed)."""
    config = load_config()
    groups = config.get("groups", [])

    for i, group in enumerate(groups):
        if group["id"] == group_id:
            if update.name is not None:
                group["name"] = update.name
            if update.auto_start is not None:
                group["auto_start"] = update.auto_start
            if update.collapsed is not None:
                group["collapsed"] = update.collapsed
            groups[i] = group
            config["groups"] = groups
            save_config(config)
            return group

    raise HTTPException(status_code=404, detail="Group not found")

@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str):
    """Delete a group. Moves any projects in it to the last remaining group."""
    config = load_config()
    groups = config.get("groups", [])
    projects = config.get("projects", [])

    if len(groups) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last group")

    # Find the group to delete
    group_to_delete = None
    for g in groups:
        if g["id"] == group_id:
            group_to_delete = g
            break

    if not group_to_delete:
        raise HTTPException(status_code=404, detail="Group not found")

    # Find a fallback group (the last one that isn't being deleted)
    remaining_groups = [g for g in groups if g["id"] != group_id]
    fallback_group = remaining_groups[-1]

    # Move projects from deleted group to fallback
    max_position = max(
        (p.get("position", 0) for p in projects if p.get("group") == fallback_group["id"]),
        default=-1
    )
    for project in projects:
        if project.get("group") == group_id:
            project["group"] = fallback_group["id"]
            max_position += 1
            project["position"] = max_position

    # Remove the group
    config["groups"] = remaining_groups
    config["projects"] = projects

    # Re-normalize group positions
    for i, g in enumerate(sorted(config["groups"], key=lambda x: x.get("position", 0))):
        g["position"] = i

    save_config(config)
    return {"status": "deleted", "projects_moved_to": fallback_group["id"]}

@app.put("/api/groups/reorder")
async def reorder_groups(group_ids: list[str]):
    """Reorder groups by providing the ordered list of group IDs."""
    config = load_config()
    groups = config.get("groups", [])

    group_map = {g["id"]: g for g in groups}

    # Validate all IDs exist
    for gid in group_ids:
        if gid not in group_map:
            raise HTTPException(status_code=400, detail=f"Unknown group ID: {gid}")

    # Reorder
    for i, gid in enumerate(group_ids):
        group_map[gid]["position"] = i

    config["groups"] = list(group_map.values())
    save_config(config)
    return {"status": "reordered"}

# -----------------------------------------------------------------------------
# Project CRUD
# -----------------------------------------------------------------------------

@app.get("/api/projects")
async def list_projects():
    """
    List all projects with their current status, grouped and ordered. Thread-safe.

    Returns list of projects, each with an added 'status' field.
    """
    projects = load_projects()
    result = []
    for project in projects:
        project_with_status = project.copy()
        project_with_status["status"] = get_project_status(project)

        # Include started_at if running (thread-safe access)
        with running_processes_lock:
            if project["id"] in running_processes:
                project_with_status["started_at"] = running_processes[project["id"]]["started_at"]

        result.append(project_with_status)
    return result

@app.post("/api/projects")
async def create_project(project: ProjectCreate):
    """Create a new project configuration."""
    config = load_config()
    projects = config.get("projects", [])
    groups = config.get("groups", [])

    # Generate unique ID
    new_id = uuid.uuid4().hex[:8]

    # Default to first group if not specified
    target_group = project.group
    if not target_group and groups:
        target_group = groups[0]["id"]
    elif not target_group:
        target_group = "active"

    # Position at end of target group
    max_position = max(
        (p.get("position", 0) for p in projects if p.get("group") == target_group),
        default=-1
    )

    new_project = {
        "id": new_id,
        "name": project.name,
        "directory": project.directory,
        "start_command": project.start_command,
        "port": project.port,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "group": target_group,
        "position": max_position + 1,
    }
    if project.url:
        new_project["url"] = project.url
    if project.graph_declaration:
        new_project["graph_declaration"] = project.graph_declaration

    projects.append(new_project)
    config["projects"] = projects
    save_config(config)

    return new_project

@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, update: ProjectUpdate):
    """Update an existing project configuration."""
    config = load_config()
    projects = config.get("projects", [])

    for i, project in enumerate(projects):
        if project["id"] == project_id:
            # Apply updates
            if update.name is not None:
                project["name"] = update.name
            if update.directory is not None:
                project["directory"] = update.directory
            if update.start_command is not None:
                project["start_command"] = update.start_command
            if update.port is not None:
                project["port"] = update.port
            if update.url is not None:
                project["url"] = update.url
            if update.graph_declaration is not None:
                if update.graph_declaration:
                    project["graph_declaration"] = update.graph_declaration
                else:
                    project.pop("graph_declaration", None)
            if update.group is not None:
                project["group"] = update.group

            projects[i] = project
            config["projects"] = projects
            save_config(config)
            return project

    raise HTTPException(status_code=404, detail="Project not found")

@app.put("/api/projects/{project_id}/move")
async def move_project(project_id: str, move: ProjectMove):
    """Move a project to a different group and/or position."""
    config = load_config()
    projects = config.get("projects", [])
    groups = config.get("groups", [])

    # Validate group exists
    group_ids = {g["id"] for g in groups}
    if move.group not in group_ids:
        raise HTTPException(status_code=400, detail=f"Unknown group: {move.group}")

    # Find the project
    project_to_move = None
    for p in projects:
        if p["id"] == project_id:
            project_to_move = p
            break

    if not project_to_move:
        raise HTTPException(status_code=404, detail="Project not found")

    old_group = project_to_move.get("group")

    # Get projects in the target group (excluding the one being moved)
    target_group_projects = sorted(
        [p for p in projects if p.get("group") == move.group and p["id"] != project_id],
        key=lambda p: p.get("position", 0)
    )

    # Insert at the requested position
    target_group_projects.insert(move.position, project_to_move)

    # Update positions for the target group
    for i, p in enumerate(target_group_projects):
        p["group"] = move.group
        p["position"] = i

    # If moved from a different group, re-normalize the old group positions
    if old_group and old_group != move.group:
        old_group_projects = sorted(
            [p for p in projects if p.get("group") == old_group and p["id"] != project_id],
            key=lambda p: p.get("position", 0)
        )
        for i, p in enumerate(old_group_projects):
            p["position"] = i

    config["projects"] = projects
    save_config(config)

    return {"status": "moved", "group": move.group, "position": move.position}

@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """Delete a project configuration. Thread-safe."""
    projects = load_projects()

    # Stop the project if running (thread-safe check)
    with running_processes_lock:
        is_running = project_id in running_processes

    if is_running:
        stop_project_process(project_id)
        logger.info(f"Stopped running project {project_id} before deletion")

    # Remove from list
    new_projects = [p for p in projects if p["id"] != project_id]

    if len(new_projects) == len(projects):
        raise HTTPException(status_code=404, detail="Project not found")

    save_projects(new_projects)
    logger.info(f"Deleted project {project_id}")
    return {"status": "deleted"}

# -----------------------------------------------------------------------------
# Process Control
# -----------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/start")
async def start_project(project_id: str):
    """Start the server for a project."""
    project = get_project_by_id(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result = start_project_process(project)
    return {"status": "started", **result}

@app.post("/api/projects/{project_id}/stop")
async def stop_project(project_id: str):
    """Stop the server for a project."""
    project = get_project_by_id(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    stop_project_process(project_id)
    return {"status": "stopped"}

@app.post("/api/projects/{project_id}/restart")
async def restart_project(project_id: str):
    """Restart the server for a project (stop then start)."""
    project = get_project_by_id(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Stop if running (ignore error if not running)
    with running_processes_lock:
        is_running = project_id in running_processes

    if is_running:
        stop_project_process(project_id)

    # Start fresh
    result = start_project_process(project)
    logger.info(f"Restarted project {project.get('name', project_id)}")
    return {"status": "restarted", **result}

# -----------------------------------------------------------------------------
# Group Batch Operations
# -----------------------------------------------------------------------------

@app.post("/api/groups/{group_id}/start-all")
async def start_all_in_group(group_id: str):
    """Start all stopped servers in a group sequentially."""
    projects = load_projects()
    group_projects = sorted(
        [p for p in projects if p.get("group") == group_id],
        key=lambda p: p.get("position", 0)
    )

    if not group_projects:
        raise HTTPException(status_code=404, detail="No projects in this group")

    started = []
    failed = []
    skipped = []

    for project in group_projects:
        project_id = project["id"]
        project_name = project.get("name", project_id)

        # Skip if already running
        with running_processes_lock:
            if project_id in running_processes:
                skipped.append(project_name)
                continue

        if is_port_in_use(project["port"]):
            skipped.append(project_name)
            continue

        try:
            start_project_process(project)
            started.append(project_name)
            # Brief delay between starts
            await asyncio.sleep(AUTO_START_DELAY_SECONDS)
        except Exception as e:
            logger.exception("Failed to start %s", project_name)
            failed.append({"name": project_name, "error": "Failed to start"})

    return {"started": started, "failed": failed, "skipped": skipped}

@app.post("/api/groups/{group_id}/stop-all")
async def stop_all_in_group(group_id: str):
    """Stop all running servers in a group."""
    projects = load_projects()
    group_projects = [p for p in projects if p.get("group") == group_id]

    stopped = []
    failed = []

    for project in group_projects:
        project_id = project["id"]
        project_name = project.get("name", project_id)

        with running_processes_lock:
            if project_id not in running_processes:
                continue

        try:
            stop_project_process(project_id)
            stopped.append(project_name)
        except Exception as e:
            logger.exception("Failed to stop %s", project_name)
            failed.append({"name": project_name, "error": "Failed to stop"})

    return {"stopped": stopped, "failed": failed}

# -----------------------------------------------------------------------------
# Port Management
# -----------------------------------------------------------------------------

def get_pid_on_port(port: int) -> Optional[int]:
    """
    Get the PID listening on a port using netstat.
    Returns just the PID, not a psutil.Process (which may fail for zombie PIDs).
    """
    try:
        result = subprocess.run(
            "netstat -ano",
            capture_output=True,
            text=True,
            timeout=5,
            shell=True,
        )
        for line in result.stdout.splitlines():
            if "LISTENING" in line:
                parts = line.split()
                if len(parts) >= 5:
                    local_addr = parts[1]
                    if local_addr.endswith(f":{port}"):
                        return int(parts[-1])
    except Exception as e:
        logger.warning(f"get_pid_on_port failed for {port}: {e}")
    return None


def force_kill_pid(pid: int) -> dict:
    """
    Force kill a PID using multiple methods.
    Returns dict with 'success' and 'method' used.
    """
    # Method 1: psutil (preferred - handles process tree)
    try:
        result = kill_process_tree(pid)
        if result["killed"] and not result["failed"]:
            return {"success": True, "method": "psutil", "details": result}
    except Exception as e:
        logger.warning(f"psutil kill failed for PID {pid}: {e}")

    # Method 2: taskkill via cmd (handles some cases psutil can't)
    try:
        result = subprocess.run(
            f'taskkill /PID {pid} /F /T',
            capture_output=True,
            text=True,
            timeout=10,
            shell=True,
        )
        if result.returncode == 0:
            return {"success": True, "method": "taskkill", "details": result.stdout}
        else:
            logger.warning(f"taskkill failed for PID {pid}: {result.stderr}")
    except Exception as e:
        logger.warning(f"taskkill exception for PID {pid}: {e}")

    # Method 3: PowerShell Stop-Process (another fallback)
    try:
        result = subprocess.run(
            f'powershell -Command "Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"',
            capture_output=True,
            text=True,
            timeout=10,
            shell=True,
        )
        # PowerShell might succeed even with non-zero return
        if "Cannot find a process" not in result.stderr:
            return {"success": True, "method": "powershell", "details": result.stdout}
    except Exception as e:
        logger.warning(f"PowerShell kill failed for PID {pid}: {e}")

    return {"success": False, "method": None, "details": "All kill methods failed"}


@app.post("/api/ports/{port}/kill")
async def kill_port(port: int):
    """
    Kill whatever process is using a port (tree kill).

    This is useful for cleaning up orphaned processes that weren't started
    by this manager, or processes whose parent was killed but children survived.

    Returns:
        Dict with status, killed PIDs, and any failures
    """
    if port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail="Invalid port number")

    # First check if this port is managed by us
    projects = load_projects()
    for project in projects:
        if project["port"] == port:
            with running_processes_lock:
                if project["id"] in running_processes:
                    # Use our normal stop which has better cleanup
                    stop_project_process(project["id"])
                    logger.info(f"Stopped managed project on port {port}")
                    return {
                        "status": "stopped",
                        "message": f"Stopped managed project: {project['name']}",
                        "killed": [running_processes.get(project["id"], {}).get("pid")],
                        "failed": []
                    }

    # Try to find and kill the process using this port
    process = get_process_using_port(port)
    if process:
        pid = process.pid
        try:
            process_name = process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = "unknown"

        logger.info(f"Killing process tree on port {port}: PID {pid} ({process_name})")
        result = kill_process_tree(pid)

        if result["failed"]:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to kill some processes: {result['failed']}. May need elevated permissions."
            )

        return {
            "status": "killed",
            "message": f"Killed process {pid} ({process_name}) and {len(result['killed']) - 1} children",
            "killed": result["killed"],
            "failed": result["failed"]
        }

    # psutil couldn't find the process - try direct PID lookup and kill
    pid = get_pid_on_port(port)
    if pid:
        logger.info(f"Attempting force kill of PID {pid} on port {port} (psutil couldn't access it)")
        kill_result = force_kill_pid(pid)

        if kill_result["success"]:
            # Verify the port is now free
            import time
            time.sleep(0.5)  # Brief wait for OS to release port
            if not is_port_in_use(port):
                return {
                    "status": "killed",
                    "message": f"Killed PID {pid} via {kill_result['method']}",
                    "killed": [pid],
                    "failed": []
                }
            else:
                # Port still in use - likely a zombie socket
                raise HTTPException(
                    status_code=500,
                    detail=f"Killed PID {pid} but port {port} is still in use. This is a zombie socket - it will clear on its own or after a system restart."
                )
        else:
            # All kill methods failed - this is a zombie/phantom process
            raise HTTPException(
                status_code=500,
                detail=f"Port {port} is held by PID {pid} which cannot be killed (zombie process). The port will clear on its own or after a system restart."
            )

    # No PID found at all
    if is_port_in_use(port):
        raise HTTPException(
            status_code=500,
            detail=f"Port {port} is in use but no process could be identified. May need elevated permissions or system restart."
        )

    return {"status": "not_in_use", "message": f"Port {port} is not in use", "killed": [], "failed": []}


@app.get("/api/ports/{port}/info")
async def get_port_info(port: int):
    """
    Get information about what's using a port.

    Returns process details if port is in use.
    """
    if port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail="Invalid port number")

    if not is_port_in_use(port):
        return {"in_use": False, "port": port}

    process = get_process_using_port(port)
    if not process:
        return {
            "in_use": True,
            "port": port,
            "process": None,
            "message": "Port in use but could not identify process"
        }

    try:
        children = process.children(recursive=True)
        return {
            "in_use": True,
            "port": port,
            "process": {
                "pid": process.pid,
                "name": process.name(),
                "cmdline": " ".join(process.cmdline()[:5]),  # First 5 args
                "children_count": len(children)
            }
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {
            "in_use": True,
            "port": port,
            "process": {"pid": process.pid},
            "error": "Process access denied or no longer exists"
        }

# -----------------------------------------------------------------------------
# Logs
# -----------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/logs")
async def get_project_logs(project_id: str, lines: int = 100):
    """
    Get recent log lines for a running project. Thread-safe.

    Args:
        project_id: The project ID
        lines: Maximum number of lines to return (default 100)

    Returns:
        List of log lines (most recent last)
    """
    project = get_project_by_id(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Thread-safe access to running_processes
    with running_processes_lock:
        if project_id not in running_processes:
            return {"logs": [], "status": "not_running"}
        # Take a snapshot of the log buffer while holding the lock
        log_buffer = running_processes[project_id]["logs"]
        log_lines = list(log_buffer)[-lines:]

    return {"logs": log_lines, "status": "running"}

# -----------------------------------------------------------------------------
# Git Health
# -----------------------------------------------------------------------------

def _run_git_command(args: list[str], cwd: str, timeout: int = 5) -> subprocess.CompletedProcess:
    """Run a git command in the given directory. Returns CompletedProcess."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _get_git_status_for_project(project: dict) -> dict:
    """Get git status info for a single project. Handles non-git dirs gracefully."""
    project_id = project["id"]
    project_name = project.get("name", project_id)
    directory = project.get("directory", "")

    result = {
        "project_id": project_id,
        "project_name": project_name,
        "is_git": False,
        "branch": None,
        "dirty_count": 0,
        "last_commit_msg": None,
        "last_commit_age": None,
        "unpushed_count": 0,
    }

    # Validate directory exists
    if not directory or not Path(directory).exists():
        return result

    try:
        # Check if it's a git repo
        check = _run_git_command(["rev-parse", "--is-inside-work-tree"], cwd=directory)
        if check.returncode != 0:
            return result
        result["is_git"] = True

        # Branch name
        branch = _run_git_command(["branch", "--show-current"], cwd=directory)
        result["branch"] = branch.stdout.strip() or "HEAD (detached)"

        # Dirty file count
        status = _run_git_command(["status", "--porcelain"], cwd=directory)
        if status.returncode == 0:
            lines = [l for l in status.stdout.splitlines() if l.strip()]
            result["dirty_count"] = len(lines)

        # Last commit message + relative age
        log = _run_git_command(
            ["log", "--oneline", "-1", "--format=%s|%ar"],
            cwd=directory,
        )
        if log.returncode == 0 and log.stdout.strip():
            parts = log.stdout.strip().rsplit("|", 1)
            result["last_commit_msg"] = parts[0] if len(parts) >= 1 else None
            result["last_commit_age"] = parts[1] if len(parts) >= 2 else None

        # Unpushed commit count (fails if no upstream configured)
        unpushed = _run_git_command(
            ["rev-list", "@{upstream}..HEAD", "--count"],
            cwd=directory,
        )
        if unpushed.returncode == 0:
            try:
                result["unpushed_count"] = int(unpushed.stdout.strip())
            except ValueError:
                result["unpushed_count"] = 0

    except subprocess.TimeoutExpired:
        logger.warning(f"Git commands timed out for project {project_name}")
    except Exception as e:
        logger.warning(f"Error getting git status for {project_name}: {e}")

    return result


@app.get("/api/git/status")
async def get_git_status():
    """Get git status for all registered projects."""
    projects = load_projects()
    results = []
    for project in projects:
        status = _get_git_status_for_project(project)
        results.append(status)
    return results


@app.post("/api/projects/{project_id}/lazygit")
async def open_lazygit(project_id: str):
    """Open lazygit in a new terminal window for the given project."""
    project = get_project_by_id(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    directory = project.get("directory", "")
    if not directory or not Path(directory).exists():
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {directory}")

    # Convert forward slashes to backslashes for Windows cmd
    win_directory = directory.replace("/", "\\")

    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "cmd", "/k", f"cd /d {win_directory} && lazygit"],
            shell=False,
        )
        logger.info(f"Opened lazygit for project {project.get('name', project_id)}")
        return {"status": "ok", "message": f"Lazygit opened for {project.get('name', project_id)}"}
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="lazygit not found. Install with: winget install lazygit")
    except Exception as e:
        logger.exception("Failed to open lazygit")
        raise HTTPException(status_code=500, detail="Failed to open lazygit")

# =============================================================================
# Performance Experiment Dashboard
# =============================================================================

PERF_DIR = Path(__file__).parent / "perf"

@app.get("/perf")
async def perf_dashboard():
    """Serve the performance experiment dashboard."""
    dashboard_path = PERF_DIR / "dashboard.html"
    if not dashboard_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))


@app.get("/api/perf/results")
async def perf_results():
    """Read perf/results.tsv and return as JSON array."""
    results_path = PERF_DIR / "results.tsv"
    if not results_path.exists():
        return []

    import csv
    rows = []
    with open(results_path, "r") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            # Convert numeric fields
            for key in ("composite_ms", "projects_p95", "groups_p95", "git_p95", "logs_p95"):
                if key in row and row[key]:
                    try:
                        row[key] = float(row[key])
                    except ValueError:
                        row[key] = 0.0
            rows.append(row)
    return rows


@app.get("/api/perf/baseline")
async def perf_baseline():
    """Return baseline metrics from perf/baseline.json."""
    baseline_path = PERF_DIR / "baseline.json"
    if not baseline_path.exists():
        raise HTTPException(status_code=404, detail="No baseline captured yet")
    return json.loads(baseline_path.read_text(encoding="utf-8"))


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"Starting Dev Server Manager on http://localhost:{MANAGER_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=MANAGER_PORT)

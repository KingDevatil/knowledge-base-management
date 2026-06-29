"""
Desktop shell for Knowledge Base Management.

This entry point opens one native desktop window that contains both local
service management controls and the existing web admin UI.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
LAUNCHER_PATH = PROJECT_ROOT / "kb_launcher.pyw"
SHELL_HTML = PROJECT_ROOT / "desktop_shell.html"
KBDATA_DIR = PROJECT_ROOT / "kbdata"
LOGS_DIR = KBDATA_DIR / "logs"
SHELL_LOG = LOGS_DIR / "desktop-shell.log"
LOCAL_GATEWAY_BASE_URL = "http://127.0.0.1:8000"


def _load_launcher_module():
    loader = importlib.machinery.SourceFileLoader("kb_launcher_module", str(LAUNCHER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Unable to load kb_launcher.pyw")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


launcher = _load_launcher_module()

# Per-service total startup timeout (seconds).  After the initial
# ``startup_delay`` the code polls ``_is_service_running`` every 0.4 s
# until the port becomes reachable or this budget is exhausted.
_SERVICE_STARTUP_TIMEOUTS: dict[str, float] = {
    "MCP Gateway": 25.0,
    "Chroma": 20.0,
}


def _check_local_port_fast(port: int, timeout: float = 0.05) -> bool:
    """Ultra-fast local port check. Returns True if port is open.

    Chroma on Windows may listen on IPv6 loopback (::1) instead of
    or in addition to IPv4 127.0.0.1, so we probe both addresses.
    """
    for host in ('127.0.0.1', '::1'):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue
    return False


class DesktopServiceManager:
    """Service control API exposed to the desktop webview."""

    def __init__(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.services = launcher.SERVICES
        self.project_root = PROJECT_ROOT
        self.kbdata_dir = KBDATA_DIR
        self.logs_dir = LOGS_DIR
        self.use_env_file = True
        self._lock = threading.RLock()
        self._logs: list[str] = []
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._log("Desktop shell initialized")

    # ---------- public API for pywebview ----------

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            for svc in self.services:
                if svc.status not in ("starting", "stopping"):
                    svc.status = "running" if self._is_service_running(svc) else "stopped"
            return {
                "adminUrl": f"{LOCAL_GATEWAY_BASE_URL}/admin",
                "healthUrl": f"{LOCAL_GATEWAY_BASE_URL}/health",
                "useEnvFile": self.use_env_file,
                "busy": self._worker is not None and self._worker.is_alive(),
                "services": [self._service_payload(svc) for svc in self.services],
                "logs": self._logs[-300:],
            }

    def set_use_env_file(self, value: bool) -> dict[str, Any]:
        with self._lock:
            self.use_env_file = bool(value)
            self._log(f"use_env_file={self.use_env_file}")
        return self.get_state()

    def start_service(self, name: str) -> dict[str, Any]:
        with self._lock:
            svc = self._service_by_name(name)
            if svc.status not in ("starting", "stopping"):
                svc.status = "starting"
                self._log(f"[UI] {name} set to starting")
        return self._run_background(f"start {name}", lambda: self._start_service_by_name(name))

    def stop_service(self, name: str) -> dict[str, Any]:
        with self._lock:
            svc = self._service_by_name(name)
            if svc.status not in ("starting", "stopping"):
                svc.status = "stopping"
                self._log(f"[UI] {name} set to stopping")
        return self._run_background(f"stop {name}", lambda: self._stop_service_by_name(name))

    def restart_service(self, name: str) -> dict[str, Any]:
        with self._lock:
            svc = self._service_by_name(name)
            if svc.status not in ("starting", "stopping"):
                svc.status = "stopping"
                self._log(f"[UI] {name} set to stopping (restart)")
        def _job() -> None:
            self._stop_service_by_name(name)
            self._start_service_by_name(name)

        return self._run_background(f"restart {name}", _job)

    def start_all(self) -> dict[str, Any]:
        ordered = ["Ollama", "Redis", "Chroma", "MinIO", "MCP Gateway"]
        with self._lock:
            for n in ordered:
                svc = self._service_by_name(n)
                if svc.status not in ("starting", "stopping"):
                    svc.status = "starting"
            self._log("[UI] All services set to starting")

        def _job() -> None:
            for name in ordered:
                if self._stopping:
                    return
                self._start_service_by_name(name)

        return self._run_background("start all", _job)

    def stop_all(self) -> dict[str, Any]:
        ordered = ["MCP Gateway", "Chroma", "MinIO", "Redis", "Ollama"]
        with self._lock:
            for n in ordered:
                svc = self._service_by_name(n)
                if svc.status not in ("starting", "stopping"):
                    svc.status = "stopping"
            self._log("[UI] All services set to stopping")

        def _job() -> None:
            for name in ordered:
                self._stop_service_by_name(name)

        return self._run_background("stop all", _job)

    def clear_logs(self) -> dict[str, Any]:
        with self._lock:
            self._logs.clear()
            self._log("Logs cleared")
        return self.get_state()

    def shutdown(self) -> None:
        self._log("Desktop shell closing; managed services left running")

    # ---------- service orchestration ----------

    def _run_background(self, label: str, job) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                self._log(f"Skipped {label}: another operation is running")
                return self.get_state()
            self._worker = threading.Thread(target=self._run_job, args=(label, job), daemon=True)
            self._worker.start()
        return self.get_state()

    def _run_job(self, label: str, job) -> None:
        self._log(f"Starting operation: {label}")
        try:
            job()
            self._log(f"Finished operation: {label}")
        except Exception as exc:
            self._log(f"Operation failed ({label}): {exc}")

    def _service_by_name(self, name: str):
        for svc in self.services:
            if svc.name == name:
                return svc
        raise ValueError(f"Unknown service: {name}")

    def _start_service_by_name(self, name: str) -> bool:
        svc = self._service_by_name(name)
        with self._lock:
            svc.status = "starting"
            svc.error_msg = ""
        ok = self._do_start_service(svc)
        with self._lock:
            svc.status = "running" if ok else "error"
        return ok

    def _stop_service_by_name(self, name: str) -> None:
        svc = self._service_by_name(name)
        with self._lock:
            svc.status = "stopping"
        self._do_stop_service(svc)
        with self._lock:
            svc.status = "stopped"
            svc.error_msg = ""

    def _do_start_service(self, svc) -> bool:
        self._log(f"--- start {svc.name} ---")

        for dep_name in svc.requires:
            dep = self._service_by_name(dep_name)
            if not self._is_service_running(dep):
                svc.error_msg = f"Dependency {dep_name} is not running"
                self._log(f"[WARN] {svc.error_msg}")
                return False

        if self._is_service_running(svc):
            self._log(f"[OK] {svc.name} already running on port {svc.port}")
            return True

        if svc.name == "Redis":
            return self._start_redis(svc)

        exe = svc.start_cmd[0] if svc.start_cmd else ""
        if exe and not self._find_exe(exe):
            svc.error_msg = f"{exe} is not installed or not in PATH"
            self._log(f"[ERROR] {svc.error_msg}")
            return False

        try:
            env = self._build_env(svc)
            cmd = list(svc.start_cmd)

            if svc.name == "MCP Gateway":
                bind_host = env.get("BIND_HOST", "127.0.0.1")
                if "--host" in cmd:
                    idx = cmd.index("--host")
                    if idx + 1 < len(cmd):
                        cmd[idx + 1] = bind_host
                cwd = str(PROJECT_ROOT / "mcp-gateway")
                log_file = open(LOGS_DIR / "gateway.log", "w", encoding="utf-8")
                stdout = log_file
            else:
                cwd = str(PROJECT_ROOT)
                log_file = None
                stdout = subprocess.DEVNULL

            self._log(f"[CMD] {' '.join(cmd)}")
            svc.process = subprocess.Popen(
                cmd,
                stdout=stdout,
                stderr=subprocess.STDOUT if svc.name == "MCP Gateway" else subprocess.DEVNULL,
                env=env,
                cwd=cwd,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            svc.pid = svc.process.pid
            svc._log_file = log_file

            total_timeout = _SERVICE_STARTUP_TIMEOUTS.get(svc.name, 10.0)
            poll_interval = 0.4
            self._log(f"[INFO] Waiting for {svc.name} to become reachable (timeout {total_timeout:.0f}s)...")
            time.sleep(min(svc.startup_delay, total_timeout))
            waited = min(svc.startup_delay, total_timeout)
            while waited < total_timeout:
                if self._is_service_running(svc):
                    self._log(f"[OK] {svc.name} started after {waited:.1f}s (PID={svc.process.pid})")
                    return True
                time.sleep(poll_interval)
                waited += poll_interval
            # Final check before giving up
            if self._is_service_running(svc):
                self._log(f"[OK] {svc.name} started after {waited:.1f}s (PID={svc.process.pid})")
                return True

            svc.error_msg = f"Process started but port {svc.port} not reachable after {waited:.0f}s"
            self._log(f"[WARN] {svc.error_msg}")
            return False
        except Exception as exc:
            svc.error_msg = f"Start failed: {exc}"
            self._log(f"[ERROR] {svc.error_msg}")
            return False

    def _start_redis(self, svc) -> bool:
        if sys.platform == "win32":
            memurai_paths = [
                os.path.expandvars(r"%ProgramFiles%\Memurai\memurai.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Memurai\memurai.exe"),
            ]
            for path in memurai_paths:
                if os.path.exists(path):
                    try:
                        subprocess.Popen(
                            [path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        time.sleep(2)
                        if launcher.check_port(6379):
                            self._log("[OK] Redis started through Memurai")
                            return True
                    except Exception as exc:
                        self._log(f"[WARN] Memurai start failed: {exc}")

        if self._find_exe("redis-server"):
            try:
                subprocess.Popen(
                    ["redis-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                time.sleep(2)
                if launcher.check_port(6379):
                    self._log("[OK] Redis started through redis-server")
                    return True
            except Exception as exc:
                self._log(f"[WARN] redis-server start failed: {exc}")

        if launcher.check_port(6379):
            self._log("[OK] Redis port 6379 is already in use")
            return True

        svc.error_msg = "Redis/Memurai is not installed or port 6379 is unavailable"
        self._log(f"[ERROR] {svc.error_msg}")
        return False

    def _do_stop_service(self, svc) -> None:
        self._log(f"--- stop {svc.name} ---")
        log_file = getattr(svc, "_log_file", None)
        if log_file:
            try:
                log_file.close()
            except Exception:
                pass
            svc._log_file = None

        if getattr(svc, "process", None) and svc.process.poll() is None:
            self._terminate_process(svc.process)
            svc.process = None
            self._log(f"[OK] {svc.name} stopped")
            return

        pid = getattr(svc, "pid", None)
        if pid:
            launcher.kill_process_by_pid(pid)
            svc.pid = None
            self._log(f"[OK] {svc.name} stopped by PID")
            return

        if svc.process_name and svc.process_name != "python":
            launcher.kill_process(svc.process_name)
            self._log(f"[OK] {svc.name} stop requested by process name")
            return

        if svc.port:
            pid_by_port = launcher.find_pid_by_port(svc.port)
            if pid_by_port:
                launcher.kill_process_by_pid(pid_by_port)
                self._log(f"[OK] {svc.name} stopped by port {svc.port}")
                return

        self._log(f"[OK] {svc.name} was not running")

    def _terminate_process(self, process: subprocess.Popen) -> None:
        try:
            process.terminate()
            for _ in range(30):
                if process.poll() is not None:
                    return
                time.sleep(0.1)
            process.kill()
        except Exception as exc:
            self._log(f"[WARN] terminate failed: {exc}")

    def _build_env(self, svc) -> dict[str, str]:
        env = os.environ.copy()
        env["KBDATA_DIR"] = str(KBDATA_DIR)

        if svc.name == "MCP Gateway":
            env.update({
                "PYTHONPATH": str(PROJECT_ROOT / "mcp-gateway" / "src"),
                "REDIS_URL": "redis://localhost:6379/0",
                "CHROMA_HOST": "localhost",
                "CHROMA_PORT": "8001",
                "OLLAMA_URL": "http://localhost:11434",
                "MINIO_ENDPOINT": "localhost:9000",
                "MINIO_ACCESS_KEY": "minioadmin",
                "MINIO_SECRET_KEY": "minioadmin",
                "MINIO_BUCKET": "kb-sources",
                "MINIO_SECURE": "false",
                "DEBUG": "true",
                "CORS_ORIGINS": "*",
                "ADMIN_ACCOUNTS_FILE": str(KBDATA_DIR / "config" / "admin_accounts.json"),
                "API_KEY_FILE": str(KBDATA_DIR / "config" / "api_keys.json"),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            if self.use_env_file:
                self._load_env_file(env)

        if svc.name == "MinIO":
            env["MINIO_ROOT_USER"] = env.get("MINIO_ROOT_USER", "minioadmin")
            env["MINIO_ROOT_PASSWORD"] = env.get("MINIO_SECRET_KEY", "minioadmin")

        return env

    def _load_env_file(self, env: dict[str, str]) -> None:
        dotenv_path = PROJECT_ROOT / "mcp-gateway" / ".env"
        if not dotenv_path.is_file():
            dotenv_path = PROJECT_ROOT / ".env"
        if not dotenv_path.is_file():
            self._log("[INFO] .env not found; using desktop defaults")
            return

        self._log(f"[INFO] loading .env: {dotenv_path}")
        for raw in dotenv_path.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\"'")
            if key:
                env[key] = val

        kbdata = env.get("KBDATA_DIR", "")
        if kbdata and not os.path.isabs(kbdata):
            env["KBDATA_DIR"] = str(PROJECT_ROOT / kbdata)

    def _is_service_running(self, svc) -> bool:
        # Use fast local port check instead of launcher.check_port
        # 300ms default. Serial port checks on stopped services
        # no longer dominate get_state latency.
        if svc.port:
            if not _check_local_port_fast(svc.port, timeout=0.05):
                return False
            # Port open - trust it for running services.
            return True
        # No port defined: try health_url, then process_name
        if svc.health_url:
            return launcher.check_url(svc.health_url, timeout=0.3)
        if svc.process_name:
            return launcher.find_process(svc.process_name)
        return False

    def _service_payload(self, svc) -> dict[str, Any]:
        return {
            "name": svc.name,
            "port": svc.port,
            "status": svc.status,
            "error": svc.error_msg,
            "urls": svc.access_urls,
            "requires": svc.requires,
        }

    def _log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self._lock:
            self._logs.append(line)
            self._logs = self._logs[-500:]
        try:
            with SHELL_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _find_exe(name: str) -> bool:
        if os.path.isabs(name) and os.path.exists(name):
            return True
        return shutil.which(name) is not None


def _show_missing_webview_error(exc: Exception) -> None:
    message = (
        "PyWebView is required for the desktop shell.\n\n"
        "Install it with:\n"
        "  python -m pip install pywebview\n\n"
        f"Original error: {exc}"
    )
    try:
        import tkinter.messagebox as mb
        mb.showerror("Desktop Shell", message)
    except Exception:
        print(message)


def main() -> None:
    try:
        import webview
    except Exception as exc:
        _show_missing_webview_error(exc)
        return

    if not SHELL_HTML.is_file():
        raise FileNotFoundError(f"Missing {SHELL_HTML}")

    manager = DesktopServiceManager()
    webview.create_window(
        "知识库管理桌面控制台",
        url=str(SHELL_HTML),
        js_api=manager,
        width=1360,
        height=860,
        min_size=(1100, 680),
        text_select=True,
    )
    storage = str(KBDATA_DIR / "webview-profile")
    try:
        webview.start(
            debug=False,
            http_server=True,
            private_mode=False,
            storage_path=storage,
        )
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()

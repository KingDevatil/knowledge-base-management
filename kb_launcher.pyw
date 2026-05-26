"""
KB Launcher — Knowledge Base Management 服务启动器
Windows GUI，支持一键启停、状态监控、报错提示、最小化到系统托盘

双击 kb_launcher.pyw 启动（无控制台窗口）
"""
from __future__ import annotations

import os
import sys
import time
import signal
import socket
import shutil
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

# ---- DPI 感知（必须在 tkinter 之前设置）----
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Per-monitor DPI
    except Exception:
        pass

# ---- tkinter ----
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont

# ---- pystray (system tray) ----
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# ---- HTTP check ----
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON_EXE = sys.executable
KBDATA_DIR = PROJECT_ROOT / "kbdata"
LOGS_DIR = KBDATA_DIR / "logs"

# Ensure kbdata directories exist
(KBDATA_DIR / "config").mkdir(parents=True, exist_ok=True)
(KBDATA_DIR / "minio").mkdir(parents=True, exist_ok=True)
(KBDATA_DIR / "chroma").mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------
class ServiceDef:
    """单个服务的定义"""
    def __init__(
        self,
        name: str,
        port: int,
        health_url: str,
        access_urls: list[str],
        start_cmd: list[str],
        stop_cmd: Optional[list[str]] = None,
        process_name: str = "",
        startup_delay: float = 2.0,
        requires: list[str] | None = None,
    ):
        self.name = name
        self.port = port
        self.health_url = health_url
        self.access_urls = access_urls
        self.start_cmd = start_cmd
        self.stop_cmd = stop_cmd or []
        self.process_name = process_name or name.lower()
        self.startup_delay = startup_delay
        self.requires = requires or []  # 依赖的其他服务 name
        self.process: Optional[subprocess.Popen] = None
        self.status: str = "stopped"   # stopped | starting | running | error
        self.error_msg: str = ""

    def __repr__(self):
        return f"ServiceDef({self.name})"


SERVICES: list[ServiceDef] = [
    ServiceDef(
        name="Ollama",
        port=11434,
        health_url="http://localhost:11434/api/tags",
        access_urls=["Ollama API: http://localhost:11434"],
        start_cmd=["ollama", "serve"],
        process_name="ollama",
        startup_delay=3.0,
    ),
    ServiceDef(
        name="Redis",
        port=6379,
        health_url="",  # 用 socket 检测端口
        access_urls=["Redis: localhost:6379"],
        start_cmd=[],
        stop_cmd=[],
        process_name="memurai",
        startup_delay=2.0,
    ),
    ServiceDef(
        name="Chroma",
        port=8001,
        health_url="http://localhost:8001/api/v2/heartbeat",
        access_urls=["Chroma API: http://localhost:8001"],
        start_cmd=["chroma", "run", "--host", "localhost", "--port", "8001", "--path", str(KBDATA_DIR / "chroma")],
        process_name="chroma",
        startup_delay=8.0,
        requires=["Redis"],
    ),
    ServiceDef(
        name="MinIO",
        port=9000,
        health_url="http://localhost:9000/minio/health/live",
        access_urls=[
            "MinIO API:    http://localhost:9000",
            "MinIO Console: http://localhost:9001",
        ],
        start_cmd=[str(PROJECT_ROOT / ("minio.exe" if sys.platform == "win32" else "minio")), "server", str(KBDATA_DIR / "minio"), "--console-address", ":9001"],
        process_name="minio",
        startup_delay=3.0,
    ),
    ServiceDef(
        name="MCP Gateway",
        port=8000,
        health_url="http://localhost:8000/health",
        access_urls=[
            "API:   http://localhost:8000",
            "Admin: http://localhost:8000/admin",
            "MCP:   http://localhost:8000/mcp (推荐)",
            "MCP:   http://localhost:8000/sse (兼容)",
        ],
        start_cmd=[
            PYTHON_EXE, "-m", "uvicorn", "src.main:app",
            "--host", "127.0.0.1", "--port", "8000",
        ],
        process_name="python",
        startup_delay=4.0,
        requires=["Ollama", "Redis", "Chroma", "MinIO"],
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def check_port(port: int, timeout: float = 0.3) -> bool:
    """检查端口是否已被占用（快速超时）"""
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def check_url(url: str, timeout: float = 1.5) -> bool:
    """检查 HTTP 健康检查端点"""
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        return 200 <= resp.status < 300
    except Exception:
        return False


def find_process(process_name: str) -> bool:
    """检查进程是否在运行"""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}.exe"],
                capture_output=True, text=True, timeout=5,
                encoding="gbk", errors="replace",  # Windows 中文编码
            )
            return process_name.lower() in result.stdout.lower()
        else:
            result = subprocess.run(
                ["pgrep", "-x", process_name],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return False


def kill_process(process_name: str) -> bool:
    """终止进程"""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", f"{process_name}.exe"],
                capture_output=True, timeout=10,
                encoding="gbk", errors="replace",
            )
        else:
            subprocess.run(
                ["pkill", "-f", process_name],
                capture_output=True, timeout=10,
            )
        return True
    except Exception:
        return False


def kill_process_by_pid(pid: int) -> bool:
    """跨平台按 PID 终止进程"""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
            return True
        except Exception:
            return False
    else:
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def find_pid_by_port(port: int) -> int | None:
    """跨平台按端口查找 PID"""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, timeout=10,
                encoding="gbk", errors="replace",
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    return int(line.strip().split()[-1])
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, timeout=5,
                encoding="utf-8",
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTEN" in line:
                    m = re.search(r"pid=(\d+)", line)
                    if m:
                        return int(m.group(1))
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["fuser", f"{port}/tcp"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.decode().strip().split()[-1])
        except Exception:
            pass
    return None


def clear_gateway_cache(project_root: str) -> tuple[int, str]:
    """清除 mcp-gateway 的 Python 缓存，返回 (删除数量, 消息)"""
    gateway_dir = os.path.join(project_root, "mcp-gateway")
    count = 0
    failed = []
    for root, dirs, files in os.walk(gateway_dir):
        if "__pycache__" in dirs:
            pycache = os.path.join(root, "__pycache__")
            try:
                import shutil
                shutil.rmtree(pycache)
                count += 1
            except Exception as e:
                failed.append(f"{pycache}: {e}")
                pass
        for f in files:
            if f.endswith(".pyc"):
                try:
                    os.remove(os.path.join(root, f))
                    count += 1
                except Exception as e:
                    failed.append(f"{f}: {e}")
                    pass
    if failed:
        return count, f"已清除 {count} 个缓存项，但 {len(failed)} 个失败: {'; '.join(failed[:3])}"
    if count:
        return count, f"已清除 {count} 个缓存项"
    return 0, "没有缓存需要清除"


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------
class KBLauncher:
    """主启动器窗口"""

    WINDOW_TITLE = "KB Launcher — Knowledge Base Management"
    WINDOW_SIZE = "820x580"

    # 颜色
    COLOR_RUNNING = "#2ecc71"
    COLOR_STARTING = "#f39c12"
    COLOR_STOPPED = "#95a5a6"
    COLOR_ERROR = "#e74c3c"
    COLOR_BG = "#1a1a2e"
    COLOR_PANEL = "#16213e"
    COLOR_TEXT = "#e0e0e0"
    COLOR_ACCENT = "#0f3460"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(self.WINDOW_TITLE)
        self.root.geometry(self.WINDOW_SIZE)
        self.root.minsize(640, 480)
        self.root.configure(bg=self.COLOR_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._stopping = False
        self._polling = True
        self._tray_icon: Optional[pystray.Icon] = None
        self._service_labels: dict[str, dict] = {}  # name -> {status_label, url_label}

        self._build_ui()
        self._center_window()
        # 延迟启动轮询，避免阻塞窗口首次渲染
        self.root.after(500, self._start_initial_poll)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        """构建界面"""
        # Title bar
        title_frame = tk.Frame(self.root, bg=self.COLOR_PANEL, height=50)
        title_frame.pack(fill=tk.X, side=tk.TOP)
        title_frame.pack_propagate(False)

        title_label = tk.Label(
            title_frame,
            text="📚 KB Launcher — Knowledge Base Management",
            font=("微软雅黑", 14, "bold"),
            bg=self.COLOR_PANEL,
            fg=self.COLOR_TEXT,
        )
        title_label.pack(side=tk.LEFT, padx=20, pady=10)

        # Control buttons
        btn_frame = tk.Frame(title_frame, bg=self.COLOR_PANEL)
        btn_frame.pack(side=tk.RIGHT, padx=10)

        self.start_all_btn = tk.Button(
            btn_frame,
            text="▶ 全部启动",
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_RUNNING,
            fg="white",
            activebackground="#27ae60",
            relief=tk.FLAT,
            padx=15, pady=3,
            command=self._start_all,
        )
        self.start_all_btn.pack(side=tk.LEFT, padx=3)

        self.stop_all_btn = tk.Button(
            btn_frame,
            text="⏹ 全部停止",
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_ERROR,
            fg="white",
            activebackground="#c0392b",
            relief=tk.FLAT,
            padx=15, pady=3,
            command=self._stop_all,
        )
        self.stop_all_btn.pack(side=tk.LEFT, padx=3)

        # Main content: notebook tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Tab 1: Services
        self._build_services_tab()
        # Tab 2: Access URLs
        self._build_access_tab()
        # Tab 3: Logs
        self._build_logs_tab()

        # Status bar
        status_frame = tk.Frame(self.root, bg=self.COLOR_PANEL, height=28)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        status_frame.pack_propagate(False)

        self.status_bar = tk.Label(
            status_frame,
            text="就绪",
            font=("微软雅黑", 10),
            bg=self.COLOR_PANEL,
            fg="#7f8c8d",
            anchor=tk.W,
        )
        self.status_bar.pack(side=tk.LEFT, padx=15, pady=4)

    def _build_services_tab(self):
        """构建服务状态 Tab"""
        tab = tk.Frame(self.notebook, bg=self.COLOR_BG)
        self.notebook.add(tab, text="  服务状态  ")

        # Header
        header = tk.Frame(tab, bg=self.COLOR_PANEL)
        header.pack(fill=tk.X, padx=0, pady=0)

        columns = [
            ("服务", 12), ("状态", 8), ("端口", 8), ("操作", 12), ("访问地址", 40)
        ]
        for text, width in columns:
            tk.Label(
                header, text=text,
                font=("微软雅黑", 11, "bold"),
                bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
                width=width, anchor=tk.W,
            ).pack(side=tk.LEFT, padx=5, pady=6)

        # Service rows (scrollable)
        self.svc_canvas = tk.Canvas(tab, bg=self.COLOR_BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(tab, orient=tk.VERTICAL, command=self.svc_canvas.yview)
        self.svc_frame = tk.Frame(self.svc_canvas, bg=self.COLOR_BG)

        self.svc_frame.bind("<Configure>", lambda e: self.svc_canvas.configure(
            scrollregion=self.svc_canvas.bbox("all")
        ))
        self.svc_canvas.create_window((0, 0), window=self.svc_frame, anchor=tk.NW)
        self.svc_canvas.configure(yscrollcommand=scrollbar.set)

        self.svc_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Populate service rows
        for svc in SERVICES:
            self._add_service_row(svc)

    def _add_service_row(self, svc: ServiceDef):
        """添加单个服务行"""
        row = tk.Frame(self.svc_frame, bg=self.COLOR_BG, pady=2)
        row.pack(fill=tk.X, padx=5)

        # Name
        tk.Label(
            row, text=svc.name,
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_BG, fg=self.COLOR_TEXT,
            width=12, anchor=tk.W,
        ).pack(side=tk.LEFT, padx=5, pady=4)

        # Status indicator
        status_label = tk.Label(
            row, text="● 未启动",
            font=("微软雅黑", 11),
            bg=self.COLOR_BG, fg=self.COLOR_STOPPED,
            width=8, anchor=tk.W,
        )
        status_label.pack(side=tk.LEFT, padx=5, pady=4)

        # Port
        tk.Label(
            row, text=str(svc.port),
            font=("微软雅黑", 11),
            bg=self.COLOR_BG, fg="#7f8c8d",
            width=8, anchor=tk.W,
        ).pack(side=tk.LEFT, padx=5, pady=4)

        # Action buttons
        action_frame = tk.Frame(row, bg=self.COLOR_BG)
        action_frame.pack(side=tk.LEFT, padx=5)

        start_btn = tk.Button(
            action_frame, text="启动",
            font=("微软雅黑", 10),
            bg="#27ae60", fg="white",
            activebackground="#2ecc71",
            relief=tk.FLAT, padx=10, pady=1,
            command=lambda s=svc: self._start_service(s),
        )
        start_btn.pack(side=tk.LEFT, padx=2)

        stop_btn = tk.Button(
            action_frame, text="停止",
            font=("微软雅黑", 10),
            bg="#c0392b", fg="white",
            activebackground="#e74c3c",
            relief=tk.FLAT, padx=10, pady=1,
            command=lambda s=svc: self._stop_service(s),
        )
        stop_btn.pack(side=tk.LEFT, padx=2)

        # Gateway 专属：清除缓存并重启
        restart_btn = None
        if svc.name == "MCP Gateway":
            restart_btn = tk.Button(
                action_frame, text="清除缓存并重启",
                font=("微软雅黑", 9),
                bg="#e67e22", fg="white",
                activebackground="#d35400",
                relief=tk.FLAT, padx=8, pady=1,
                command=lambda s=svc: self._restart_gateway_with_cache_clear(s),
            )
            restart_btn.pack(side=tk.LEFT, padx=2)

        # Access URLs
        url_text = "\n".join(svc.access_urls) if svc.access_urls else "-"
        url_label = tk.Label(
            row, text=url_text,
            font=("Consolas", 10),
            bg=self.COLOR_BG, fg="#3498db",
            width=40, anchor=tk.W, justify=tk.LEFT,
        )
        url_label.pack(side=tk.LEFT, padx=5, pady=4)

        self._service_labels[svc.name] = {
            "status": status_label,
            "url": url_label,
            "start_btn": start_btn,
            "stop_btn": stop_btn,
            "restart_btn": restart_btn,
        }

    def _build_access_tab(self):
        """构建接入地址 Tab（可滚动）"""
        tab = tk.Frame(self.notebook, bg=self.COLOR_BG)
        self.notebook.add(tab, text="  接入地址  ")

        # 外层 Canvas + Scrollbar 实现滚动
        access_canvas = tk.Canvas(tab, bg=self.COLOR_BG, highlightthickness=0)
        access_scroll = tk.Scrollbar(tab, orient=tk.VERTICAL, command=access_canvas.yview)
        access_content = tk.Frame(access_canvas, bg=self.COLOR_BG)

        access_content.bind("<Configure>",
            lambda e: access_canvas.configure(scrollregion=access_canvas.bbox("all")))
        access_canvas.create_window((0, 0), window=access_content, anchor=tk.NW, width=access_canvas.winfo_reqwidth())
        access_canvas.configure(yscrollcommand=access_scroll.set)

        # 让内容宽度跟随 Canvas 宽度
        def _on_canvas_configure(event):
            access_canvas.itemconfig(1, width=event.width)
        access_canvas.bind("<Configure>", _on_canvas_configure)

        access_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        access_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ---- 管理后台 ----
        admin_frame = tk.LabelFrame(
            access_content, text="  管理后台 (Web UI)  ",
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_BG, fg=self.COLOR_TEXT,
            foreground=self.COLOR_TEXT,
        )
        admin_frame.pack(fill=tk.X, padx=12, pady=(12, 6))

        admin_urls = [
            ("管理仪表盘", "http://localhost:8000/admin/dashboard"),
            ("文档管理", "http://localhost:8000/admin/documents"),
            ("上传导入", "http://localhost:8000/admin/documents/upload"),
            ("API Key 管理", "http://localhost:8000/admin/api-keys"),
            ("系统设置", "http://localhost:8000/admin/settings"),
        ]
        for label, url in admin_urls:
            self._make_url_row(admin_frame, f"📊 {label}", url, fg="#3498db")

        # ---- MCP 接入 ----
        mcp_frame = tk.LabelFrame(
            access_content, text="  MCP 接入 (AI Agent)  ",
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_BG, fg=self.COLOR_TEXT,
            foreground=self.COLOR_TEXT,
        )
        mcp_frame.pack(fill=tk.X, padx=12, pady=(6, 6))

        # StreamableHTTP（推荐，端点 /mcp）
        self._make_url_row(mcp_frame,
            "🔗 MCP StreamableHTTP（推荐）", "http://localhost:8000/mcp", fg="#2ecc71")
        # SSE 兼容端点 /sse
        self._make_url_row(mcp_frame,
            "🔗 MCP SSE（兼容旧客户端）", "http://localhost:8000/sse", fg="#f39c12")

        # MCP 客户端配置 (只读代码块 + 复制按钮)
        configs = [
            ("Cursor / Windsurf（支持 StreamableHTTP）", (
                '{\n'
                '  "mcpServers": {\n'
                '    "knowledge-base": {\n'
                '      "url": "http://localhost:8000/mcp",\n'
                '      "headers": {\n'
                '        "X-API-Key": "<your-api-key>"\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}'
            )),
            ("Claude Desktop / Kimi Code（需 mcp-proxy 中转）", (
                '{\n'
                '  "mcpServers": {\n'
                '    "knowledge-base": {\n'
                '      "command": "npx",\n'
                '      "args": [\n'
                '        "-y", "mcp-proxy",\n'
                '        "http://localhost:8000/sse?api_key=<your-api-key>"\n'
                '      ]\n'
                '    }\n'
                '  }\n'
                '}'
            )),
        ]
        for client_name, config_json in configs:
            self._make_config_block(mcp_frame, f"⚙ {client_name}", config_json)

        # ---- API 接口 ----
        api_frame = tk.LabelFrame(
            access_content, text="  REST API  ",
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_BG, fg=self.COLOR_TEXT,
            foreground=self.COLOR_TEXT,
        )
        api_frame.pack(fill=tk.X, padx=12, pady=(6, 6))

        api_urls = [
            ("搜索知识库", "GET  http://localhost:8000/api/search?q=<query>"),
            ("列出文档", "GET  http://localhost:8000/api/documents"),
            ("添加文档", "POST http://localhost:8000/api/documents"),
            ("更新文档", "PUT  http://localhost:8000/api/documents/{doc_id}"),
            ("删除文档", "DEL  http://localhost:8000/api/documents/{doc_id}"),
            ("目录树", "GET  http://localhost:8000/api/directories"),
        ]
        for label, url in api_urls:
            self._make_url_row(api_frame, f"📡 {label}", url, fg="#f39c12")

        # ---- MinIO ----
        minio_frame = tk.LabelFrame(
            access_content, text="  对象存储  ",
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_BG, fg=self.COLOR_TEXT,
            foreground=self.COLOR_TEXT,
        )
        minio_frame.pack(fill=tk.X, padx=12, pady=(6, 12))

        self._make_url_row(minio_frame,
            "🗄 MinIO 控制台", "http://localhost:9001", fg="#e67e22")
        self._make_url_row(minio_frame,
            "🔑 默认账号/密码", "minioadmin / minioadmin", fg="#7f8c8d")
    def _make_url_row(self, parent, label: str, url: str, fg: str = "#3498db"):
        """创建 URL 行（标签 + URL + 复制按钮）"""
        row = tk.Frame(parent, bg=self.COLOR_BG)
        row.pack(fill=tk.X, padx=10, pady=3)

        tk.Label(
            row, text=label,
            font=("微软雅黑", 11),
            bg=self.COLOR_BG, fg=self.COLOR_TEXT,
            width=16, anchor=tk.W,
        ).pack(side=tk.LEFT, padx=(0, 5))

        url_label = tk.Label(
            row, text=url,
            font=("Consolas", 10),
            bg="#0d1117", fg=fg,
            padx=8, pady=2,
            anchor=tk.W,
        )
        url_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        copy_btn = tk.Button(
            row, text="📋",
            font=("微软雅黑", 10),
            bg="#2c3e50", fg="white",
            activebackground="#34495e",
            relief=tk.FLAT, padx=8,
            command=lambda u=url: self._copy_to_clipboard(u),
        )
        copy_btn.pack(side=tk.RIGHT, padx=(5, 0))

    def _make_config_block(self, parent, title: str, content: str):
        """创建 MCP 配置代码块（带标题 + 复制按钮）"""
        header_row = tk.Frame(parent, bg=self.COLOR_BG)
        header_row.pack(fill=tk.X, padx=10, pady=(8, 0))

        tk.Label(
            header_row, text=title,
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_BG, fg=self.COLOR_TEXT,
        ).pack(side=tk.LEFT)

        copy_btn = tk.Button(
            header_row, text="📋 复制配置",
            font=("微软雅黑", 10),
            bg="#2c3e50", fg="white",
            activebackground="#34495e",
            relief=tk.FLAT, padx=10,
            command=lambda c=content: self._copy_to_clipboard(c),
        )
        copy_btn.pack(side=tk.RIGHT)

        code_block = tk.Text(
            parent,
            font=("Consolas", 10),
            bg="#0d1117", fg="#c9d1d9",
            insertbackground="white",
            height=7, width=50,
            padx=10, pady=6,
            relief=tk.FLAT,
            wrap=tk.NONE,
        )
        code_block.insert(tk.END, content)
        code_block.config(state=tk.DISABLED)  # 只读
        code_block.pack(fill=tk.X, padx=12, pady=(2, 6))

    def _copy_to_clipboard(self, text: str):
        """复制文本到剪贴板（使用主窗口实例，避免多 Tk root 冲突）"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_status("已复制到剪贴板 ✓")
        except Exception:
            pass

    def _restart_gateway_with_cache_clear(self, svc: ServiceDef):
        """停止 Gateway → 等待完全退出 → 清除缓存 → 启动 Gateway"""
        self.set_status("正在重启 Gateway（清除缓存）...")
        self._log("--- 清除缓存并重启 Gateway ---")

        def task():
            # 1. 停止 Gateway
            self._log("[Step 1/4] 停止 Gateway")
            self._do_stop_service(svc)

            # 2. 等待进程完全退出（防止旧进程继续生成缓存）
            self._log("[Step 2/4] 等待进程完全退出...")
            import time
            for i in range(20):
                if not self._is_service_running(svc):
                    break
                time.sleep(0.1)
            if self._is_service_running(svc):
                self._log("  [WARN] 进程仍在运行，尝试强制终止")
                self._do_stop_service(svc)
                time.sleep(0.3)

            # 3. 清除缓存
            self._log("[Step 3/4] 清除 Python 缓存")
            count, msg = clear_gateway_cache(PROJECT_ROOT)
            self._log(f"  {msg}")

            # 4. 启动 Gateway
            self._log("[Step 4/4] 启动 Gateway")
            self._do_start_service(svc)
            self._log(f"--- Gateway 重启完成 ---")
            self.root.after(0, self.set_status, "Gateway 已重启（缓存已清除）")

        threading.Thread(target=task, daemon=True).start()

    def _build_logs_tab(self):
        """构建日志 Tab"""
        tab = tk.Frame(self.notebook, bg=self.COLOR_BG)
        self.notebook.add(tab, text="  运行日志  ")

        toolbar = tk.Frame(tab, bg=self.COLOR_PANEL, height=36)
        toolbar.pack(fill=tk.X)
        toolbar.pack_propagate(False)

        tk.Label(
            toolbar, text="📋 服务启动日志与报错",
            font=("微软雅黑", 11, "bold"),
            bg=self.COLOR_PANEL, fg=self.COLOR_TEXT,
        ).pack(side=tk.LEFT, padx=15, pady=6)

        clear_btn = tk.Button(
            toolbar, text="清空",
            font=("微软雅黑", 10),
            bg="#7f8c8d", fg="white",
            relief=tk.FLAT, padx=10,
            command=lambda: self.log_area.delete(1.0, tk.END),
        )
        clear_btn.pack(side=tk.RIGHT, padx=10, pady=4)

        self.log_area = scrolledtext.ScrolledText(
            tab,
            font=("Consolas", 10),
            bg="#0d1117", fg="#c9d1d9",
            insertbackground="white",
            wrap=tk.WORD,
        )
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _start_log_reader(self, log_path: str):
        """在后台线程读取 Gateway 日志并输出到运行日志区域"""
        self._log("[LOG] Gateway 日志已开启，内容实时显示 ↓")
        self._gw_log_stop = False

        def reader():
            # 等待日志文件创建
            for _ in range(20):
                if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
                    break
                time.sleep(0.3)
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    # seek to end
                    f.seek(0, 2)
                    while not self._gw_log_stop:
                        line = f.readline()
                        if line:
                            line = line.rstrip("\n")
                            if line.strip():
                                self.root.after(0, self._log, f"[GW] {line}")
                        else:
                            time.sleep(0.1)
            except Exception:
                pass

        threading.Thread(target=reader, daemon=True).start()

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------
    def _center_window(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"+{x}+{y}")

    def _on_close(self):
        """关闭窗口 → 最小化到托盘"""
        if TRAY_AVAILABLE:
            self.root.withdraw()
            self._create_tray()
            self._log("窗口已最小化到系统托盘，右键图标退出")
        else:
            if messagebox.askokcancel("退出", "关闭窗口将停止所有服务\n确定退出吗？"):
                self._shutdown()

    # ------------------------------------------------------------------
    # System Tray
    # ------------------------------------------------------------------
    def _create_tray(self):
        """创建系统托盘图标"""
        if self._tray_icon:
            return

        # 生成简单图标 (16x16 scaled to 64x64)
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([4, 4, 60, 60], radius=16, fill="#0f3460")
        draw.rectangle([22, 20, 28, 44], fill="#2ecc71")   # K
        draw.rectangle([36, 20, 42, 44], fill="#2ecc71")   # K
        draw.polygon([22, 20, 32, 12, 42, 20], fill="#2ecc71")

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._restore_window, default=True),
            pystray.MenuItem("启动全部", self._start_all),
            pystray.MenuItem("停止全部", self._stop_all),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_exit),
        )

        self._tray_icon = pystray.Icon(
            "kb_launcher", image, "KB Launcher", menu,
        )
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _restore_window(self, icon=None, item=None):
        """从托盘恢复窗口"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None

    def _tray_exit(self, icon=None, item=None):
        """从托盘退出"""
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None
        self._shutdown()

    # ------------------------------------------------------------------
    # Service control
    # ------------------------------------------------------------------
    def _start_all(self):
        """按依赖顺序启动所有服务"""
        self._stopping = False
        self.start_all_btn.config(state=tk.DISABLED)
        self.set_status("正在启动所有服务...")

        # 按依赖顺序排序
        started = set()
        result = []

        def add_with_deps(svc):
            if svc.name in started:
                return
            for dep_name in svc.requires:
                dep = next((s for s in SERVICES if s.name == dep_name), None)
                if dep and dep.name not in started:
                    add_with_deps(dep)
            result.append(svc)
            started.add(svc.name)

        for svc in SERVICES:
            add_with_deps(svc)

        thread = threading.Thread(target=self._start_all_thread, args=(result,), daemon=True)
        thread.start()

    def _start_all_thread(self, ordered_services: list[ServiceDef]):
        """后台线程启动所有服务"""
        for svc in ordered_services:
            if self._stopping:
                break
            svc.error_msg = ""
            self.root.after(0, self._update_service_status, svc, "starting", "启动中...")
            if not self._do_start_service(svc):
                self._log(f"[ERROR] {svc.name} 启动失败，停止后续服务")
                self.root.after(0, self.set_status, f"{svc.name} 启动失败，请检查日志")
                break
            self.root.after(0, self._update_service_status, svc, "running", "运行中")

        self.root.after(0, self.start_all_btn.config, {"state": tk.NORMAL})
        self.root.after(0, self.set_status, "全部启动完成")

    def _stop_all(self):
        """停止所有服务"""
        self._stopping = True
        self.stop_all_btn.config(state=tk.DISABLED)
        self.set_status("正在停止所有服务...")

        thread = threading.Thread(target=self._stop_all_thread, daemon=True)
        thread.start()

    def _stop_all_thread(self):
        """后台线程停止所有服务（反序）"""
        for svc in reversed(SERVICES):
            self._do_stop_service(svc)
            self.root.after(0, self._update_service_status, svc, "stopped", "未启动")

        self.root.after(0, self.stop_all_btn.config, {"state": tk.NORMAL})
        self.root.after(0, self.set_status, "所有服务已停止")

    def _start_service(self, svc: ServiceDef):
        """启动单个服务"""
        self.set_status(f"正在启动 {svc.name}...")
        self._update_service_status(svc, "starting", "启动中...")

        thread = threading.Thread(target=self._start_service_thread, args=(svc,), daemon=True)
        thread.start()

    def _start_service_thread(self, svc: ServiceDef):
        svc.error_msg = ""
        success = self._do_start_service(svc)
        self.root.after(0, self._update_service_status, svc,
            "running" if success else "error",
            "运行中" if success else svc.error_msg or "启动失败",
        )
        self.root.after(0, self.set_status, f"{svc.name} {'启动成功' if success else '启动失败'}")

    def _do_start_service(self, svc: ServiceDef) -> bool:
        """实际执行启动逻辑，返回是否成功"""
        self._log(f"--- 启动 {svc.name} ---")

        # 检查依赖
        for dep_name in svc.requires:
            dep = next((s for s in SERVICES if s.name == dep_name), None)
            if dep and dep.status != "running":
                svc.error_msg = f"依赖服务 {dep_name} 未运行，请先启动 {dep_name}"
                self._log(f"[WARN] {svc.error_msg}")
                return False

        # 检查是否已在运行
        if self._is_service_running(svc):
            self._log(f"[INFO] {svc.name} 已在运行 (端口 {svc.port})")
            return True

        # 特殊处理 Redis —— 优先查找已安装的服务（Memurai / Redis）
        if svc.name == "Redis":
            return self._start_redis(svc)

        # 检查可执行文件
        exe = svc.start_cmd[0] if svc.start_cmd else ""
        if exe and not self._find_exe(exe):
            svc.error_msg = f"{exe} 未安装或不在 PATH 中"
            self._log(f"[ERROR] {svc.error_msg}")
            return False

        # 启动进程
        try:
            env = os.environ.copy()
            env["KBDATA_DIR"] = str(KBDATA_DIR)
            if svc.name in ("MCP Gateway",):
                env["PYTHONPATH"] = str(PROJECT_ROOT / "mcp-gateway" / "src")
                env["REDIS_URL"] = "redis://localhost:6379/0"
                env["CHROMA_HOST"] = "localhost"
                env["CHROMA_PORT"] = "8001"
                env["OLLAMA_URL"] = "http://localhost:11434"
                env["MINIO_ENDPOINT"] = "localhost:9000"
                env["MINIO_ACCESS_KEY"] = "minioadmin"
                env["MINIO_SECRET_KEY"] = "minioadmin"
                env["MINIO_BUCKET"] = "kb-sources"
                env["MINIO_SECURE"] = "false"
                env["DEBUG"] = "true"
                env["CORS_ORIGINS"] = "*"
                env["ADMIN_ACCOUNTS_FILE"] = str(KBDATA_DIR / "config" / "admin_accounts.json")
                env["API_KEY_FILE"] = str(KBDATA_DIR / "config" / "api_keys.json")
                # 开发模式：禁用 Python 字节码缓存，避免旧代码残留问题
                env["PYTHONDONTWRITEBYTECODE"] = "1"

            if svc.name == "MinIO":
                env["MINIO_ROOT_USER"] = "minioadmin"
                env["MINIO_ROOT_PASSWORD"] = "minioadmin"

            if svc.name == "MCP Gateway":
                cwd = str(PROJECT_ROOT / "mcp-gateway")
            else:
                cwd = str(PROJECT_ROOT)

            self._log(f"[CMD] {' '.join(svc.start_cmd)}")
            # Gateway logs: capture to file for debugging
            if svc.name == "MCP Gateway":
                log_file = open(str(LOGS_DIR / "gateway.log"), "w", encoding="utf-8")
                svc.process = subprocess.Popen(
                    svc.start_cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=cwd,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                svc._log_file = log_file  # keep reference for closing
                # start reader thread
                self._start_log_reader(str(LOGS_DIR / "gateway.log"))
            else:
                svc.process = subprocess.Popen(
                    svc.start_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    cwd=cwd,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            svc.pid = svc.process.pid  # 记录 PID 用于精确停止

            # 等待启动
            self._log(f"  等待 {svc.startup_delay}s...")
            time.sleep(svc.startup_delay)

            if self._is_service_running(svc):
                self._log(f"[OK] {svc.name} 启动成功 (PID={svc.process.pid})")
                return True
            else:
                svc.error_msg = f"进程已启动但端口 {svc.port} 无法访问，请检查日志"
                self._log(f"[WARN] {svc.error_msg}")
                return False

        except Exception as e:
            svc.error_msg = f"启动异常: {e}"
            self._log(f"[ERROR] {svc.error_msg}")
            return False

    def _start_redis(self, svc: ServiceDef) -> bool:
        """特殊处理 Redis / Memurai（跨平台）"""
        # Windowspak: 尝试 Memurai
        if sys.platform == "win32":
            memurai_paths = [
                os.path.expandvars(r"%ProgramFiles%\Memurai\memurai.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Memurai\memurai.exe"),
            ]
            for p in memurai_paths:
                if os.path.exists(p):
                    try:
                        subprocess.Popen(
                            [p],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        self._log(f"[INFO] 启动 Memurai: {p}")
                        time.sleep(2)
                        if check_port(6379):
                            self._log("[OK] Redis (Memurai) 启动成功")
                            return True
                    except Exception as e:
                        self._log(f"[WARN] Memurai 启动失败: {e}")

        # 尝试 redis-server（Windows/Linux 通用）
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        if self._find_exe("redis-server"):
            try:
                subprocess.Popen(
                    ["redis-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
                self._log("[INFO] 启动 redis-server")
                time.sleep(2)
                if check_port(6379):
                    self._log("[OK] Redis 启动成功")
                    return True
            except Exception as e:
                self._log(f"[WARN] redis-server 启动失败: {e}")

        # 检查是否已经在运行
        if check_port(6379):
            self._log("[INFO] Redis 端口 6379 已被占用，跳过启动")
            return True

        svc.error_msg = "Redis 未安装。Windows: https://www.memurai.com | Linux: apt install redis-server" if sys.platform == "win32" else "Redis 未安装。Linux: apt install redis-server"
        self._log(f"[ERROR] {svc.error_msg}")
        return False

    def _stop_service(self, svc: ServiceDef):
        """停止单个服务"""
        self.set_status(f"正在停止 {svc.name}...")
        thread = threading.Thread(target=self._stop_service_thread, args=(svc,), daemon=True)
        thread.start()

    def _stop_service_thread(self, svc: ServiceDef):
        self._do_stop_service(svc)
        self.root.after(0, self._update_service_status, svc, "stopped", "未启动")
        self.root.after(0, self.set_status, f"{svc.name} 已停止")

    def _do_stop_service(self, svc: ServiceDef):
        """实际执行停止逻辑（多级回退，确保进程被终止）"""
        self._log(f"--- 停止 {svc.name} ---")

        # Stop gateway log reader
        if svc.name == "MCP Gateway":
            self._gw_log_stop = True
            if hasattr(svc, '_log_file') and svc._log_file:
                try:
                    svc._log_file.close()
                except Exception:
                    pass
                svc._log_file = None

        # 1) 记录的进程句柄
        if svc.process and svc.process.poll() is None:
            self._log(f"  终止进程 PID={svc.process.pid}")
            try:
                svc.process.terminate()
                # 等待进程完全退出（最多 3 秒），防止旧进程在后台继续生成缓存
                for _ in range(30):
                    if svc.process.poll() is not None:
                        break
                    time.sleep(0.1)
                if svc.process.poll() is None:
                    self._log(f"  [WARN] 进程未响应 terminate()，强制 kill")
                    svc.process.kill()
                    time.sleep(0.3)
            except Exception as e:
                self._log(f"  [WARN] 终止异常: {e}")
            svc.process = None
            self._log(f"[OK] {svc.name} 已停止")
            return

        # 2) 记录的 PID
        if getattr(svc, "pid", None):
            self._log(f"  按 PID={svc.pid} 终止")
            kill_process_by_pid(svc.pid)
            svc.pid = None
            self._log(f"[OK] {svc.name} 已停止")
            return

        # 3) 按进程名（避免宽泛匹配 python）
        if svc.process_name and svc.process_name != "python":
            kill_process(svc.process_name)
            self._log(f"[OK] {svc.name} 已停止")
            return

        # 4) 有端口时，按端口找出占用进程并杀
        if svc.port:
            pid = find_pid_by_port(svc.port)
            if pid:
                self._log(f"  按端口 {svc.port} 找到 PID={pid}，终止中")
                kill_process_by_pid(pid)
                self._log(f"[OK] {svc.name} (端口 {svc.port}) 已停止")
                return

        self._log(f"[OK] {svc.name} 未找到运行中进程")

    # ------------------------------------------------------------------
    # Status detection
    # ------------------------------------------------------------------
    def _is_service_running(self, svc: ServiceDef) -> bool:
        """检测服务是否在运行"""
        # 优先用健康检查 URL
        if svc.health_url and check_url(svc.health_url):
            return True
        # 其次用端口检测
        if svc.port and check_port(svc.port):
            return True
        # 仅当无 URL/端口时才用进程名回退（避免误匹配同名进程）
        if not svc.health_url and not svc.port and svc.process_name:
            return find_process(svc.process_name)
        return False

    def _update_service_status(self, svc: ServiceDef, status: str, text: str):
        """更新单个服务的 GUI 状态"""
        svc.status = status

        colors = {
            "running": self.COLOR_RUNNING,
            "starting": self.COLOR_STARTING,
            "stopped": self.COLOR_STOPPED,
            "error": self.COLOR_ERROR,
        }
        icons = {
            "running": "●",
            "starting": "◐",
            "stopped": "○",
            "error": "●",
        }

        labels = self._service_labels.get(svc.name)
        if not labels:
            return

        color = colors.get(status, self.COLOR_STOPPED)
        icon = icons.get(status, "○")

        labels["status"].config(
            text=f"{icon} {text}", fg=color,
        )

        # Also update error message in url label on error
        if status == "error" and svc.error_msg:
            labels["url"].config(text=svc.error_msg, fg=self.COLOR_ERROR)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _start_initial_poll(self):
        """首次轮询（延迟执行，不阻塞 GUI 渲染）"""
        self._polling = True
        self.set_status("正在检测服务状态...")
        thread = threading.Thread(target=self._poll_once_thread, daemon=True)
        thread.start()

    def _poll_once_thread(self):
        """后台线程执行一次轮询，结果回主线程更新 UI"""
        statuses = []
        for svc in SERVICES:
            running = self._is_service_running(svc)
            new_status = "running" if running else "stopped"
            new_text = "运行中" if running else "未启动"
            statuses.append((svc, new_status, new_text))

        def _update():
            for svc, status, text in statuses:
                svc.status = status
                self._update_service_status(svc, status, text)
            self.set_status("就绪")
        self.root.after(0, _update)

        # 后续轮询用 after 触发（已无阻塞操作）
        self.root.after(3000, self._poll_once)

    def _poll_once(self):
        """轮询入口（主线程触发，后台线程执行检测）"""
        if not self._polling:
            return

        to_check = [
            svc for svc in SERVICES
            if svc.status not in ("starting", "stopping", "running")
        ]

        if not to_check:
            self.root.after(3000, self._poll_once)
            return

        # 后台线程执行检测，主线程继续响应 UI
        thread = threading.Thread(
            target=self._poll_once_thread_body, args=(to_check,), daemon=True
        )
        thread.start()

    def _poll_once_thread_body(self, to_check: list):
        """后台线程执行服务检测，结果回主线程更新 UI"""
        statuses = []
        for svc in to_check:
            running = self._is_service_running(svc)
            new_status = "running" if running else "stopped"
            new_text = "运行中" if running else "未启动"
            statuses.append((svc, new_status, new_text))

        def _update():
            for svc, status, text in statuses:
                svc.status = status
                self._update_service_status(svc, status, text)
        self.root.after(0, _update)

        # 下一次轮询（主线程调度）
        self.root.after(3000, self._poll_once)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log(self, message: str):
        """写入日志区域（线程安全）"""
        timestamp = datetime.now().strftime("%H:%M:%S")

        def _write():
            self.log_area.insert(tk.END, f"[{timestamp}] {message}\n")
            self.log_area.see(tk.END)

        self.root.after(0, _write)

    def set_status(self, text: str):
        """更新状态栏"""
        self.root.after(0, lambda: self.status_bar.config(text=text))

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _find_exe(name: str) -> bool:
        """检查可执行文件是否在 PATH 中或为绝对路径"""
        if os.path.isabs(name) and os.path.exists(name):
            return True
        return shutil.which(name) is not None

    def _shutdown(self):
        """关闭应用"""
        self._polling = False
        self._log("应用退出，停止所有服务...")
        for svc in SERVICES:
            self._do_stop_service(svc)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        app = KBLauncher()
        app.run()
    except Exception as e:
        import traceback
        # 写入错误日志文件
        log_path = Path(__file__).parent / "kb_launcher_error.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"FATAL: KB Launcher crashed\n")
            f.write(f"Error: {e}\n\n")
            f.write(traceback.format_exc())
        # 弹窗显示错误
        try:
            import tkinter.messagebox as mb
            mb.showerror(
                "KB Launcher 错误",
                f"启动失败:\n\n{e}\n\n错误详情已写入:\n{log_path}"
            )
        except Exception:
            pass
        raise
    app.run()

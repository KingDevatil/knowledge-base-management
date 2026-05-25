"""
Process Test — 验证进程启停、端口检测、杀进程逻辑
运行: python process_test.py
"""
import subprocess
import time
import socket
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
GATEWAY_DIR = os.path.join(PROJECT_ROOT, "mcp-gateway")
PYTHON = sys.executable

passed = 0
failed = 0
errors: list[str] = []


def T(name):
    def d(fn):
        global passed, failed
        try:
            fn()
            passed += 1
            print(f"  OK {name}")
        except Exception as e:
            failed += 1
            msg = f"  FAIL {name}: {e}"
            errors.append(msg)
            print(msg)
    return d


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    """检测端口是否被占用"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def pid_listening(port: int) -> str | None:
    """返回监听指定端口的 PID"""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, timeout=10,
            encoding="gbk", errors="replace",
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                return parts[-1]
    except Exception:
        pass
    return None


def kill_by_pid(pid: str) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", pid],
            capture_output=True, timeout=10,
            encoding="gbk", errors="replace",
        )
        time.sleep(0.5)
        return not pid_listening(8000)
    except Exception:
        return False


# ── 1. 环境检查 ────────────────────────────────
print("\n1. 环境检查")
@T("Python 可执行")
def _():
    p = subprocess.run([PYTHON, "--version"], capture_output=True, timeout=5)
    assert p.returncode == 0

@T("Gateway 源码存在")
def _():
    main_py = os.path.join(GATEWAY_DIR, "src", "main.py")
    assert os.path.exists(main_py), f"missing: {main_py}"


# ── 2. 端口检测 ────────────────────────────────
print("\n2. 端口检测")
@T("port_open 函数可用")
def _():
    opened = port_open(8000)
    print(f"    (端口 8000 当前 {'占用' if opened else '空闲'})")

@T("pid_listening 可用")
def _():
    pid = pid_listening(8000)
    if pid:
        print(f"    (端口 8000 PID={pid})")


# ── 3. 杀进程逻辑 ──────────────────────────────
print("\n3. 杀进程逻辑")
INITIAL_STATE = port_open(8000)

if INITIAL_STATE:
    print("    (Gateway 已在运行，跳过启停测试，仅验证检测功能)")
    @T("netstat 可找到 Gateway PID")
    def _():
        pid = pid_listening(8000)
        assert pid is not None, "netstat 未找到监听 8000 的进程"
        assert pid.isdigit(), f"PID 非数字: {pid}"
        print(f"    (PID={pid})")

    # 不实际杀，用子进程验证 taskkill 可用
    @T("子进程启停循环")
    def _():
        p = subprocess.Popen(
            [PYTHON, "-c", "import time; time.sleep(10)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        pid = str(p.pid)
        time.sleep(0.5)
        assert p.poll() is None, "子进程应仍在运行"
        subprocess.run(["taskkill", "/F", "/PID", pid],
            capture_output=True, timeout=5,
            encoding="gbk", errors="replace")
        time.sleep(0.5)
        assert p.poll() is not None, "子进程应已终止"

else:
    print("    (Gateway 未运行，尝试启动...")
    @T("启动 Gateway → 端口通")
    def _():
        global gateway_process
        env = os.environ.copy()
        env["DEBUG"] = "true"
        env["KBDATA_DIR"] = os.path.join(PROJECT_ROOT, "kbdata")
        env["REDIS_URL"] = "redis://localhost:6379/0"
        env["SESSION_SECRET"] = "a" * 32
        gateway_process = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "src.main:app",
             "--host", "127.0.0.1", "--port", "8000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, cwd=GATEWAY_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # Gateway 依赖多，等 10 秒
        for i in range(10):
            time.sleep(1)
            if port_open(8000):
                print(f"    (启动耗时 {i+1}s)")
                return
        # 超时 → 可能是依赖服务未运行
        gateway_process.kill()
        gateway_process = None
        raise AssertionError("Gateway 启动超时（依赖可能未就绪）")

    @T("netstat 可找到 Gateway PID")
    def _():
        pid = pid_listening(8000)
        assert pid is not None, f"netstat 未找到监听 8000 的进程: {pid}"

    @T("taskkill /PID 可终止 Gateway")
    def _():
        pid = pid_listening(8000)
        assert kill_by_pid(pid), f"无法终止 PID={pid}"

    @T("终止后端口关闭")
    def _():
        time.sleep(1)
        assert not port_open(8000), "端口 8000 仍占用"


# ── 4. 异常路径 ────────────────────────────────
print("\n4. 异常路径")
@T("杀不存在的 PID → 不崩溃")
def _():
    p = subprocess.run(
        ["taskkill", "/F", "/PID", "99999"],
        capture_output=True, timeout=5,
        encoding="gbk", errors="replace",
    )
    # 不存在返回 error，但不应崩溃

@T("netstat 输出可解析")
def _():
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True, timeout=5,
        encoding="gbk", errors="replace",
    )
    lines = result.stdout.splitlines()
    assert len(lines) > 5, f"netstat 输出异常: {len(lines)} 行"

@T("检测空闲端口 → False")
def _():
    # 找一个大概率空闲的高端口
    assert not port_open(59999), "端口 59999 意外被占用"


# ── 结果 ────────────────────────────────────────
print()
print("=" * 60)
print(f"  Process Test: {passed} 通过, {failed} 失败")
print("=" * 60)
if errors:
    print("\n失败详情:")
    for e in errors:
        print(f"  {e}")
if failed:
    sys.exit(1)

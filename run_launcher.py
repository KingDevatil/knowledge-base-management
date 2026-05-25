"""
KB Launcher — Python 启动器
用法: python run_launcher.py
功能: 按命令行参数精确清理残留 kb_launcher 进程，再启动 GUI
"""
import subprocess
import sys
import os
import time


def find_kb_launcher_pids():
    """查找所有运行 kb_launcher.pyw 的 python 进程 PID（不含自身）"""
    my_pid = os.getpid()
    pids = []
    try:
        # wmic 获取所有 python.exe 的命令行
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "processid,commandline", "/format:csv"],
            capture_output=True, text=True, timeout=5,
            encoding="gbk", errors="replace",
        )
        for line in result.stdout.strip().splitlines():
            if "kb_launcher" in line:
                parts = line.rsplit(",", 1)
                if len(parts) == 2:
                    try:
                        pid = int(parts[1].strip())
                        if pid != my_pid:
                            pids.append(pid)
                    except ValueError:
                        continue
    except Exception:
        pass
    return pids


def main():
    # 1. 清理残留（按命令行参数精确匹配，不误杀自身）
    pids = find_kb_launcher_pids()
    if pids:
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                    encoding="gbk", errors="replace",
                )
            except Exception:
                pass
        time.sleep(0.5)

    # 2. 也清理 pythonw.exe 残留
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "pythonw.exe"],
            capture_output=True, timeout=5,
            encoding="gbk", errors="replace",
        )
    except Exception:
        pass

    # 3. 启动 GUI
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb_launcher.pyw")
    subprocess.Popen(
        [sys.executable, script],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


if __name__ == "__main__":
    main()

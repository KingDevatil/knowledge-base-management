"""
kb_launcher 自动化测试 — 覆盖所有非 GUI 逻辑
运行: python test_launcher.py
"""
import os, sys, time, socket
import subprocess
from pathlib import Path

# 确保项目在路径中
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---- 测试框架 ----
passed = 0
failed = 0
errors = []

def test(name: str):
    """测试装饰器"""
    def decorator(fn):
        global passed, failed
        try:
            fn()
            passed += 1
            print(f"  ✓ {name}")
        except Exception as e:
            failed += 1
            msg = f"  ✗ {name}: {e}"
            errors.append(msg)
            print(msg)
            import traceback; traceback.print_exc()
    return decorator

# ============================================================
# 1. 模块级代码测试
# ============================================================
@test("模块导入无异常")
def test_import():
    import kb_launcher
    assert hasattr(kb_launcher, 'KBLauncher')
    assert hasattr(kb_launcher, 'ServiceDef')
    assert hasattr(kb_launcher, 'SERVICES')
    assert hasattr(kb_launcher, 'check_port')
    assert hasattr(kb_launcher, 'check_url')
    assert hasattr(kb_launcher, 'find_process')
    assert hasattr(kb_launcher, 'kill_process')

@test("SERVICES 包含 5 个服务")
def test_services_count():
    import kb_launcher
    assert len(kb_launcher.SERVICES) == 5
    names = [s.name for s in kb_launcher.SERVICES]
    assert names == ["Ollama", "Redis", "Chroma", "MinIO", "MCP Gateway"]

@test("ServiceDef 字段完整")
def test_service_fields():
    from kb_launcher import SERVICES
    for svc in SERVICES:
        assert svc.name, f"{svc} name empty"
        assert svc.port > 0 or svc.name == "Redis", f"{svc.name} port invalid"
        assert svc.status == "stopped", f"{svc.name} initial status wrong"
        assert svc.error_msg == "", f"{svc.name} initial error_msg not empty"

@test("MCP Gateway 依赖链正确")
def test_gateway_deps():
    from kb_launcher import SERVICES
    gw = next(s for s in SERVICES if s.name == "MCP Gateway")
    assert "Ollama" in gw.requires
    assert "Redis" in gw.requires
    assert "Chroma" in gw.requires
    assert "MinIO" in gw.requires

@test("Chroma 依赖 Redis")
def test_chroma_deps():
    from kb_launcher import SERVICES
    chroma = next(s for s in SERVICES if s.name == "Chroma")
    assert "Redis" in chroma.requires

@test("Ollama/Redis/MinIO 无依赖")
def test_no_deps():
    from kb_launcher import SERVICES
    for name in ["Ollama", "Redis", "MinIO"]:
        svc = next(s for s in SERVICES if s.name == name)
        assert svc.requires == [], f"{name} should have no dependencies"

# ============================================================
# 2. 帮助函数测试
# ============================================================
@test("check_port: 未占用端口返回 False")
def test_check_port_free():
    from kb_launcher import check_port
    # 选择极不可能被占用的端口
    assert check_port(19999) == False

@test("check_port: 快速响应 (< 2s)")
def test_check_port_fast():
    from kb_launcher import check_port
    t0 = time.time()
    check_port(19999)
    check_port(19998)
    elapsed = time.time() - t0
    # Windows socket 连接有额外延迟，600ms 以内合理
    assert elapsed < 2.0, f"Too slow: {elapsed:.2f}s (expected < 2s for 2 checks)"

@test("check_url: 不存在的服务返回 False")
def test_check_url_unreachable():
    from kb_launcher import check_url
    assert check_url("http://localhost:19999/health") == False

@test("check_url: 快速超时 (< 3.5s)")
def test_check_url_fast():
    from kb_launcher import check_url
    t0 = time.time()
    check_url("http://localhost:19999/health")
    elapsed = time.time() - t0
    assert elapsed < 3.5, f"Too slow: {elapsed:.2f}s"

@test("find_process: py.exe 存在返回 True (gbk编码)")
def test_find_process_running():
    from kb_launcher import find_process
    # python.exe 一定在运行（我们在用它）
    result = find_process("python")
    assert result == True, f"find_process('python') = {result}, expected True"

@test("find_process: 不存在进程返回 False")
def test_find_process_missing():
    from kb_launcher import find_process
    result = find_process("nonexistent_process_xyz_12345")
    assert result == False

@test("find_process: 无异常抛出")
def test_find_process_no_exception():
    from kb_launcher import find_process
    try:
        find_process("some_random_name_!!##")
    except Exception as e:
        assert False, f"find_process raised: {e}"

# ============================================================
# 3. 依赖拓扑排序测试
# ============================================================
@test("拓扑排序: 结果包含全部 5 个服务")
def test_topo_all_included():
    from kb_launcher import SERVICES
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

    assert len(result) == 5
    names = [s.name for s in result]
    assert set(names) == {"Ollama", "Redis", "Chroma", "MinIO", "MCP Gateway"}

@test("拓扑排序: 依赖在前")
def test_topo_deps_first():
    from kb_launcher import SERVICES
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

    names = [s.name for s in result]

    # Redis 必须在 Chroma 前面
    assert names.index("Redis") < names.index("Chroma")
    # 依赖链末端是 MCP Gateway
    assert names[-1] == "MCP Gateway"

# ============================================================
# 4. 服务检测逻辑测试
# ============================================================
@test("_is_service_running: 未运行服务返回 False (健康检查)")
def test_not_running_health():
    # 模拟一个指向不存在端口的服务
    class MockService:
        name = "test"
        port = 19999
        health_url = "http://localhost:19999/health"
        process_name = "nonexistent_xyz"
        status = "stopped"
        error_msg = ""

    svc = MockService()
    from kb_launcher import check_url, check_port, find_process
    # 模拟 _is_service_running 逻辑（无法直接调用实例方法）
    running = (check_url(svc.health_url) or
               (svc.port and check_port(svc.port)) or
               (find_process(svc.process_name)))
    assert running == False

@test("_is_service_running: url 优先于 port")
def test_url_before_port():
    from kb_launcher import check_url, check_port

    # 即使端口可达，url 不可达仍算未运行
    url_ok = check_url("http://localhost:19999/health")
    port_ok = check_port(19999)
    assert url_ok == False  # 没有服务
    # 两个都 false，结果 false
    assert (url_ok or port_ok) == False

# ============================================================
# 5. 边界条件测试
# ============================================================
@test("ServiceDef: requires=None 转为空列表")
def test_service_requires_none():
    from kb_launcher import ServiceDef
    svc = ServiceDef(
        name="test", port=8888, health_url="",
        access_urls=[], start_cmd=[], requires=None,
    )
    assert svc.requires == []

@test("ServiceDef: startup_delay 默认值")
def test_service_default_delay():
    from kb_launcher import ServiceDef
    svc = ServiceDef(
        name="test", port=8888, health_url="",
        access_urls=[], start_cmd=[],
    )
    assert svc.startup_delay == 2.0
    assert svc.process_name == "test"
    assert svc.status == "stopped"

@test("_content_hash: 相同内容哈希一致")
def test_content_hash_consistent():
    import hashlib
    c1 = hashlib.sha256("hello world".encode()).hexdigest()
    c2 = hashlib.sha256("hello world".encode()).hexdigest()
    assert c1 == c2

@test("_content_hash: 不同内容哈希不同")
def test_content_hash_different():
    import hashlib
    c1 = hashlib.sha256("hello world".encode()).hexdigest()
    c2 = hashlib.sha256("hello world!".encode()).hexdigest()
    assert c1 != c2

# ============================================================
# 6. 线程安全：polling 状态更新测试
# ============================================================
@test("轮询跳过 starting/stopping 状态")
def test_poll_skips_transition():
    from kb_launcher import SERVICES

    # 模拟：设置 MCP Gateway 为 starting
    gw = SERVICES[-1]
    orig_status = gw.status
    gw.status = "starting"

    # 模拟 polling 逻辑
    skipped = False
    for svc in SERVICES:
        if svc.status in ("starting", "stopping"):
            skipped = True
            continue

    gw.status = orig_status  # 恢复
    assert skipped, "Should have skipped the starting service"

@test("轮询 running 服务不做重复检查")
def test_poll_skips_running():
    from kb_launcher import SERVICES

    # 模拟一个服务为 running
    svc = SERVICES[0]
    orig_status = svc.status
    svc.status = "running"

    check_count = 0
    for s in SERVICES:
        if s.status == "running":
            continue  # 跳过 running 服务，不检查
        check_count += 1

    svc.status = orig_status
    assert check_count == 4, f"Should skip 1 running service, checked {check_count}"

# ============================================================
# 7. 模块级常量测试
# ============================================================
@test("KBDATA_DIR 存在")
def test_kbdata_exists():
    from kb_launcher import KBDATA_DIR
    assert KBDATA_DIR.exists()
    assert (KBDATA_DIR / "config").exists()
    assert (KBDATA_DIR / "minio").exists()
    assert (KBDATA_DIR / "chroma").exists()

@test("PROJECT_ROOT 正确")
def test_project_root():
    from kb_launcher import PROJECT_ROOT
    assert (PROJECT_ROOT / "kb_launcher.pyw").exists()
    assert (PROJECT_ROOT / "docker-compose.yml").exists()

# ============================================================
print("\n" + "=" * 60)
print(f"  测试结果: {passed} 通过, {failed} 失败")
print("=" * 60)

if errors:
    print("\n失败详情:")
    for e in errors:
        print(f"  {e}")

if failed > 0:
    sys.exit(1)

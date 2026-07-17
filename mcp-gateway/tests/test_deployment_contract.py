import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ONE_CLICK_LAUNCHERS = (
    "start.ps1",
    "start.sh",
    "start-dev.ps1",
    "start-dev.bat",
    "start-docker.bat",
)
SUPPORT_LAUNCHERS = (
    "init-config.bat",
    "stop-dev.bat",
    "start-desktop-shell.bat",
    "start-gui.bat",
)
GLOBAL_CLI_FILES = (
    "scripts/knowbase.ps1",
    "scripts/knowbase.sh",
    "scripts/knowbase.cmd",
    "scripts/knowbase",
    "scripts/install-cli.ps1",
    "scripts/install-cli.sh",
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _copy_launcher_fixture(tmp_path: Path, launcher: str) -> Path:
    shutil.copy2(ROOT / launcher, tmp_path / launcher)
    shutil.copy2(ROOT / ".env.example", tmp_path / ".env.example")
    profiles_dir = tmp_path / "deploy" / "profiles"
    profiles_dir.mkdir(parents=True)
    for profile in (ROOT / "deploy" / "profiles").glob("*.env"):
        shutil.copy2(profile, profiles_dir / profile.name)
    return tmp_path / launcher


def _copy_native_launcher_fixture(tmp_path: Path) -> Path:
    launcher = tmp_path / "start-dev.ps1"
    shutil.copy2(ROOT / "start-dev.ps1", launcher)
    shutil.copy2(ROOT / "start-dev.bat", tmp_path / "start-dev.bat")
    shutil.copy2(ROOT / ".env.example.local", tmp_path / ".env.example.local")
    profiles_dir = tmp_path / "deploy" / "profiles"
    profiles_dir.mkdir(parents=True)
    for profile in (ROOT / "deploy" / "profiles").glob("*.env"):
        shutil.copy2(profile, profiles_dir / profile.name)
    return launcher


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_compose_bootstraps_model_and_waits_for_gateway_health():
    compose = _read("docker-compose.yml")

    assert "ollama-model-init:" in compose
    assert "condition: service_completed_successfully" in compose
    gateway = compose.split("  mcp-gateway:", 1)[1].split("\nvolumes:", 1)[0]
    assert "healthcheck:" in gateway
    nginx = compose.split("  nginx:", 1)[1].split("\n  redis:", 1)[0]
    assert "condition: service_healthy" in nginx


def test_one_click_launchers_stay_at_root_and_support_launchers_are_centralized():
    for launcher_name in ONE_CLICK_LAUNCHERS:
        assert (ROOT / launcher_name).is_file(), f"missing one-click launcher: {launcher_name}"
        assert not (ROOT / "scripts" / launcher_name).exists()

    for launcher_name in SUPPORT_LAUNCHERS:
        canonical = ROOT / "scripts" / launcher_name
        assert canonical.is_file(), f"missing support launcher: {canonical}"
        assert not (ROOT / launcher_name).exists()
        launcher = canonical.read_text(encoding="utf-8")
        assert "%~dp0.." in launcher

    support_targets = {
        "init-config.bat": r"%ROOT_DIR%\start.ps1",
        "stop-dev.bat": r"%ROOT_DIR%\start-dev.ps1",
        "start-desktop-shell.bat": r"%ROOT_DIR%\desktop_shell.pyw",
        "start-gui.bat": r"%ROOT_DIR%\run_launcher.py",
    }
    for launcher_name, target in support_targets.items():
        assert target in _read(f"scripts/{launcher_name}")

    assert "$RootDir = $PSScriptRoot" in _read("start.ps1")
    assert "$RootDir = $PSScriptRoot" in _read("start-dev.ps1")
    assert 'dirname -- "$0")"' in _read("start.sh")


def test_docker_launchers_expose_the_same_small_command_interface():
    shell_launcher = _read("start.sh")
    powershell_launcher = _read("start.ps1")

    for command in ("up", "down", "status", "logs", "init", "configure"):
        assert command in shell_launcher
        assert command in powershell_launcher
    assert ".env.example" in shell_launcher
    assert ".env.example" in powershell_launcher
    assert "SESSION_SECRET" in shell_launcher
    assert "SESSION_SECRET" in powershell_launcher
    for key in (
        "DEPLOY_CONFIGURED",
        "DEPLOY_GPU_MODE",
        "DEPLOY_IMAGE_SOURCE",
        "DEPLOY_ACCESS_MODE",
        "DEPLOY_TUNNEL_MODE",
    ):
        assert key in shell_launcher
        assert key in powershell_launcher
        assert key in _read(".env.example")


def test_global_cli_contract_is_cross_platform_and_wired_into_deployment_launchers():
    for relative_path in GLOBAL_CLI_FILES:
        assert (ROOT / relative_path).is_file(), f"missing global CLI file: {relative_path}"

    powershell_cli = _read("scripts/knowbase.ps1")
    shell_cli = _read("scripts/knowbase.sh")
    for command in (
        "gateway",
        "health",
        "configure",
        "restart",
        "status",
        "logs",
        "cli",
        "home",
    ):
        assert command in powershell_cli
        assert command in shell_cli

    powershell_launcher = _read("start.ps1")
    shell_launcher = _read("start.sh")
    for command in ("cli-install", "cli-uninstall", "cli-status"):
        assert command in powershell_launcher
        assert command in shell_launcher
    assert "-InstallCli" in powershell_launcher
    assert "--install-cli" in shell_launcher


def test_global_cli_shell_scripts_have_valid_posix_syntax():
    shell = shutil.which("sh")
    if not shell:
        common_git_shell = Path(r"C:\Program Files\Git\bin\sh.exe")
        if common_git_shell.is_file():
            shell = str(common_git_shell)
    if not shell:
        pytest.skip("POSIX sh is not available")

    subprocess.run(
        [
            shell,
            "-n",
            "scripts/knowbase.sh",
            "scripts/install-cli.sh",
            "scripts/knowbase",
            "start.sh",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )


def test_windows_global_cli_installs_routes_and_uninstalls_in_isolation(tmp_path: Path):
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    cmd = shutil.which("cmd.exe")
    if not powershell or not cmd:
        pytest.skip("Windows PowerShell and cmd.exe are required")

    installer = ROOT / "scripts" / "install-cli.ps1"
    bin_dir = tmp_path / "bin"
    install_command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer),
        "-Action",
        "install",
        "-Scope",
        "Process",
        "-BinDir",
        str(bin_dir),
        "-Quiet",
    ]
    subprocess.run(install_command, cwd=tmp_path, check=True, capture_output=True)

    shim = bin_dir / "knowbase.cmd"
    assert shim.is_file()
    assert (bin_dir / "knowbase-home.txt").read_text(encoding="utf-8").strip() == str(ROOT)

    routed = subprocess.run(
        [cmd, "/d", "/c", str(shim), "home"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(ROOT).lower() in routed.stdout.strip().lower()

    subprocess.run(
        [
            cmd,
            "/d",
            "/c",
            str(shim),
            "cli",
            "uninstall",
            "-Scope",
            "Process",
            "-BinDir",
            str(bin_dir),
            "-Quiet",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    for _ in range(50):
        if not shim.exists():
            break
        time.sleep(0.1)
    assert not shim.exists()
    assert not (bin_dir / "knowbase-home.txt").exists()


def test_windows_default_cli_is_discoverable_without_refreshing_terminal_path(tmp_path: Path):
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    cmd = shutil.which("cmd.exe")
    if not powershell or not cmd:
        pytest.skip("Windows PowerShell and cmd.exe are required")

    local_app_data = tmp_path / "LocalAppData"
    windows_apps = local_app_data / "Microsoft" / "WindowsApps"
    windows_apps.mkdir(parents=True)
    stale_env = os.environ.copy()
    for key in list(stale_env):
        if key.lower() in {"path", "localappdata"}:
            stale_env.pop(key)
    stale_env["LOCALAPPDATA"] = str(local_app_data)
    stale_env["PATH"] = str(windows_apps) + os.pathsep + os.environ["PATH"]

    installer = ROOT / "scripts" / "install-cli.ps1"
    subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            "-Action",
            "install",
            "-Scope",
            "Process",
            "-Quiet",
        ],
        cwd=tmp_path,
        env=stale_env,
        check=True,
        capture_output=True,
    )

    shim = windows_apps / "knowbase.cmd"
    home_file = local_app_data / "KnowledgeBaseManagement" / "knowbase-home.txt"
    assert shim.is_file()
    assert home_file.read_text(encoding="utf-8").strip() == str(ROOT)
    routed = subprocess.run(
        [cmd, "/d", "/c", "knowbase", "home"],
        cwd=tmp_path,
        env=stale_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(ROOT).lower() in routed.stdout.strip().lower()

    subprocess.run(
        [cmd, "/d", "/c", "knowbase", "cli", "uninstall", "-Scope", "Process", "-Quiet"],
        cwd=tmp_path,
        env=stale_env,
        check=True,
        capture_output=True,
    )
    for _ in range(50):
        if not shim.exists():
            break
        time.sleep(0.1)
    assert not shim.exists()
    assert not home_file.exists()
    assert windows_apps.is_dir()


def test_windows_global_cli_forwards_deployment_options_from_any_working_directory(tmp_path: Path):
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not available")

    _copy_launcher_fixture(tmp_path, "start.ps1")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(ROOT / "scripts" / "knowbase.ps1", scripts_dir / "knowbase.ps1")
    caller_dir = tmp_path / "unrelated-cwd"
    caller_dir.mkdir()
    subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "knowbase.ps1"),
            "init",
            "-NonInteractive",
            "-Profile",
            "minimum",
            "-Gpu",
            "cpu",
            "-Source",
            "official",
        ],
        cwd=caller_dir,
        check=True,
        capture_output=True,
    )

    env = _read_env(tmp_path / ".env")
    assert env["HARDWARE_PROFILE"] == "minimum"
    assert env["DEPLOY_GPU_MODE"] == "cpu"
    assert env["DEPLOY_IMAGE_SOURCE"] == "official"


def test_linux_global_cli_installs_routes_and_uninstalls_in_isolation(tmp_path: Path):
    shell = shutil.which("sh")
    if not shell:
        common_git_shell = Path(r"C:\Program Files\Git\bin\sh.exe")
        if common_git_shell.is_file():
            shell = str(common_git_shell)
    if not shell:
        pytest.skip("POSIX sh is not available")

    home_dir = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    if os.name == "nt":
        command = r'''
home_dir=$(cygpath -u "$1")
bin_dir=$(cygpath -u "$2")
export HOME="$home_dir"
export XDG_CONFIG_HOME="$home_dir/config"
export SHELL=/bin/bash
export KNOWBASE_CLI_BIN_DIR="$bin_dir"
mkdir -p "$HOME"
sh scripts/install-cli.sh install --quiet
"$bin_dir/knowbase" home
sh scripts/install-cli.sh status --quiet
grep -q 'knowbase CLI' "$HOME/.profile"
grep -q 'knowbase CLI' "$HOME/.bashrc"
"$bin_dir/knowbase" cli uninstall --bin-dir "$bin_dir" --quiet
test ! -e "$bin_dir/knowbase"
test ! -e "$XDG_CONFIG_HOME/knowbase/home"
! grep -q 'knowbase CLI' "$HOME/.profile"
! grep -q 'knowbase CLI' "$HOME/.bashrc"
'''
        routed = subprocess.run(
            [shell, "-c", command, "knowbase-test", str(home_dir), str(bin_dir)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home_dir),
                "XDG_CONFIG_HOME": str(home_dir / "config"),
                "SHELL": "/bin/bash",
                "KNOWBASE_CLI_BIN_DIR": str(bin_dir),
            }
        )
        home_dir.mkdir()
        command = """
set -e
sh scripts/install-cli.sh install --quiet
"$KNOWBASE_CLI_BIN_DIR/knowbase" home
sh scripts/install-cli.sh status --quiet
grep -q 'knowbase CLI' "$HOME/.profile"
grep -q 'knowbase CLI' "$HOME/.bashrc"
"$KNOWBASE_CLI_BIN_DIR/knowbase" cli uninstall --bin-dir "$KNOWBASE_CLI_BIN_DIR" --quiet
test ! -e "$KNOWBASE_CLI_BIN_DIR/knowbase"
test ! -e "$XDG_CONFIG_HOME/knowbase/home"
! grep -q 'knowbase CLI' "$HOME/.profile"
! grep -q 'knowbase CLI' "$HOME/.bashrc"
"""
        routed = subprocess.run(
            [shell, "-c", command],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    assert "knowledge-base-management" in routed.stdout


def test_powershell_launcher_generates_and_reconfigures_noninteractive_env(tmp_path: Path):
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not available")

    launcher = _copy_launcher_fixture(tmp_path, "start.ps1")
    caller_dir = tmp_path / "unrelated-cwd"
    caller_dir.mkdir()
    initialized = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "init",
            "-NonInteractive",
            "-Profile",
            "minimum",
            "-Gpu",
            "cpu",
            "-Source",
            "official",
        ],
        cwd=caller_dir,
        check=True,
        capture_output=True,
    )

    env = _read_env(tmp_path / ".env")
    assert env["HARDWARE_PROFILE"] == "minimum"
    assert env["DEPLOY_CONFIGURED"] == "true"
    assert env["DEPLOY_GPU_MODE"] == "cpu"
    assert env["DEPLOY_IMAGE_SOURCE"] == "official"
    assert len(env["SESSION_SECRET"]) >= 32
    assert env["MINIO_ROOT_PASSWORD"] == env["MINIO_SECRET_KEY"]
    assert env["SESSION_SECRET"].encode() not in initialized.stdout + initialized.stderr
    assert env["MINIO_ROOT_PASSWORD"].encode() not in initialized.stdout + initialized.stderr

    original_session_secret = env["SESSION_SECRET"]
    original_minio_password = env["MINIO_ROOT_PASSWORD"]
    subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "configure",
            "-NonInteractive",
            "-Profile",
            "high-performance",
            "-Gpu",
            "auto",
            "-Source",
            "mainland",
            "-Tunnel",
            "off",
        ],
        cwd=caller_dir,
        check=True,
        capture_output=True,
    )

    reconfigured = _read_env(tmp_path / ".env")
    assert reconfigured["HARDWARE_PROFILE"] == "high-performance"
    assert reconfigured["DEPLOY_CONFIGURED"] == "true"
    assert reconfigured["DEPLOY_GPU_MODE"] == "auto"
    assert reconfigured["DEPLOY_IMAGE_SOURCE"] == "mainland"
    assert reconfigured["DEPLOY_TUNNEL_MODE"] == "off"
    assert reconfigured["SESSION_SECRET"] == original_session_secret
    assert reconfigured["MINIO_ROOT_PASSWORD"] == original_minio_password


def test_shell_launcher_generates_and_reconfigures_noninteractive_env(tmp_path: Path):
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("POSIX sh is not available")

    launcher = _copy_launcher_fixture(tmp_path, "start.sh")
    caller_dir = tmp_path / "unrelated-cwd"
    caller_dir.mkdir()
    initialized = subprocess.run(
        [
            shell,
            str(launcher),
            "init",
            "--non-interactive",
            "--profile",
            "minimum",
            "--cpu",
            "--source",
            "official",
        ],
        cwd=caller_dir,
        check=True,
        capture_output=True,
    )

    env = _read_env(tmp_path / ".env")
    assert env["HARDWARE_PROFILE"] == "minimum"
    assert env["DEPLOY_CONFIGURED"] == "true"
    assert env["DEPLOY_GPU_MODE"] == "cpu"
    assert env["DEPLOY_IMAGE_SOURCE"] == "official"
    assert len(env["SESSION_SECRET"]) >= 32
    assert env["MINIO_ROOT_PASSWORD"] == env["MINIO_SECRET_KEY"]
    assert env["SESSION_SECRET"].encode() not in initialized.stdout + initialized.stderr
    assert env["MINIO_ROOT_PASSWORD"].encode() not in initialized.stdout + initialized.stderr

    original_session_secret = env["SESSION_SECRET"]
    original_minio_password = env["MINIO_ROOT_PASSWORD"]
    subprocess.run(
        [
            shell,
            str(launcher),
            "configure",
            "--non-interactive",
            "--profile",
            "high-performance",
            "--source",
            "mainland",
            "--tunnel",
            "off",
        ],
        cwd=caller_dir,
        check=True,
        capture_output=True,
    )

    reconfigured = _read_env(tmp_path / ".env")
    assert reconfigured["HARDWARE_PROFILE"] == "high-performance"
    assert reconfigured["DEPLOY_CONFIGURED"] == "true"
    assert reconfigured["DEPLOY_GPU_MODE"] == "cpu"
    assert reconfigured["DEPLOY_IMAGE_SOURCE"] == "mainland"
    assert reconfigured["DEPLOY_TUNNEL_MODE"] == "off"
    assert reconfigured["SESSION_SECRET"] == original_session_secret
    assert reconfigured["MINIO_ROOT_PASSWORD"] == original_minio_password


def test_windows_native_launcher_uses_isolated_local_env_and_scoped_stop():
    launcher = _read("start-dev.ps1")

    assert ".env.local" in launcher
    assert "Test-PythonDependencies" in launcher
    assert "[switch]$InitOnly" in launcher
    assert "[switch]$Background" in launcher
    assert "Test-RedisReady" in launcher
    assert "Start-Service -Name \"Memurai\"" in launcher
    assert "Wait-GatewayReady" in launcher
    assert "All services are healthy!" in launcher
    assert ".Split(@('='), 2)" not in launcher
    assert '@{Name="mcp-gateway";   Process="python"}' not in launcher
    assert "$env:BIND_HOST" in launcher


def test_windows_native_launcher_initializes_profile_with_windows_powershell(tmp_path: Path):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is not available")

    launcher = _copy_native_launcher_fixture(tmp_path)
    caller_dir = tmp_path / "unrelated-cwd"
    caller_dir.mkdir()
    subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "-InitOnly",
            "-Quiet",
            "-Profile",
            "minimum",
        ],
        cwd=caller_dir,
        check=True,
        capture_output=True,
    )

    env = _read_env(tmp_path / ".env.local")
    assert env["HARDWARE_PROFILE"] == "minimum"
    assert env["SEARCH_MAX_CONCURRENCY"] == "4"
    assert not (tmp_path / "kbdata").exists()


def test_windows_native_batch_launcher_is_non_blocking_and_preserves_exit_code(tmp_path: Path):
    cmd = shutil.which("cmd.exe")
    if not cmd:
        pytest.skip("Windows cmd.exe is not available")

    _copy_native_launcher_fixture(tmp_path)
    batch = tmp_path / "start-dev.bat"
    caller_dir = tmp_path / "unrelated-cwd"
    caller_dir.mkdir()
    initialized = subprocess.run(
        [cmd, "/d", "/c", str(batch), "-InitOnly", "-Quiet", "-Profile", "minimum"],
        cwd=caller_dir,
        check=False,
        capture_output=True,
        input=b"\n",
        timeout=15,
    )
    assert initialized.returncode == 0

    rejected = subprocess.run(
        [cmd, "/d", "/c", str(batch), "-InitOnly", "-Quiet", "-Profile", "invalid"],
        cwd=caller_dir,
        check=False,
        capture_output=True,
        input=b"\n",
        timeout=15,
    )
    assert rejected.returncode != 0


def test_windows_native_defaults_use_ipv4_loopback_for_local_dependencies():
    env = _read_env(ROOT / ".env.example.local")
    launcher = _read("start-dev.ps1")

    assert env["REDIS_URL"] == "redis://127.0.0.1:6379/0"
    assert env["CHROMA_HOST"] == "127.0.0.1"
    assert env["OLLAMA_URL"] == "http://127.0.0.1:11434"
    assert env["MINIO_ENDPOINT"] == "127.0.0.1:9000"
    assert "Normalize legacy localhost values for Windows IPv4 services" in launcher


def test_default_sources_prefer_mainland_mirrors_and_have_official_fallback():
    env_example = _read(".env.example")
    official = _read("docker-compose.official.yml")
    shell_launcher = _read("start.sh")
    powershell_launcher = _read("start.ps1")

    assert "MIRROR_PREFIX=m.daocloud.io/docker.io/" in env_example
    assert "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in env_example
    assert "APT_MIRROR=mirrors.tuna.tsinghua.edu.cn" in env_example
    assert "image: nginx:1.27-alpine" in official
    assert "PIP_INDEX_URL: https://pypi.org/simple" in official
    assert "docker-compose.official.yml" in shell_launcher
    assert "docker-compose.official.yml" in powershell_launcher


def test_hardware_profiles_and_runtime_concurrency_are_configurable():
    compose = _read("docker-compose.yml")
    minimum = _read("deploy/profiles/minimum.env")
    recommended = _read("deploy/profiles/recommended.env")
    high_performance = _read("deploy/profiles/high-performance.env")

    assert "SEARCH_MAX_CONCURRENCY=${SEARCH_MAX_CONCURRENCY:-12}" in compose
    assert "EMBEDDING_MAX_CONNECTIONS=${EMBEDDING_MAX_CONNECTIONS:-24}" in compose
    assert "SEARCH_MAX_CONCURRENCY=4" in minimum
    assert "SEARCH_MAX_CONCURRENCY=12" in recommended
    assert "SEARCH_MAX_CONCURRENCY=20" in high_performance
    assert "GRAPH_RETRIEVAL_MAX_HOPS=1" in minimum
    assert "GRAPH_RETRIEVAL_MAX_RESULTS=5" in high_performance


def test_windows_native_launcher_can_bootstrap_system_dependencies_with_fallbacks():
    launcher = _read("start-dev.ps1")

    assert "Python.Python.3.13" in launcher
    assert "Ollama.Ollama" in launcher
    assert "Memurai.MemuraiDeveloper" in launcher
    assert "mirrors.tuna.tsinghua.edu.cn/python" in launcher
    assert "www.python.org/ftp/python" in launcher
    assert "PIP_FALLBACK_INDEX_URL" in launcher
    assert "dl.min.io/community/server/minio" in launcher
    assert "$NoAutoInstall" in launcher


def test_optional_cloudflare_tunnel_is_profile_gated():
    compose = _read("docker-compose.yml")
    shell_launcher = _read("start.sh")
    powershell_launcher = _read("start.ps1")

    cloudflared = compose.split("  cloudflared:", 1)[1].split("\n  redis:", 1)[0]
    assert 'profiles: ["tunnel"]' in cloudflared
    assert "TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN:-}" in cloudflared
    assert "http://nginx:80" in compose
    assert "--tunnel cloudflare" in shell_launcher
    assert "-Tunnel cloudflare" in powershell_launcher


def test_deployment_and_graph_retrieval_docs_are_present():
    guide = _read("部署与容量配置指南.md")
    readme = _read("README.md")

    assert "10–20" in guide
    assert "GRAPH_RETRIEVAL_WEIGHT" in guide
    assert "Cloudflare Tunnel" in guide
    assert "retrieval_index.json" in readme

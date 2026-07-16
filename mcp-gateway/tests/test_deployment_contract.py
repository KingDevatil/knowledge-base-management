from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_compose_bootstraps_model_and_waits_for_gateway_health():
    compose = _read("docker-compose.yml")

    assert "ollama-model-init:" in compose
    assert "condition: service_completed_successfully" in compose
    gateway = compose.split("  mcp-gateway:", 1)[1].split("\nvolumes:", 1)[0]
    assert "healthcheck:" in gateway
    nginx = compose.split("  nginx:", 1)[1].split("\n  redis:", 1)[0]
    assert "condition: service_healthy" in nginx


def test_docker_launchers_expose_the_same_small_command_interface():
    shell_launcher = _read("start.sh")
    powershell_launcher = _read("start.ps1")

    for command in ("up", "down", "status", "logs", "init"):
        assert command in shell_launcher
        assert command in powershell_launcher
    assert ".env.example" in shell_launcher
    assert ".env.example" in powershell_launcher
    assert "SESSION_SECRET" in shell_launcher
    assert "SESSION_SECRET" in powershell_launcher


def test_windows_native_launcher_uses_isolated_local_env_and_scoped_stop():
    launcher = _read("start-dev.ps1")

    assert ".env.local" in launcher
    assert "Test-PythonDependencies" in launcher
    assert '@{Name="mcp-gateway";   Process="python"}' not in launcher
    assert "$env:BIND_HOST" in launcher

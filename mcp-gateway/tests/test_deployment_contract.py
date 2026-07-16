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

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.check_deps_sync import dependency_diff, parse_requirement_line


def test_parse_requirement_line_normalizes_extras_and_version():
    assert parse_requirement_line("uvicorn[standard]==0.44.0") == ("uvicorn", "==0.44.0")
    assert parse_requirement_line("graphifyy>=0.8.22") == ("graphifyy", ">=0.8.22")
    assert parse_requirement_line("# comment") is None


def test_dependency_diff_detects_mismatches():
    issues = dependency_diff(
        {"fastapi": "==1.0.0", "redis": "==7.4.0"},
        {"fastapi": "==1.0.1", "redis": "==7.4.0", "minio": "==7.2.20"},
    )

    assert "fastapi: requirements='==1.0.0', pyproject='==1.0.1'" in issues
    assert "minio: requirements=None, pyproject='==7.2.20'" in issues


def test_lock_diff_prefix_is_human_readable():
    issues = [f"lock {issue}" for issue in dependency_diff({"fastapi": "==1"}, {"fastapi": "==2"})]

    assert issues == ["lock fastapi: requirements='==1', pyproject='==2'"]

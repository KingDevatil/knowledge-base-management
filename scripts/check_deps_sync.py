"""Verify mcp-gateway requirements.txt and pyproject.toml dependency versions match."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


EXTRAS_RE = re.compile(r"\[[^\]]+\]")


def normalize_name(name: str) -> str:
    return name.lower().replace("_", "-")


def normalize_spec(spec: str) -> str:
    return spec.strip().strip('"').strip("'")


def parse_requirement_line(line: str) -> tuple[str, str] | None:
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    for operator in ("==", ">=", "<=", "~=", ">", "<"):
        if operator in line:
            name, version = line.split(operator, 1)
            name = EXTRAS_RE.sub("", name).strip()
            return normalize_name(name), f"{operator}{normalize_spec(version)}"
    name = EXTRAS_RE.sub("", line).strip()
    return normalize_name(name), ""


def parse_requirements(path: Path) -> dict[str, str]:
    deps: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_requirement_line(line)
        if parsed:
            deps[parsed[0]] = parsed[1]
    return deps


def parse_pyproject(path: Path) -> dict[str, str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    deps: dict[str, str] = {}
    for name, raw_spec in poetry_deps.items():
        normalized = normalize_name(name)
        if normalized == "python":
            continue
        if isinstance(raw_spec, str):
            deps[normalized] = raw_spec if raw_spec.startswith((">", "<", "=", "~", "^")) else f"=={raw_spec}"
        elif isinstance(raw_spec, dict):
            version = raw_spec.get("version", "")
            deps[normalized] = version if version.startswith((">", "<", "=", "~", "^")) else f"=={version}"
    return deps


def dependency_diff(requirements: dict[str, str], pyproject: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for name in sorted(set(requirements) | set(pyproject)):
        req_spec = requirements.get(name)
        py_spec = pyproject.get(name)
        if req_spec != py_spec:
            issues.append(f"{name}: requirements={req_spec!r}, pyproject={py_spec!r}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root")
    args = parser.parse_args(argv)

    root = Path(args.root)
    requirements_path = root / "mcp-gateway" / "requirements.txt"
    lock_path = root / "mcp-gateway" / "requirements.lock.txt"
    requirements = parse_requirements(requirements_path)
    pyproject = parse_pyproject(root / "mcp-gateway" / "pyproject.toml")
    locked = parse_requirements(lock_path)
    issues = dependency_diff(requirements, pyproject)
    issues.extend(f"lock {issue}" for issue in dependency_diff(requirements, locked))

    if issues:
        print("Dependency declarations are out of sync:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Dependency declarations are in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

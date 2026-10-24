"""Shared helpers for the local evaluation scripts."""
from __future__ import annotations
from pathlib import Path

#: repository root (this file lives in <root>/_local_eval/)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "admin_console" / "config.properties"


def load_config(path: Path | None = None) -> dict:
    """Parse admin_console/config.properties into a flat dict. Values are never printed."""
    path = path or CONFIG_PATH
    if not path.exists():
        raise SystemExit(
            f"Missing {path}\n"
            "Copy admin_console/config.properties.example to config.properties and fill it in."
        )
    cfg: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def java_executable(cfg: dict) -> str:
    """JVM used to launch the demo jars; override with java_executable in config.properties."""
    return cfg.get("java_executable", "java")

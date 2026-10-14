"""Collector configuration (TOML). Tokens are never stored in the file: use token_env or token_file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "vibevault" / "collector.toml"


@dataclass
class Destination:
    name: str
    url: str
    token_env: str | None = None
    token_file: str | None = None

    def token(self) -> str | None:
        if self.token_env and os.environ.get(self.token_env):
            return os.environ[self.token_env].strip()
        if self.token_file:
            p = Path(self.token_file).expanduser()
            if p.exists():
                if p.stat().st_mode & 0o077:
                    raise PermissionError(f"{p} must not be readable by group/others (chmod 600)")
                return p.read_text().strip()
        return None


@dataclass
class CollectorConfig:
    enabled: bool = True
    device_id: str = "unnamed-device"
    device_label: str | None = None
    spool_dir: Path = field(default_factory=lambda: Path("~/.local/share/vibevault-collector").expanduser())
    destinations: list[Destination] = field(default_factory=list)
    batch_size: int = 100
    request_timeout: float = 15.0
    # claude code
    projects_dir: Path = field(default_factory=lambda: Path("~/.claude/projects").expanduser())
    exclude_paths: list[str] = field(default_factory=list)
    capture_thinking: bool = False
    max_content_chars: int = 200_000
    include_raw_payload: bool = True
    max_raw_payload_chars: int = 200_000
    redaction_rules_file: str | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> CollectorConfig:
        p = Path(path or os.environ.get("VIBEVAULT_COLLECTOR_CONFIG") or DEFAULT_CONFIG).expanduser()
        raw = tomllib.loads(p.read_text()) if p.exists() else {}
        c, cc, cr, cd = raw.get("collector", {}), raw.get("claude_code", {}), raw.get("redaction", {}), raw.get("delivery", {})
        cfg = cls(
            enabled=c.get("enabled", True),
            device_id=c.get("device_id", cls.device_id),
            device_label=c.get("device_label"),
            batch_size=cd.get("batch_size", 100),
            request_timeout=cd.get("request_timeout", 15.0),
            exclude_paths=[str(Path(x).expanduser()) for x in cc.get("exclude_paths", [])],
            capture_thinking=cc.get("capture_thinking", False),
            max_content_chars=cc.get("max_content_chars", 200_000),
            include_raw_payload=cc.get("include_raw_payload", True),
            max_raw_payload_chars=cc.get("max_raw_payload_chars", 200_000),
            redaction_rules_file=cr.get("rules_file") or None,
        )
        if c.get("spool_dir"):
            cfg.spool_dir = Path(c["spool_dir"]).expanduser()
        if cc.get("projects_dir"):
            cfg.projects_dir = Path(cc["projects_dir"]).expanduser()
        for d in raw.get("destination", []):
            cfg.destinations.append(Destination(d["name"], d["url"], d.get("token_env"), d.get("token_file")))
        return cfg

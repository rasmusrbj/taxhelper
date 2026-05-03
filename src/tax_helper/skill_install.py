from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from typing import Iterable


SKILL_NAME = "taxhelper"
SUPPORTED_TARGETS = ("codex", "claude", "agents")
DEFAULT_TARGETS = ("codex", "claude")


@dataclass(frozen=True)
class SkillInstallResult:
    target: str
    path: Path
    installed: bool
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "path": str(self.path),
            "installed": self.installed,
            "skipped_reason": self.skipped_reason,
        }


def install_skill_bundle(
    *,
    targets: Iterable[str] | None = None,
    custom_roots: Iterable[Path] = (),
    force: bool = False,
) -> list[SkillInstallResult]:
    destinations = default_destinations(targets)
    destinations.extend(("custom", Path(root).expanduser()) for root in custom_roots)
    if not destinations:
        raise ValueError("no skill install targets selected")

    skill_source = files("tax_helper").joinpath("skills", SKILL_NAME)
    results: list[SkillInstallResult] = []
    with as_file(skill_source) as source_path:
        if not source_path.is_dir():
            raise ValueError(f"bundled skill not found: {source_path}")
        for target, root in destinations:
            destination = root.expanduser() / SKILL_NAME
            if destination.exists() and not force:
                results.append(
                    SkillInstallResult(
                        target=target,
                        path=destination,
                        installed=False,
                        skipped_reason="already exists; pass --force to replace it",
                    )
                )
                continue
            if destination.exists():
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_path, destination)
            results.append(SkillInstallResult(target=target, path=destination, installed=True))
    return results


def default_destinations(targets: Iterable[str] | None = None) -> list[tuple[str, Path]]:
    selected = DEFAULT_TARGETS if targets is None else tuple(targets)
    destinations: list[tuple[str, Path]] = []
    for target in selected:
        if target not in SUPPORTED_TARGETS:
            allowed = ", ".join(SUPPORTED_TARGETS)
            raise ValueError(f"unknown skill target: {target}; expected one of {allowed}")
        destinations.append((target, default_skills_root(target)))
    return destinations


def default_skills_root(target: str) -> Path:
    if target == "codex":
        return config_home(("CODEX_HOME",), "~/.codex") / "skills"
    if target == "claude":
        return config_home(("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"), "~/.claude") / "skills"
    if target == "agents":
        return config_home(("AGENTS_HOME",), "~/.agents") / "skills"
    raise ValueError(f"unknown skill target: {target}")


def config_home(env_names: tuple[str, ...], default: str) -> Path:
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser()
    return Path(default).expanduser()

"""Load standard SKILL.md packages without widening runtime permissions.

`name` and `description` are the standard required frontmatter fields.
`version` and `allowed-tools` are optional project extensions. Declared tools
are always intersected with the tools visible to the current actor.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from agent.env_loader import PROJECT_ROOT


BOM = chr(0xFEFF)
DEFAULT_EXTERNAL_SKILLS_DIR = PROJECT_ROOT / "data" / "skills"
SKILL_MANIFEST_NAME = "SKILL.md"
EXTERNAL_ORIGIN = "external"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_DESCRIPTION_CHARS = 1024
MAX_RESOURCES = 50
DEFAULT_BODY_CHAR_LIMIT = 4000

_NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")


class SkillPackageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SkillPackage:
    name: str
    description: str
    version: str
    body_markdown: str
    folder: str
    directory: str
    allowed_tools: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "origin": EXTERNAL_ORIGIN,
            "status": "loaded",
            "error": "",
            "folder": self.folder,
            "allowed_tools": list(self.allowed_tools),
            "resources": list(self.resources),
            "body_chars": len(self.body_markdown),
        }

    def search_row(
        self,
        *,
        allowed_tool_names: set[str] | None = None,
        body_char_limit: int = DEFAULT_BODY_CHAR_LIMIT,
    ) -> dict[str, Any]:
        tool_names = list(self.allowed_tools)
        if allowed_tool_names is not None:
            tool_names = [name for name in tool_names if name in allowed_tool_names]
        limit = max(200, int(body_char_limit))
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "body_markdown": self.body_markdown[:limit],
            "body_truncated": len(self.body_markdown) > limit,
            "tool_names": tool_names,
            "origin": EXTERNAL_ORIGIN,
            "package": self.folder,
        }


@dataclass(frozen=True)
class SkillPackageIssue:
    folder: str
    error: str
    name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name or self.folder,
            "description": "",
            "version": "",
            "origin": EXTERNAL_ORIGIN,
            "status": "error",
            "error": self.error,
            "folder": self.folder,
            "allowed_tools": [],
            "resources": [],
            "body_chars": 0,
        }


@dataclass(frozen=True)
class SkillPackageIndex:
    directory: str
    packages: tuple[SkillPackage, ...] = ()
    issues: tuple[SkillPackageIssue, ...] = ()


def external_skills_dir() -> Path:
    configured = os.getenv("EXTERNAL_SKILLS_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_EXTERNAL_SKILLS_DIR


def split_skill_frontmatter(text: str) -> tuple[str, str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip(BOM)
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillPackageValidationError("SKILL.md 必须以 --- 开头的 YAML frontmatter")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:]).strip()
    raise SkillPackageValidationError("SKILL.md 的 frontmatter 缺少结束的 --- 分隔符")


def parse_frontmatter(block: str) -> dict[str, Any]:
    try:
        value = yaml.load(str(block or ""), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise SkillPackageValidationError(f"SKILL.md frontmatter 不是合法 YAML：{exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SkillPackageValidationError("SKILL.md frontmatter 顶层必须是 YAML 对象")
    return value


def build_skill_package(
    text: str,
    *,
    directory: str | Path,
    resources: Sequence[str] = (),
) -> SkillPackage:
    block, body = split_skill_frontmatter(text)
    front = parse_frontmatter(block)
    name = _text_field(front, "name")
    if not name:
        raise SkillPackageValidationError("SKILL.md frontmatter 缺少 name")
    if _NAME_PATTERN.fullmatch(name) is None:
        raise SkillPackageValidationError(
            f"skill name 只允许小写字母、数字和连字符，且不超过 64 字符：{name}"
        )
    description = " ".join(_text_field(front, "description").split())
    if not description:
        raise SkillPackageValidationError("SKILL.md frontmatter 缺少 description")
    if not body.strip():
        raise SkillPackageValidationError("SKILL.md 缺少正文，无法说明何时使用和怎么做")
    package_dir = Path(directory)
    if package_dir.name != name:
        raise SkillPackageValidationError(
            f"Skill 文件夹名必须与 frontmatter name 一致：{package_dir.name} != {name}"
        )
    return SkillPackage(
        name=name,
        description=description[:MAX_DESCRIPTION_CHARS],
        version=_text_field(front, "version") or "1.0.0",
        body_markdown=body,
        folder=package_dir.name,
        directory=str(package_dir),
        allowed_tools=_tool_list(
            front.get("allowed-tools") if "allowed-tools" in front else front.get("allowed_tools")
        ),
        resources=tuple(str(item) for item in resources),
    )


def load_skill_package(directory: str | Path) -> SkillPackage:
    package_dir = Path(directory)
    manifest = package_dir / SKILL_MANIFEST_NAME
    if not manifest.is_file():
        raise SkillPackageValidationError(f"目录缺少 {SKILL_MANIFEST_NAME}")
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise SkillPackageValidationError(
            f"{SKILL_MANIFEST_NAME} 超过 {MAX_MANIFEST_BYTES} 字节上限"
        )
    try:
        text = manifest.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillPackageValidationError(f"{SKILL_MANIFEST_NAME} 不是 UTF-8 文本") from exc
    return build_skill_package(text, directory=package_dir, resources=_resources(package_dir))


def load_skill_packages(directory: str | Path | None = None) -> SkillPackageIndex:
    root = Path(directory) if directory is not None else external_skills_dir()
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return SkillPackageIndex(directory=str(root))
    packages: list[SkillPackage] = []
    issues: list[SkillPackageIssue] = []
    claimed: dict[str, str] = {}
    for entry in entries:
        if entry.name.startswith((".", "_")) or not entry.is_dir():
            continue
        if not (entry / SKILL_MANIFEST_NAME).is_file():
            continue
        try:
            package = load_skill_package(entry)
        except SkillPackageValidationError as exc:
            issues.append(SkillPackageIssue(folder=entry.name, error=str(exc)))
            continue
        except OSError as exc:
            issues.append(SkillPackageIssue(folder=entry.name, error=f"读取失败：{exc}"))
            continue
        if package.name in claimed:
            issues.append(SkillPackageIssue(
                folder=entry.name,
                name=package.name,
                error=f"skill name 与目录 {claimed[package.name]} 重复",
            ))
            continue
        claimed[package.name] = entry.name
        packages.append(package)
    return SkillPackageIndex(
        directory=str(root),
        packages=tuple(packages),
        issues=tuple(issues),
    )


def search_skill_packages(
    query: str,
    *,
    packages: Sequence[SkillPackage] | None = None,
    allowed_tool_names: set[str] | None = None,
    limit: int = 3,
    body_char_limit: int = DEFAULT_BODY_CHAR_LIMIT,
) -> list[dict[str, Any]]:
    candidates = list(packages) if packages is not None else list(load_skill_packages().packages)
    terms = _query_terms(query)
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for package in candidates:
        text = f"{package.name} {package.description}".lower()
        score = sum(1 for term in terms if term in text)
        if terms and score == 0:
            continue
        ranked.append((
            score,
            package.name,
            package.search_row(
                allowed_tool_names=allowed_tool_names,
                body_char_limit=body_char_limit,
            ),
        ))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row for _, _, row in ranked[: max(1, min(int(limit), 10))]]


def _resources(package_dir: Path) -> tuple[str, ...]:
    names: list[str] = []
    try:
        for path in sorted(package_dir.rglob("*")):
            if len(names) >= MAX_RESOURCES:
                break
            if path.name == SKILL_MANIFEST_NAME or not path.is_file():
                continue
            names.append(path.relative_to(package_dir).as_posix())
    except OSError:
        return tuple(names)
    return tuple(names)


def _text_field(front: dict[str, Any], key: str) -> str:
    value = front.get(key, "")
    if isinstance(value, (list, dict)):
        raise SkillPackageValidationError(f"frontmatter 字段 {key} 必须是一行文本")
    return str(value or "").strip()


def _tool_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise SkillPackageValidationError("frontmatter 字段 allowed-tools 必须是文本或文本列表")
    invalid = [part for part in parts if _TOOL_NAME_PATTERN.fullmatch(part) is None]
    if invalid:
        raise SkillPackageValidationError(f"allowed-tools 包含非法工具名：{', '.join(invalid)}")
    return tuple(dict.fromkeys(parts))


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _query_terms(query: str) -> list[str]:
    normalized = " ".join(str(query or "").lower().split())
    if not normalized:
        return []
    terms = [part for part in re.split(r"[\s,，。；;：:、]+", normalized) if part]
    if len(terms) == 1 and len(terms[0]) > 2:
        terms.extend(terms[0][index:index + 2] for index in range(len(terms[0]) - 1))
    return list(dict.fromkeys(terms))

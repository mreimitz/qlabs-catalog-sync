#!/usr/bin/env python3
"""Validate and maintain the Research Scaffold OKF knowledge tree.

The runtime is intentionally dependency-free. Frontmatter is restricted to a
small JSON-compatible YAML 1.2 subset: one ``key: value`` pair per line, with
values encoded as JSON strings, arrays, numbers, booleans, or null. This subset
is valid YAML and keeps hook execution deterministic without network installs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


RESERVED_NAMES = {"index.md", "log.md"}
SKIP_DIRS = {".git", "__pycache__"}
FRONTMATTER_ORDER = [
    "type",
    "title",
    "description",
    "resource",
    "tags",
    "timestamp",
    "status",
    "name",
    "argument-hint",
]
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})$")
TAG_DIR_RE = re.compile(r"^(RS|RM)-(\d{2,})-[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}")


@dataclass(frozen=True)
class Issue:
    layer: str
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "layer": self.layer,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


class ProfileError(ValueError):
    """Raised when a document violates the constrained frontmatter grammar."""


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text_if_changed(path: Path, content: str) -> bool:
    normalized = content.rstrip() + "\n"
    if path.exists() and read_text(path) == normalized:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(handle.name)
    try:
        with handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def load_profile(root: Path) -> dict[str, Any]:
    profile_path = root / ".claude" / "okf-profile.json"
    try:
        return json.loads(read_text(profile_path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot load {profile_path}: {exc}") from exc


def parse_frontmatter(text: str) -> tuple[Optional[dict[str, Any]], str]:
    """Parse the profile's JSON-compatible YAML subset."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text

    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise ProfileError("frontmatter has no closing '---' delimiter") from exc

    metadata: dict[str, Any] = {}
    for line_number, raw_line in enumerate(lines[1:end], start=2):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ProfileError(f"frontmatter line {line_number} is not 'key: value'")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise ProfileError(f"frontmatter line {line_number} has invalid key {key!r}")
        if key in metadata:
            raise ProfileError(f"frontmatter key {key!r} is duplicated")
        if not raw_value:
            raise ProfileError(f"frontmatter key {key!r} has no value")
        try:
            metadata[key] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ProfileError(
                f"frontmatter value for {key!r} must use JSON-compatible YAML"
            ) from exc

    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return metadata, body


def render_frontmatter(metadata: dict[str, Any], body: str) -> str:
    keys = [key for key in FRONTMATTER_ORDER if key in metadata]
    keys.extend(sorted(key for key in metadata if key not in keys))
    lines = ["---"]
    for key in keys:
        lines.append(f"{key}: {json.dumps(metadata[key], ensure_ascii=False)}")
    lines.extend(["---", "", body.rstrip(), ""])
    return "\n".join(lines)


def markdown_files(root: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(directory for directory in dirs if directory not in SKIP_DIRS)
        for filename in sorted(files):
            if filename.endswith(".md"):
                yield Path(current) / filename


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def first_nonblank_line(body: str) -> str:
    return next((line.strip() for line in body.splitlines() if line.strip()), "")


def validate_index(root: Path, path: Path, text: str) -> list[Issue]:
    rel = relative(root, path)
    issues: list[Issue] = []
    try:
        metadata, body = parse_frontmatter(text)
    except ProfileError as exc:
        return [Issue("okf", "OKF003", rel, str(exc))]

    if rel == "index.md":
        if metadata is None:
            issues.append(
                Issue("profile", "PROFILE020", rel, "root index must declare okf_version")
            )
        elif metadata != {"okf_version": "0.1"}:
            issues.append(
                Issue(
                    "profile",
                    "PROFILE021",
                    rel,
                    'root index frontmatter must contain only okf_version: "0.1"',
                )
            )
    elif metadata is not None:
        issues.append(
            Issue("okf", "OKF004", rel, "non-root index.md must not contain frontmatter")
        )

    if not first_nonblank_line(body).startswith("# "):
        issues.append(
            Issue("okf", "OKF005", rel, "index body must begin with a level-one heading")
        )
    return issues


def validate_log(root: Path, path: Path, text: str) -> list[Issue]:
    rel = relative(root, path)
    issues: list[Issue] = []
    try:
        metadata, body = parse_frontmatter(text)
    except ProfileError as exc:
        return [Issue("okf", "OKF006", rel, str(exc))]
    if metadata is not None:
        issues.append(Issue("okf", "OKF007", rel, "log.md must not contain frontmatter"))
    if not first_nonblank_line(body).startswith("# "):
        issues.append(
            Issue("okf", "OKF008", rel, "log body must begin with a level-one heading")
        )
    date_headings = [
        match.group(1)
        for line in body.splitlines()
        if (match := DATE_RE.fullmatch(line.strip()))
    ]
    if not date_headings:
        issues.append(
            Issue("okf", "OKF009", rel, "log must contain an ISO 8601 date heading")
        )
    for value in date_headings:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            issues.append(Issue("okf", "OKF010", rel, f"invalid log date {value!r}"))
    if date_headings != sorted(date_headings, reverse=True):
        issues.append(
            Issue("profile", "PROFILE022", rel, "log date headings must be newest first")
        )
    return issues


def validate_concept(
    root: Path, path: Path, text: str, profile: dict[str, Any]
) -> tuple[list[Issue], Optional[dict[str, Any]], str]:
    rel = relative(root, path)
    issues: list[Issue] = []
    try:
        metadata, body = parse_frontmatter(text)
    except ProfileError as exc:
        return [Issue("okf", "OKF001", rel, str(exc))], None, ""

    if metadata is None:
        return [
            Issue("okf", "OKF001", rel, "concept must begin with YAML frontmatter")
        ], None, body

    concept_type = metadata.get("type")
    if not isinstance(concept_type, str) or not concept_type.strip():
        issues.append(Issue("okf", "OKF002", rel, "type must be a non-empty string"))

    for field in profile["required_fields"]:
        value = metadata.get(field)
        if value is None or value == "" or value == []:
            issues.append(
                Issue("profile", "PROFILE001", rel, f"required field {field!r} is missing")
            )

    title = metadata.get("title")
    description = metadata.get("description")
    tags = metadata.get("tags")
    timestamp = metadata.get("timestamp")
    status = metadata.get("status")

    if title is not None and not isinstance(title, str):
        issues.append(Issue("profile", "PROFILE002", rel, "title must be a string"))
    if description is not None and (
        not isinstance(description, str) or "\n" in description
    ):
        issues.append(
            Issue("profile", "PROFILE003", rel, "description must be a one-line string")
        )
    if tags is not None and (
        not isinstance(tags, list)
        or not tags
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
    ):
        issues.append(
            Issue("profile", "PROFILE004", rel, "tags must be a non-empty string list")
        )
    if timestamp is not None:
        if not isinstance(timestamp, str) or not TIMESTAMP_RE.fullmatch(timestamp):
            issues.append(
                Issue(
                    "profile",
                    "PROFILE005",
                    rel,
                    "timestamp must use UTC YYYY-MM-DDTHH:MM:SSZ form",
                )
            )
        else:
            try:
                datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                issues.append(
                    Issue("profile", "PROFILE005", rel, "timestamp is not a valid datetime")
                )

    allowed_statuses = profile["types"].get(concept_type)
    if concept_type and allowed_statuses is None:
        issues.append(
            Issue(
                "profile",
                "PROFILE006",
                rel,
                f"type {concept_type!r} is not registered",
            )
        )
    elif allowed_statuses is not None and status not in allowed_statuses:
        issues.append(
            Issue(
                "profile",
                "PROFILE007",
                rel,
                f"status {status!r} is invalid for type {concept_type!r}",
            )
        )

    if not body.strip():
        issues.append(Issue("profile", "PROFILE008", rel, "concept body must not be empty"))
    if PLACEHOLDER_RE.search(text):
        issues.append(
            Issue("profile", "PROFILE009", rel, "live concept contains a template placeholder")
        )
    if (
        concept_type in profile["citation_required_types"]
        and not re.search(r"(?m)^# Citations\s*$", body)
    ):
        issues.append(
            Issue(
                "profile",
                "PROFILE010",
                rel,
                f"{concept_type} must contain a '# Citations' section",
            )
        )

    return issues, metadata, body


def resolve_link(root: Path, source: Path, target: str) -> Optional[Path]:
    target = target.strip().split(maxsplit=1)[0]
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target or target.startswith("#"):
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("//"):
        return None
    if target.startswith("/"):
        return root / target.lstrip("/")
    return source.parent / target


def validate_links(root: Path, path: Path, text: str) -> list[Issue]:
    rel = relative(root, path)
    issues: list[Issue] = []
    for raw_target in LINK_RE.findall(text):
        resolved = resolve_link(root, path, raw_target)
        if resolved is None:
            continue
        resolved = Path(os.path.normpath(resolved))
        try:
            resolved.relative_to(root)
        except ValueError:
            issues.append(
                Issue(
                    "profile",
                    "PROFILE011",
                    rel,
                    f"internal link escapes the bundle: {raw_target}",
                )
            )
            continue
        if not resolved.exists():
            issues.append(
                Issue(
                    "profile",
                    "PROFILE012",
                    rel,
                    f"internal link target does not exist: {raw_target}",
                )
            )
    return issues


def managed_directories(root: Path) -> list[Path]:
    directories = [root, root / "Research", root / "Roadmap"]
    research = root / "Research"
    roadmap = root / "Roadmap"
    if research.exists():
        for directory in research.iterdir():
            if directory.is_dir() and directory.name.startswith("RS-"):
                directories.append(directory)
                directories.extend(directory / name for name in ("sources", "notes", "outputs"))
    if roadmap.exists():
        for directory in roadmap.iterdir():
            if directory.is_dir() and directory.name.startswith("RM-"):
                directories.append(directory)
    return directories


def validate_managed_structure(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for directory in managed_directories(root):
        rel_dir = "." if directory == root else relative(root, directory)
        if not directory.is_dir():
            issues.append(
                Issue("profile", "PROFILE013", rel_dir, "required directory is missing")
            )
            continue
        index_path = directory / "index.md"
        if not index_path.is_file():
            issues.append(
                Issue(
                    "profile",
                    "PROFILE014",
                    relative(root, index_path),
                    "managed directory must contain index.md",
                )
            )
        if directory.name.startswith("RS-"):
            if not TAG_DIR_RE.fullmatch(directory.name):
                issues.append(
                    Issue(
                        "profile",
                        "PROFILE015",
                        relative(root, directory),
                        "research directory must be RS-NN-lowercase-slug",
                    )
                )
            for required in ("topic.md", "log.md", "sources", "notes", "outputs"):
                if not (directory / required).exists():
                    issues.append(
                        Issue(
                            "profile",
                            "PROFILE016",
                            relative(root, directory / required),
                            "research topic structure is incomplete",
                        )
                    )
        if directory.name.startswith("RM-"):
            if not TAG_DIR_RE.fullmatch(directory.name):
                issues.append(
                    Issue(
                        "profile",
                        "PROFILE015",
                        relative(root, directory),
                        "roadmap directory must be RM-NN-lowercase-slug",
                    )
                )
            for required in ("item.md", "log.md"):
                if not (directory / required).exists():
                    issues.append(
                        Issue(
                            "profile",
                            "PROFILE017",
                            relative(root, directory / required),
                            "roadmap item structure is incomplete",
                        )
                    )
    return issues


def validate_index_completeness(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for directory in managed_directories(root):
        index_path = directory / "index.md"
        if not index_path.is_file():
            continue
        text = read_text(index_path)
        expected: list[str] = []
        for child in sorted(directory.iterdir(), key=lambda value: value.name.lower()):
            if child.name in SKIP_DIRS or child.name.startswith(".okf-staging-"):
                continue
            if child.is_file() and child.suffix == ".md" and child.name not in RESERVED_NAMES:
                expected.append(child.name)
            elif child.is_dir() and (child / "index.md").is_file():
                expected.append(child.name + "/")
        linked = {target.split("#", 1)[0] for target in LINK_RE.findall(text)}
        for target in expected:
            if target not in linked:
                issues.append(
                    Issue(
                        "profile",
                        "PROFILE018",
                        relative(root, index_path),
                        f"index does not list {target}",
                    )
                )
    return issues


def validate_source_attachments(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for sources in root.glob("Research/RS-*/sources"):
        if not sources.is_dir():
            continue
        for attachment in sources.iterdir():
            if (
                not attachment.is_file()
                or attachment.name.startswith(".")
                or attachment.suffix == ".md"
            ):
                continue
            companion = attachment.with_suffix(".md")
            if not companion.is_file():
                issues.append(
                    Issue(
                        "profile",
                        "PROFILE019",
                        relative(root, attachment),
                        f"source attachment requires companion {companion.name}",
                    )
                )
                continue
            try:
                metadata, body = parse_frontmatter(read_text(companion))
            except ProfileError:
                continue
            if not metadata or metadata.get("type") != "Source Reference":
                issues.append(
                    Issue(
                        "profile",
                        "PROFILE029",
                        relative(root, companion),
                        "attachment companion must have type 'Source Reference'",
                    )
                )
            companion_targets = {
                target.split("#", 1)[0] for target in LINK_RE.findall(body)
            }
            if attachment.name not in companion_targets:
                issues.append(
                    Issue(
                        "profile",
                        "PROFILE030",
                        relative(root, companion),
                        f"source companion must link to {attachment.name}",
                    )
                )
    return issues


def validate_tag_uniqueness(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for base, prefix in ((root / "Research", "RS"), (root / "Roadmap", "RM")):
        seen: dict[int, str] = {}
        if not base.is_dir():
            continue
        for directory in base.iterdir():
            match = TAG_DIR_RE.fullmatch(directory.name) if directory.is_dir() else None
            if not match or match.group(1) != prefix:
                continue
            number = int(match.group(2))
            if number in seen:
                issues.append(
                    Issue(
                        "profile",
                        "PROFILE023",
                        relative(root, directory),
                        f"tag {prefix}-{number:02d} is already used by {seen[number]}",
                    )
                )
            seen[number] = directory.name
    return issues


def validate(root: Path) -> list[Issue]:
    root = root.resolve()
    try:
        profile = load_profile(root)
    except ProfileError as exc:
        return [Issue("profile", "PROFILE000", ".claude/okf-profile.json", str(exc))]

    issues: list[Issue] = []
    for required in profile["required_static_paths"]:
        if not (root / required).is_file():
            issues.append(
                Issue("profile", "PROFILE024", required, "required scaffold file is missing")
            )

    for path in markdown_files(root):
        rel = relative(root, path)
        try:
            text = read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            issues.append(Issue("okf", "OKF011", rel, f"file is not readable UTF-8: {exc}"))
            continue

        if path.name == "index.md":
            issues.extend(validate_index(root, path, text))
        elif path.name == "log.md":
            issues.extend(validate_log(root, path, text))
        else:
            concept_issues, _, _ = validate_concept(root, path, text, profile)
            issues.extend(concept_issues)
        issues.extend(validate_links(root, path, text))

        if path.name.lower() == "readme.md":
            issues.append(
                Issue(
                    "profile",
                    "PROFILE025",
                    rel,
                    "README.md is not allowed; use reserved index.md",
                )
            )

    issues.extend(validate_managed_structure(root))
    issues.extend(validate_index_completeness(root))
    issues.extend(validate_source_attachments(root))
    issues.extend(validate_tag_uniqueness(root))
    return sorted(issues, key=lambda issue: (issue.path, issue.layer, issue.code, issue.message))


def concept_metadata(path: Path) -> dict[str, Any]:
    metadata, _ = parse_frontmatter(read_text(path))
    return metadata or {}


def directory_title(directory: Path) -> str:
    if directory.name == ".claude":
        return "Claude Controls"
    if directory.name == "Research":
        return "Research Topics"
    if directory.name == "Roadmap":
        return "Roadmap"
    if directory.name == "sources":
        return "Sources"
    if directory.name == "notes":
        return "Research Notes"
    if directory.name == "outputs":
        return "Research Outputs"
    topic = directory / "topic.md"
    item = directory / "item.md"
    if topic.is_file():
        return str(concept_metadata(topic).get("title", directory.name))
    if item.is_file():
        return str(concept_metadata(item).get("title", directory.name))
    return directory.name.replace("-", " ").title()


def render_index(root: Path, directory: Path) -> str:
    concepts: list[tuple[str, str, str]] = []
    sections: list[tuple[str, str, str]] = []
    for child in sorted(directory.iterdir(), key=lambda value: value.name.lower()):
        if child.name in SKIP_DIRS or child.name.startswith(".okf-staging-"):
            continue
        if child.is_file() and child.suffix == ".md" and child.name not in RESERVED_NAMES:
            metadata = concept_metadata(child)
            concepts.append(
                (
                    str(metadata.get("title", child.stem)),
                    child.name,
                    str(metadata.get("description", "Knowledge concept.")),
                )
            )
        elif child.is_dir() and (child / "index.md").is_file():
            sections.append(
                (
                    directory_title(child),
                    child.name + "/",
                    f"Browse {directory_title(child).lower()}.",
                )
            )

    lines: list[str] = []
    if directory == root:
        lines.extend(["---", 'okf_version: "0.1"', "---", ""])
    lines.append(f"# {directory_title(directory) if directory != root else 'Project Knowledge'}")
    if concepts:
        lines.extend(["", "## Concepts", ""])
        lines.extend(f"* [{title}]({target}) - {description}" for title, target, description in concepts)
    if sections:
        lines.extend(["", "## Sections", ""])
        lines.extend(f"* [{title}]({target}) - {description}" for title, target, description in sections)
    if not concepts and not sections:
        lines.extend(["", "No concepts have been added yet."])
    return "\n".join(lines) + "\n"


def sync_indexes(root: Path) -> int:
    changed = 0
    for directory in managed_directories(root):
        if directory.is_dir():
            changed += int(write_text_if_changed(directory / "index.md", render_index(root, directory)))
    return changed


def sync_master_roadmap(root: Path) -> bool:
    path = root / "Roadmap" / "roadmap.md"
    if not path.is_file():
        return False
    metadata, _ = parse_frontmatter(read_text(path))
    if metadata is None:
        raise ProfileError("Roadmap/roadmap.md has no frontmatter")

    rm_rows: list[str] = []
    for item_path in sorted((root / "Roadmap").glob("RM-*/item.md")):
        item = concept_metadata(item_path)
        rm_rows.append(
            f"* [{item_path.parent.name}]({item_path.parent.name}/item.md) — "
            f"{item.get('title')} · `{item.get('status')}`"
        )
    rs_rows: list[str] = []
    for topic_path in sorted((root / "Research").glob("RS-*/topic.md")):
        topic = concept_metadata(topic_path)
        rs_rows.append(
            f"* [{topic_path.parent.name}](../Research/{topic_path.parent.name}/topic.md) — "
            f"{topic.get('title')} · `{topic.get('status')}`"
        )

    body = "\n".join(
        [
            "# Master Roadmap",
            "",
            "This concept is the live project-level index of roadmap items and research topics.",
            "",
            "## Roadmap Items",
            "",
            *(rm_rows or ["No roadmap items have been created yet."]),
            "",
            "## Research Topics",
            "",
            *(rs_rows or ["No research topics have been created yet."]),
            "",
            "## Now / Next / Later",
            "",
            "**Now:** Define active work in the relevant roadmap item.",
            "",
            "**Next:** Prioritize planned roadmap items.",
            "",
            "**Later:** Keep parked work in explicit roadmap items rather than loose notes.",
        ]
    )
    old_body = parse_frontmatter(read_text(path))[1]
    if old_body.rstrip() == body.rstrip():
        return False
    metadata["timestamp"] = utc_now()
    return write_text_if_changed(path, render_frontmatter(metadata, body))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    words = [word for word in slug.split("-") if word][:4]
    if not words:
        raise ValueError("title must contain at least one letter or number")
    return "-".join(words)


def load_registry(root: Path) -> dict[str, Any]:
    path = root / ".claude" / "tag-registry.json"
    if not path.exists():
        return {"next_rs": 1, "next_rm": 1, "allocated_rs": [], "allocated_rm": []}
    return json.loads(read_text(path))


def save_registry(root: Path, registry: dict[str, Any]) -> None:
    path = root / ".claude" / "tag-registry.json"
    write_text_if_changed(path, json.dumps(registry, indent=2, sort_keys=True))


class AllocationLock:
    def __init__(self, root: Path):
        self.path = root / ".claude" / ".tag-allocation.lock"
        self.fd: Optional[int] = None

    def __enter__(self) -> "AllocationLock":
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError(
                f"tag allocation is already running; remove stale lock only after verifying: {self.path}"
            ) from exc
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def allocate_tag(root: Path, prefix: str) -> tuple[str, dict[str, Any]]:
    registry = load_registry(root)
    key = prefix.lower()
    number = int(registry[f"next_{key}"])
    base = root / ("Research" if prefix == "RS" else "Roadmap")
    used = {
        int(match.group(2))
        for directory in base.glob(f"{prefix}-*")
        if directory.is_dir() and (match := TAG_DIR_RE.fullmatch(directory.name))
    }
    allocated = {int(value.split("-")[1]) for value in registry[f"allocated_{key}"]}
    while number in used or number in allocated:
        number += 1
    tag = f"{prefix}-{number:02d}"
    registry[f"next_{key}"] = number + 1
    registry[f"allocated_{key}"].append(tag)
    return tag, registry


def render_template(root: Path, name: str, values: dict[str, str]) -> str:
    path = root / ".claude" / "templates" / name
    text = read_text(path)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    remaining = PLACEHOLDER_RE.findall(text)
    if remaining:
        raise ProfileError(f"unresolved template fields in {name}: {', '.join(remaining)}")
    return text


def initialize_log(title: str, target: str, action: str) -> str:
    return (
        f"# {title} Update Log\n\n"
        f"## {today()}\n\n"
        f"* **Initialization**: {action} [{target}]({target}).\n"
    )


def new_research(root: Path, args: argparse.Namespace) -> Path:
    existing_issues = validate(root)
    if existing_issues:
        raise RuntimeError(
            "cannot generate research in an invalid bundle:\n" + format_issues(existing_issues)
        )
    with AllocationLock(root):
        original_registry = load_registry(root)
        roadmap_path = root / "Roadmap" / "roadmap.md"
        original_roadmap = read_text(roadmap_path)
        tag, registry = allocate_tag(root, "RS")
        slug = args.slug or slugify(args.title)
        target = root / "Research" / f"{tag}-{slug}"
        if target.exists():
            raise RuntimeError(f"target already exists: {target}")
        staging = Path(tempfile.mkdtemp(prefix=".okf-staging-", dir=root))
        candidate = staging / target.name
        moved = False
        try:
            for name in ("sources", "notes", "outputs"):
                (candidate / name).mkdir(parents=True, exist_ok=True)
            values = {
                "TITLE": args.title,
                "TITLE_JSON": json.dumps(args.title, ensure_ascii=False),
                "DESCRIPTION_JSON": json.dumps(args.objective, ensure_ascii=False),
                "TIMESTAMP_JSON": json.dumps(utc_now()),
                "TAG_JSON": json.dumps(tag),
                "OBJECTIVE": args.objective,
                "WHY_NOW": args.why_now,
                "SCOPE_IN": args.scope_in,
                "SCOPE_OUT": args.scope_out,
                "DELIVERABLE": args.deliverable,
                "SUCCESS_CRITERIA": args.success_criteria,
            }
            write_text_if_changed(
                candidate / "topic.md",
                render_template(root, "research-topic.md.tmpl", values),
            )
            write_text_if_changed(
                candidate / "log.md",
                initialize_log(args.title, "topic.md", "Created research topic"),
            )
            for name in ("sources", "notes", "outputs"):
                write_text_if_changed(
                    candidate / name / "index.md",
                    f"# {name.title()}\n\nNo concepts have been added yet.\n",
                )
            write_text_if_changed(candidate / "index.md", render_index(root, candidate))
            os.replace(candidate, target)
            moved = True
            save_registry(root, registry)
            sync_master_roadmap(root)
            sync_indexes(root)
            issues = validate(root)
            if issues:
                raise RuntimeError(format_issues(issues))
            return target
        except Exception:
            if moved and target.exists():
                shutil.rmtree(target)
                save_registry(root, original_registry)
                write_text_if_changed(roadmap_path, original_roadmap)
                sync_indexes(root)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def find_research_link(root: Path, tag: str) -> str:
    matches = list((root / "Research").glob(f"{tag}-*/topic.md"))
    if len(matches) != 1:
        raise ValueError(f"related research tag {tag!r} does not resolve to one topic")
    return "/" + relative(root, matches[0])


def new_roadmap(root: Path, args: argparse.Namespace) -> Path:
    existing_issues = validate(root)
    if existing_issues:
        raise RuntimeError(
            "cannot generate a roadmap item in an invalid bundle:\n"
            + format_issues(existing_issues)
        )
    with AllocationLock(root):
        original_registry = load_registry(root)
        roadmap_path = root / "Roadmap" / "roadmap.md"
        original_roadmap = read_text(roadmap_path)
        tag, registry = allocate_tag(root, "RM")
        slug = args.slug or slugify(args.title)
        target = root / "Roadmap" / f"{tag}-{slug}"
        if target.exists():
            raise RuntimeError(f"target already exists: {target}")
        staging = Path(tempfile.mkdtemp(prefix=".okf-staging-", dir=root))
        candidate = staging / target.name
        moved = False
        try:
            candidate.mkdir(parents=True)
            milestones = args.milestone or [
                "Define the implementation boundary.",
                "Complete the planned work.",
                "Verify the acceptance criteria.",
            ]
            milestone_text = "\n".join(f"- [ ] {item}" for item in milestones)
            research_links = (
                "\n".join(
                    f"- [{research_tag}]({find_research_link(root, research_tag)})"
                    for research_tag in args.research
                )
                or "No linked research yet."
            )
            values = {
                "TITLE": args.title,
                "TITLE_JSON": json.dumps(args.title, ensure_ascii=False),
                "DESCRIPTION_JSON": json.dumps(args.goal, ensure_ascii=False),
                "TIMESTAMP_JSON": json.dumps(utc_now()),
                "TAG_JSON": json.dumps(tag),
                "GOAL": args.goal,
                "WHY_IT_MATTERS": args.why_it_matters,
                "MILESTONES": milestone_text,
                "LINKED_RESEARCH": research_links,
            }
            write_text_if_changed(
                candidate / "item.md",
                render_template(root, "roadmap-item.md.tmpl", values),
            )
            write_text_if_changed(
                candidate / "log.md",
                initialize_log(args.title, "item.md", "Created roadmap item"),
            )
            write_text_if_changed(candidate / "index.md", render_index(root, candidate))
            os.replace(candidate, target)
            moved = True
            save_registry(root, registry)
            sync_master_roadmap(root)
            sync_indexes(root)
            issues = validate(root)
            if issues:
                raise RuntimeError(format_issues(issues))
            return target
        except Exception:
            if moved and target.exists():
                shutil.rmtree(target)
                save_registry(root, original_registry)
                write_text_if_changed(roadmap_path, original_roadmap)
                sync_indexes(root)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def format_issues(issues: list[Issue]) -> str:
    return "\n".join(
        f"{issue.layer.upper()} {issue.code} {issue.path}: {issue.message}"
        for issue in issues
    )


def print_validation(root: Path, as_json: bool = False) -> int:
    issues = validate(root)
    okf_issues = [issue for issue in issues if issue.layer == "okf"]
    profile_issues = [issue for issue in issues if issue.layer == "profile"]
    if as_json:
        print(
            json.dumps(
                {
                    "okf_version": "0.1",
                    "okf_conformant": not okf_issues,
                    "profile_conformant": not issues,
                    "issues": [issue.as_dict() for issue in issues],
                },
                indent=2,
            )
        )
    else:
        print(f"OKF v0.1 conformance: {'PASS' if not okf_issues else 'FAIL'}")
        print(
            "Research Scaffold OKF Profile: "
            + ("PASS" if not issues else "FAIL")
        )
        if issues:
            print(format_issues(issues))
    return 0 if not issues else 1


def status(root: Path) -> int:
    rows: list[tuple[str, str, str, str]] = []
    for path in sorted((root / "Research").glob("RS-*/topic.md")):
        metadata = concept_metadata(path)
        rows.append((path.parent.name.split("-", 2)[0] + "-" + path.parent.name.split("-", 2)[1], "Research", metadata["title"], metadata["status"]))
    for path in sorted((root / "Roadmap").glob("RM-*/item.md")):
        metadata = concept_metadata(path)
        rows.append((path.parent.name.split("-", 2)[0] + "-" + path.parent.name.split("-", 2)[1], "Roadmap", metadata["title"], metadata["status"]))
    print("| Tag | Domain | Title | Status |")
    print("| --- | --- | --- | --- |")
    if not rows:
        print("| — | — | No research or roadmap items yet | — |")
    else:
        for row in rows:
            print("| " + " | ".join(row) + " |")
    return 0


def validate_projected_document(
    root: Path, path: Path, content: str, profile: dict[str, Any]
) -> list[Issue]:
    if path.suffix != ".md":
        return []
    rel = relative(root, path)
    if path.name.lower() == "readme.md":
        return [Issue("profile", "PROFILE025", rel, "use index.md instead of README.md")]
    if path.name == "index.md":
        return validate_index(root, path, content)
    if path.name == "log.md":
        return validate_log(root, path, content)
    return validate_concept(root, path, content, profile)[0]


def projected_edit(
    path: Path, tool_name: str, tool_input: dict[str, Any]
) -> Optional[str]:
    if tool_name == "Write":
        return tool_input.get("content")
    if not path.exists():
        return None
    content = read_text(path)
    if tool_name == "Edit":
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or old not in content:
            return None
        return content.replace(old, new, 1)
    if tool_name == "MultiEdit":
        for edit in tool_input.get("edits", []):
            old = edit.get("old_string")
            new = edit.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or old not in content:
                return None
            content = content.replace(old, new, 1)
        return content
    return None


def path_policy_issues(root: Path, path: Path) -> list[Issue]:
    try:
        rel = relative(root, path)
    except ValueError:
        return []
    parts = Path(rel).parts
    if not parts:
        return []
    if path.name.lower() == "readme.md":
        return [Issue("profile", "PROFILE025", rel, "use reserved index.md instead")]
    if parts[0] == "Research":
        if len(parts) == 2 and parts[1] == "index.md":
            return []
        if len(parts) >= 3 and TAG_DIR_RE.fullmatch(parts[1]) and parts[1].startswith("RS-"):
            return []
        return [
            Issue(
                "profile",
                "PROFILE026",
                rel,
                "Research content must live in Research/RS-NN-slug/",
            )
        ]
    if parts[0] == "Roadmap":
        if len(parts) == 2 and parts[1] in {"index.md", "roadmap.md"}:
            return []
        if len(parts) >= 3 and TAG_DIR_RE.fullmatch(parts[1]) and parts[1].startswith("RM-"):
            return []
        return [
            Issue(
                "profile",
                "PROFILE027",
                rel,
                "Roadmap detail must live in Roadmap/RM-NN-slug/",
            )
        ]
    return []


def hook_pre(root: Path, payload: dict[str, Any]) -> list[Issue]:
    tool_name = payload.get("tool_name", "")
    if tool_name not in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return []
    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw_path:
        return []
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    path = Path(os.path.normpath(path))
    issues = path_policy_issues(root, path)
    projected = projected_edit(path, tool_name, tool_input)
    if projected is not None:
        issues.extend(validate_projected_document(root, path, projected, load_profile(root)))
        if (
            tool_name in {"Write", "Edit", "MultiEdit"}
            and path.is_file()
            and path.suffix == ".md"
            and path.name not in RESERVED_NAMES
        ):
            try:
                old_metadata, old_body = parse_frontmatter(read_text(path))
                new_metadata, new_body = parse_frontmatter(projected)
            except ProfileError:
                old_metadata = new_metadata = None
                old_body = new_body = ""
            if old_metadata and new_metadata:
                old_without_timestamp = {
                    key: value for key, value in old_metadata.items() if key != "timestamp"
                }
                new_without_timestamp = {
                    key: value for key, value in new_metadata.items() if key != "timestamp"
                }
                meaning_changed = (
                    old_body != new_body or old_without_timestamp != new_without_timestamp
                )
                if meaning_changed and old_metadata.get("timestamp") == new_metadata.get("timestamp"):
                    issues.append(
                        Issue(
                            "profile",
                            "PROFILE028",
                            relative(root, path),
                            "meaningful concept edits must update timestamp in the same operation",
                        )
                    )
    return issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=project_root_from_script())
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--json", action="store_true")
    subparsers.add_parser("sync-indexes")
    subparsers.add_parser("status")
    subparsers.add_parser("hook-pre")

    research = subparsers.add_parser("new-research")
    research.add_argument("--title", required=True)
    research.add_argument("--objective", required=True)
    research.add_argument("--why-now", required=True)
    research.add_argument("--scope-in", required=True)
    research.add_argument("--scope-out", required=True)
    research.add_argument("--deliverable", required=True)
    research.add_argument("--success-criteria", required=True)
    research.add_argument("--slug")

    roadmap = subparsers.add_parser("new-roadmap")
    roadmap.add_argument("--title", required=True)
    roadmap.add_argument("--goal", required=True)
    roadmap.add_argument("--why-it-matters", required=True)
    roadmap.add_argument("--milestone", action="append", default=[])
    roadmap.add_argument("--research", action="append", default=[])
    roadmap.add_argument("--slug")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "validate":
            return print_validation(root, args.json)
        if args.command == "sync-indexes":
            changed = sync_indexes(root)
            changed += int(sync_master_roadmap(root))
            print(f"Synchronized {changed} file(s).")
            return print_validation(root)
        if args.command == "status":
            return status(root)
        if args.command == "hook-pre":
            try:
                payload = json.load(sys.stdin)
            except json.JSONDecodeError as exc:
                print(f"OKF hook rejected an invalid payload: {exc}", file=sys.stderr)
                return 2
            issues = hook_pre(root, payload)
            if issues:
                print(format_issues(issues), file=sys.stderr)
                return 2
            return 0
        if args.command == "new-research":
            target = new_research(root, args)
            print(f"Created {relative(root, target)}")
            return 0
        if args.command == "new-roadmap":
            target = new_roadmap(root, args)
            print(f"Created {relative(root, target)}")
            return 0
    except (OSError, ValueError, RuntimeError, ProfileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

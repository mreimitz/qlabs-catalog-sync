#!/usr/bin/env python3
"""Ready-queue computer for the QLabs Catalog Sync agent task board.

This is a dependency-free (stdlib only) helper for coding agents working the
implementation plan. It reads every ``tasks*.json`` board in this directory and
reports which tasks are *ready* to start.

One board holds the tasks for one roadmap item (its ``roadmap_item`` field).
All boards are loaded together because dependencies cross between them: a
Track B task on a later roadmap item waits on a Track A task from the MVP.
Use ``--roadmap`` to scope the output to a single item.

A task is READY when:

  * its ``status`` is ``"pending"``, and
  * every task id listed in its ``depends_on`` has ``status == "done"``.

Because nothing is done initially, the ready set on a fresh board is exactly
the tasks with no dependencies (the WP0 foundation tasks).

Usage
-----
    python3 ready_queue.py                    # show all ready tasks, grouped by WP
    python3 ready_queue.py --roadmap RM-01    # only tasks for one roadmap item
    python3 ready_queue.py --model opus       # only ready tasks recommended for opus
    python3 ready_queue.py --wp WP1           # only ready tasks in work package WP1
    python3 ready_queue.py --all              # list every task with its status
    python3 ready_queue.py --model sonnet --wp WP3   # filters combine (AND)
    python3 ready_queue.py --help             # full option help

The ``--model``, ``--wp`` and ``--roadmap`` filters combine and also apply to
``--all``.
To mark progress, edit the ``status`` field of a task in ``tasks.json``
(``pending`` -> ``in_progress`` -> ``done``); this script recomputes readiness
from whatever the file currently says.

Exit codes: 0 on success, 2 on bad input (a missing or invalid board, a
duplicate task id across boards, or a dependency referencing an unknown id).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BOARD_DIR = Path(__file__).resolve().parent

# WP identifier -> integer sort key (e.g. "WP10" sorts after "WP9").
def _wp_key(wp: str) -> int:
    digits = "".join(ch for ch in wp if ch.isdigit())
    return int(digits) if digits else 0


# Task id -> sortable tuple (e.g. "T3.10" sorts after "T3.9").
def _task_key(task_id: str) -> tuple[int, int]:
    body = task_id[1:] if task_id.startswith("T") else task_id
    major, _, minor = body.partition(".")
    try:
        return (int(major), int(minor))
    except ValueError:
        return (0, 0)


def discover_boards(directory: Path) -> list[Path]:
    """Every task board in a directory. One board per roadmap item."""
    boards = sorted(directory.glob("tasks*.json"))
    if not boards:
        print(f"error: no tasks*.json board found in {directory}", file=sys.stderr)
        raise SystemExit(2)
    return boards


def load_tasks(paths: list[Path]) -> list[dict]:
    """Load every board, tag each task with its roadmap item, and validate."""
    tasks: list[dict] = []
    seen: dict[str, Path] = {}
    for path in paths:
        try:
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            print(f"error: tasks file not found: {path}", file=sys.stderr)
            raise SystemExit(2)
        except json.JSONDecodeError as exc:
            print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
            raise SystemExit(2)

        board_tasks = data.get("tasks")
        if not isinstance(board_tasks, list) or not board_tasks:
            print(f"error: {path} has no 'tasks' array", file=sys.stderr)
            raise SystemExit(2)
        roadmap_item = data.get("roadmap_item", "")
        for task in board_tasks:
            if task["id"] in seen:
                print(
                    f"error: task {task['id']} appears in both {seen[task['id']]} "
                    f"and {path}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            seen[task["id"]] = path
            # Not part of the board schema; attached for filtering and display.
            task["roadmap_item"] = roadmap_item
            tasks.append(task)

    for task in tasks:
        for dep in task.get("depends_on", []):
            if dep not in seen:
                print(
                    f"error: task {task['id']} depends on unknown task {dep}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
    return tasks


def is_ready(task: dict, status_by_id: dict[str, str]) -> bool:
    """A task is ready when it is pending and all deps are done."""
    if task.get("status") != "pending":
        return False
    return all(status_by_id.get(dep) == "done" for dep in task.get("depends_on", []))


def matches_filters(
    task: dict, model: str | None, wp: str | None, roadmap: str | None
) -> bool:
    if model is not None and task.get("model") != model:
        return False
    if wp is not None and task.get("wp") != wp:
        return False
    if roadmap is not None and task.get("roadmap_item") != roadmap:
        return False
    return True


def group_by_wp(tasks: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for task in tasks:
        groups.setdefault(task["wp"], []).append(task)
    ordered = sorted(groups.items(), key=lambda kv: _wp_key(kv[0]))
    return [
        (wp, sorted(items, key=lambda t: _task_key(t["id"])))
        for wp, items in ordered
    ]


def print_task(task: dict, show_status: bool, show_roadmap: bool) -> None:
    status = f" [{task['status']}]" if show_status else ""
    item = f" {task['roadmap_item']}" if show_roadmap and task.get("roadmap_item") else ""
    print(f"  {task['id']}  ({task['model']}){status}{item}  {task['title']}")
    owns = task.get("owns_paths", [])
    if owns:
        print(f"        owns: {', '.join(owns)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ready_queue.py",
        description=(
            "Compute and print the ready-to-start tasks from the agent task "
            "boards (every tasks*.json in this directory, one per roadmap "
            "item). A task is ready when it is pending and all of its "
            "dependencies are done."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ready_queue.py                    list ready tasks grouped by WP\n"
            "  ready_queue.py --roadmap RM-01    only tasks for one roadmap item\n"
            "  ready_queue.py --model opus       only ready opus tasks\n"
            "  ready_queue.py --wp WP1           only ready tasks in WP1\n"
            "  ready_queue.py --all              every task with its status\n"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="NAME",
        choices=["opus", "sonnet", "haiku"],
        help="filter to tasks with this recommended model",
    )
    parser.add_argument(
        "--wp",
        metavar="ID",
        help="filter to tasks in this work package, e.g. WP3",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="list every task with its status instead of only ready tasks",
    )
    parser.add_argument(
        "--roadmap",
        metavar="TAG",
        help="filter to tasks belonging to this roadmap item, e.g. RM-01",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        type=Path,
        action="append",
        help=(
            "path to a task board; repeatable "
            "(default: every tasks*.json alongside this script)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    boards = args.file or discover_boards(BOARD_DIR)
    tasks = load_tasks(boards)
    # Readiness is computed across every board, so a cross-item dependency is
    # honoured even when the output is filtered to one roadmap item.
    status_by_id = {t["id"]: t.get("status", "pending") for t in tasks}
    show_roadmap = len({t.get("roadmap_item") for t in tasks}) > 1

    if args.all:
        selected = [
            t for t in tasks if matches_filters(t, args.model, args.wp, args.roadmap)
        ]
        heading = "All tasks"
        show_status = True
    else:
        selected = [
            t
            for t in tasks
            if is_ready(t, status_by_id)
            and matches_filters(t, args.model, args.wp, args.roadmap)
        ]
        heading = "Ready tasks"
        show_status = False

    filters = []
    if args.roadmap:
        filters.append(f"roadmap={args.roadmap}")
    if args.model:
        filters.append(f"model={args.model}")
    if args.wp:
        filters.append(f"wp={args.wp}")
    suffix = f" ({', '.join(filters)})" if filters else ""

    print(f"{heading}{suffix}: {len(selected)}")
    if not selected:
        print("  (none)")
        return 0

    for wp, items in group_by_wp(selected):
        print(f"\n{wp}")
        for task in items:
            print_task(task, show_status, show_roadmap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

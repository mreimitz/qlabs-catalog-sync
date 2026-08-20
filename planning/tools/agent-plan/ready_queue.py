#!/usr/bin/env python3
"""Ready-queue computer for the QLabs Catalog Sync agent task board.

This is a dependency-free (stdlib only) helper for coding agents working the
RM-01 implementation plan. It reads ``tasks.json`` (the machine-readable task
board) and reports which tasks are *ready* to start.

A task is READY when:

  * its ``status`` is ``"pending"``, and
  * every task id listed in its ``depends_on`` has ``status == "done"``.

Because nothing is done initially, the ready set on a fresh board is exactly
the tasks with no dependencies (the WP0 foundation tasks).

Usage
-----
    python3 ready_queue.py                 # show all ready tasks, grouped by WP
    python3 ready_queue.py --model opus    # only ready tasks recommended for opus
    python3 ready_queue.py --wp WP1        # only ready tasks in work package WP1
    python3 ready_queue.py --all           # list every task with its status
    python3 ready_queue.py --model sonnet --wp WP3   # filters combine (AND)
    python3 ready_queue.py --help          # full option help

The ``--model`` and ``--wp`` filters combine and also apply to ``--all``.
To mark progress, edit the ``status`` field of a task in ``tasks.json``
(``pending`` -> ``in_progress`` -> ``done``); this script recomputes readiness
from whatever the file currently says.

Exit codes: 0 on success, 2 on bad input (missing/invalid tasks.json or a
dependency referencing an unknown task id).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TASKS_FILE = Path(__file__).resolve().parent / "tasks.json"

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


def load_tasks(path: Path) -> list[dict]:
    """Load and lightly validate the task list from tasks.json."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"error: tasks file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    except json.JSONDecodeError as exc:
        print(f"error: tasks file is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2)

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        print("error: tasks.json has no 'tasks' array", file=sys.stderr)
        raise SystemExit(2)

    ids = {t["id"] for t in tasks}
    for task in tasks:
        for dep in task.get("depends_on", []):
            if dep not in ids:
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


def matches_filters(task: dict, model: str | None, wp: str | None) -> bool:
    if model is not None and task.get("model") != model:
        return False
    if wp is not None and task.get("wp") != wp:
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


def print_task(task: dict, show_status: bool) -> None:
    status = f" [{task['status']}]" if show_status else ""
    print(f"  {task['id']}  ({task['model']}){status}  {task['title']}")
    owns = task.get("owns_paths", [])
    if owns:
        print(f"        owns: {', '.join(owns)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ready_queue.py",
        description=(
            "Compute and print the ready-to-start tasks from the RM-01 agent "
            "task board (tasks.json). A task is ready when it is pending and "
            "all of its dependencies are done."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ready_queue.py                 list ready tasks grouped by WP\n"
            "  ready_queue.py --model opus    only ready opus tasks\n"
            "  ready_queue.py --wp WP1        only ready tasks in WP1\n"
            "  ready_queue.py --all           every task with its status\n"
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
        "--file",
        metavar="PATH",
        type=Path,
        default=TASKS_FILE,
        help="path to tasks.json (default: alongside this script)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    tasks = load_tasks(args.file)
    status_by_id = {t["id"]: t.get("status", "pending") for t in tasks}

    if args.all:
        selected = [t for t in tasks if matches_filters(t, args.model, args.wp)]
        heading = "All tasks"
        show_status = True
    else:
        selected = [
            t
            for t in tasks
            if is_ready(t, status_by_id) and matches_filters(t, args.model, args.wp)
        ]
        heading = "Ready tasks"
        show_status = False

    filters = []
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
            print_task(task, show_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""enforce-structure.py — research project structure guard.

Invoked by the PreToolUse hook wrapper. Reads the Claude Code hook payload as JSON
on stdin. argv[1] is the project root.

Exit codes:
  0  allow the tool call
  2  block it (stderr is fed back to the agent)
anything else / any error -> treated as non-blocking by Claude Code (we fail open).
"""
import sys, os, json, re


def main():
    project_dir = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # unparseable -> don't block

    if payload.get("tool_name", "") not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return 0

    tin = payload.get("tool_input", {}) or {}
    path = tin.get("file_path") or tin.get("notebook_path") or ""
    if not path:
        return 0

    abspath = path if os.path.isabs(path) else os.path.join(project_dir, path)
    abspath = os.path.normpath(abspath)
    try:
        rel = os.path.relpath(abspath, project_dir).replace(os.sep, "/")
    except ValueError:
        return 0
    if rel.startswith("../"):
        return 0  # outside the project tree

    parts = rel.split("/")
    top = parts[0]

    def block(msg):
        sys.stderr.write("BLOCKED by enforce-structure hook:\n" + msg + "\n")
        return 2

    # Reserved template scaffolds must stay free of real content.
    if re.search(r"/(RS|RM)-00-template/", "/" + rel):
        return block(
            f"'{rel}' is inside a reserved *-00-template scaffold.\n"
            "Copy the template to the next free tag folder (RS-NN / RM-NN) and write there instead."
        )

    if top == "Research":
        if len(parts) == 2 and parts[1] == "README.md":
            return 0
        if len(parts) >= 3 and re.match(r"^RS-\d{2}-", parts[1]):
            return 0
        return block(
            f"'{rel}' would put a file loose in Research/.\n"
            "Every research document must live inside a topic folder named 'RS-NN-<slug>'.\n"
            "Copy Research/RS-00-template/ to Research/RS-NN-<slug>/ and write inside it."
        )

    if top == "Roadmap":
        if len(parts) == 2 and parts[1] in ("ROADMAP.md", "README.md"):
            return 0
        if len(parts) >= 3 and re.match(r"^RM-\d{2}-", parts[1]):
            return 0
        return block(
            f"'{rel}' would put a file loose in Roadmap/.\n"
            "Only ROADMAP.md and README.md may sit directly in Roadmap/.\n"
            "Detailed plans go in a folder named 'RM-NN-<slug>'."
        )

    return 0  # .claude/, root README, etc. are fine


if __name__ == "__main__":
    sys.exit(main())

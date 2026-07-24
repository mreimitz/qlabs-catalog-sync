"""Tests for OKF validation, projected-write guards, and generators."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / ".claude" / "scripts" / "okf.py"
SPEC = importlib.util.spec_from_file_location("research_scaffold_okf", MODULE_PATH)
assert SPEC and SPEC.loader
okf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = okf
SPEC.loader.exec_module(okf)


class OkfValidationTests(unittest.TestCase):
    def test_current_scaffold_passes_both_layers(self) -> None:
        self.assertEqual([], okf.validate(ROOT))

    def test_missing_frontmatter_is_official_conformance_error(self) -> None:
        profile = okf.load_profile(ROOT)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.md"
            path.write_text("# Missing frontmatter\n", encoding="utf-8")
            issues, _, _ = okf.validate_concept(Path(temp), path, path.read_text(), profile)
        self.assertIn("OKF001", {issue.code for issue in issues})

    def test_invalid_type_status_pair_is_profile_error(self) -> None:
        profile = okf.load_profile(ROOT)
        content = okf.render_frontmatter(
            {
                "type": "Research Note",
                "title": "Test",
                "description": "A test note.",
                "tags": ["test"],
                "timestamp": "2026-07-24T00:00:00Z",
                "status": "active",
            },
            "# Test\n\nBody.\n\n# Citations\n\nNone.",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "note.md"
            path.write_text(content, encoding="utf-8")
            issues, _, _ = okf.validate_concept(Path(temp), path, content, profile)
        self.assertIn("PROFILE007", {issue.code for issue in issues})

    def test_pre_hook_rejects_nonconformant_write(self) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(ROOT / "Research" / "RS-01-test" / "topic.md"),
                "content": "# Invalid",
            },
        }
        self.assertIn("OKF001", {issue.code for issue in okf.hook_pre(ROOT, payload)})

    def test_pre_hook_accepts_conformant_control_write(self) -> None:
        content = okf.render_frontmatter(
            {
                "type": "Agent Instruction",
                "title": "Temporary instruction",
                "description": "A projected valid control document.",
                "tags": ["agent"],
                "timestamp": "2026-07-24T00:00:00Z",
                "status": "active",
            },
            "# Temporary instruction\n\nTest.",
        )
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(ROOT / ".claude" / "temporary.md"),
                "content": content,
            },
        }
        self.assertEqual([], okf.hook_pre(ROOT, payload))

    def test_pre_hook_requires_timestamp_change_with_meaningful_edit(self) -> None:
        original = (ROOT / "project.md").read_text(encoding="utf-8")
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(ROOT / "project.md"),
                "old_string": "This repository is a reusable knowledge-project scaffold",
                "new_string": "This repository is a reusable knowledge workspace",
            },
        }
        self.assertIn("PROFILE028", {issue.code for issue in okf.hook_pre(ROOT, payload)})
        self.assertIn("knowledge-project scaffold", original)

    def test_source_attachment_requires_typed_linking_companion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = root / "Research" / "RS-01-test" / "sources"
            sources.mkdir(parents=True)
            (sources / "evidence.pdf").write_bytes(b"%PDF-test")
            issues = okf.validate_source_attachments(root)
            self.assertIn("PROFILE019", {issue.code for issue in issues})


class GeneratorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "scaffold"
        shutil.copytree(
            ROOT,
            self.root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_research_and_roadmap_generation_remain_conformant(self) -> None:
        research_args = argparse.Namespace(
            title="OKF Compliance",
            objective="Verify the generated research structure.",
            why_now="The template must remain conformant.",
            scope_in="Generated RS structure and metadata.",
            scope_out="External publication.",
            deliverable="A validated research output.",
            success_criteria="Both validation layers pass.",
            slug=None,
        )
        research_path = okf.new_research(self.root, research_args)
        self.assertEqual("RS-01-okf-compliance", research_path.name)

        roadmap_args = argparse.Namespace(
            title="Ship OKF Template",
            goal="Deliver the conformant scaffold.",
            why_it_matters="Every generated project needs valid knowledge structure.",
            milestone=["Validate the implementation."],
            research=["RS-01"],
            slug=None,
        )
        roadmap_path = okf.new_roadmap(self.root, roadmap_args)
        self.assertEqual("RM-01-ship-okf-template", roadmap_path.name)

        self.assertEqual([], okf.validate(self.root))
        registry = json.loads(
            (self.root / ".claude" / "tag-registry.json").read_text(encoding="utf-8")
        )
        self.assertEqual(["RS-01"], registry["allocated_rs"])
        self.assertEqual(["RM-01"], registry["allocated_rm"])


if __name__ == "__main__":
    unittest.main()

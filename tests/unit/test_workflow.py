"""Static checks on the CI workflow and scripts.

A CI file that references a command which no longer exists is worse than no CI:
it looks like coverage. These tests keep the workflow honest without needing a CI
runner.
"""
from __future__ import annotations
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_every_referenced_script_exists(self):
        referenced = set(re.findall(r"python (tests/[\w/\.]+\.py|tools/[\w/\.]+\.py)", self.workflow))
        self.assertTrue(referenced, "workflow references no project scripts")
        for relative in sorted(referenced):
            self.assertTrue((ROOT / relative).exists(), f"missing {relative}")

    def test_no_live_credentials_in_workflow(self):
        self.assertNotRegex(self.workflow, r"sk-[A-Za-z0-9]{16,}")
        self.assertNotIn("POPO_UPSTREAM_KEY", self.workflow)

    def test_covers_more_than_one_platform(self):
        self.assertIn("windows-latest", self.workflow)
        self.assertIn("ubuntu-latest", self.workflow)

    def test_tests_the_archive_not_only_the_tree(self):
        self.assertIn("tar -xzf", self.workflow)

    def test_reproducibility_is_asserted(self):
        self.assertIn("test \"$a\" = \"$b\"", self.workflow)


class ShellScriptTests(unittest.TestCase):
    def test_test_runner_is_executable_sh(self):
        script = ROOT / "run-tests.sh"
        text = script.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("set -e", text)

    def test_test_runner_stage_commands_exist(self):
        text = (ROOT / "run-tests.sh").read_text(encoding="utf-8")
        for relative in re.findall(r"python3 (tools/[\w/\.]+\.py|tests/[\w/\.]+\.py)", text):
            self.assertTrue((ROOT / relative).exists(), f"missing {relative}")


if __name__ == "__main__":
    unittest.main()

"""Workflow dispatch inputs must not appear directly in run: blocks.

If a workflow has user-controllable inputs (like github.event.inputs.version,
github.head_ref, or github.event.pull_request.title), using them directly in
a run: shell script allows shell injection. An attacker could supply a crafted
input like `"; rm -rf /; echo "` to break out and execute arbitrary code.

The safe pattern is to pass user inputs through environment variables:
  env:
    INPUT_VAR: ${{ github.event.inputs.version }}
  run: echo "$INPUT_VAR"

GitHub Actions will set INPUT_VAR securely and the shell will expand it safely.

This test parses workflow files as text and refuses if a run: block contains
any of:
- ${{ github.event.inputs.*
- ${{ github.head_ref
- ${{ github.event.pull_request.title
- ${{ github.event.pull_request.body
- ${{ github.event.create.ref
- ${{ github.event.pull_request.head.ref

These are user-editable context values that appear in PR bodies, dispatch inputs,
branch names, and commit messages. What breaks if this test stops passing: a
release workflow could run attacker-chosen shell commands with the release token.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Patterns that are safe and should NOT trigger the check:
# - ${{ secrets.* }} (managed by repo, not user-editable)
# - ${{ github.sha }} (commit hash, immutable)
# - ${{ github.repository }} (repo name, immutable)
# - ${{ steps.*.outputs.* }} (defined in workflow, not user input)
# - Matrix values (defined by workflow)
SAFE_PATTERNS = {
    r'\$\{\{\s*secrets\.',
    r'\$\{\{\s*github\.sha',
    r'\$\{\{\s*github\.repository',
    r'\$\{\{\s*steps\.',
    r'\$\{\{\s*matrix\.',
}

# Patterns that are UNSAFE when inside a run: block (user-controllable):
UNSAFE_PATTERNS = {
    r'\$\{\{\s*github\.event\.inputs\.',
    r'\$\{\{\s*github\.head_ref',
    r'\$\{\{\s*github\.event\.pull_request\.title',
    r'\$\{\{\s*github\.event\.pull_request\.body',
    r'\$\{\{\s*github\.event\.create\.ref',
    r'\$\{\{\s*github\.event\.pull_request\.head\.ref',
}


class WorkflowInputsNotInShell(unittest.TestCase):

    def _find_unsafe_expressions(self, workflow_path):
        """Parse a workflow file and find unsafe expressions in run: blocks.

        Returns a list of (line_number, line_text) tuples.
        """
        text = workflow_path.read_text()
        lines = text.split('\n')

        unsafe_expressions = []
        in_run_block = False
        run_block_start = None
        run_block_lines = []

        for i, line in enumerate(lines, start=1):
            # Detect start of run: block
            if re.search(r'^\s*run:\s*\|', line):
                in_run_block = True
                run_block_start = i
                run_block_lines = []
                continue
            elif re.search(r'^\s*run:\s*', line) and '|' not in line:
                # Single-line run: command
                in_run_block = True
                run_block_start = i
                run_block_lines = [line]
                # Check this line for unsafe patterns
                for unsafe_pattern in UNSAFE_PATTERNS:
                    if re.search(unsafe_pattern, line):
                        # Make sure it's not a safe pattern
                        is_safe = False
                        for safe_pattern in SAFE_PATTERNS:
                            if re.search(safe_pattern, line):
                                is_safe = True
                                break
                        if not is_safe:
                            unsafe_expressions.append((i, line.strip()))
                in_run_block = False
                continue

            # Detect end of run block (next line with no indentation or next key)
            if in_run_block and line and not line[0].isspace():
                in_run_block = False
                run_block_lines = []
            elif in_run_block and re.match(r'^\s+\w+:', line):
                in_run_block = False
                run_block_lines = []

            if in_run_block:
                run_block_lines.append(line)
                # Check each line in the block for unsafe patterns
                for unsafe_pattern in UNSAFE_PATTERNS:
                    if re.search(unsafe_pattern, line):
                        # Make sure it's not a safe pattern
                        is_safe = False
                        for safe_pattern in SAFE_PATTERNS:
                            if re.search(safe_pattern, line):
                                is_safe = True
                                break
                        if not is_safe:
                            unsafe_expressions.append((i, line.strip()))

        return unsafe_expressions

    def test_release_workflow_has_no_unsafe_inputs(self):
        """release.yml must not have user inputs directly in run: blocks."""
        workflow_path = WORKFLOWS_DIR / "release.yml"
        self.assertTrue(workflow_path.exists(), "release.yml not found")

        unsafe = self._find_unsafe_expressions(workflow_path)
        self.assertEqual(
            unsafe, [],
            f"Found {len(unsafe)} unsafe expressions in release.yml:\n" +
            "\n".join(f"  Line {line_num}: {text}" for line_num, text in unsafe)
        )

    def test_all_workflows_have_no_unsafe_inputs(self):
        """All workflow files must not have user inputs directly in run: blocks."""
        workflow_files = sorted(list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml")))
        self.assertTrue(workflow_files, "No workflow files found")

        all_unsafe = {}
        for workflow_path in workflow_files:
            unsafe = self._find_unsafe_expressions(workflow_path)
            if unsafe:
                all_unsafe[workflow_path.name] = unsafe

        self.assertEqual(
            all_unsafe, {},
            f"Found unsafe expressions in {len(all_unsafe)} workflow(s):\n" +
            "\n".join(
                f"  {name}: {len(exprs)} occurrence(s)\n" +
                "\n".join(f"    Line {line_num}: {text}" for line_num, text in exprs)
                for name, exprs in sorted(all_unsafe.items())
            )
        )


if __name__ == "__main__":
    unittest.main()

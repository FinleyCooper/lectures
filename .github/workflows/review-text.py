"""Review changed .tex files for mathematical errors and LaTeX formatting errors
using the Claude API.

Runs inside GitHub Actions on push. For every .tex file touched by the push it
asks Claude to find genuine mathematical errors and LaTeX formatting errors
(things that break compilation or render wrongly), applies high-confidence fixes
to the working tree, and writes a PR description. The workflow then commits the
changes to a branch and opens a PR.

Env vars:
  ANTHROPIC_API_KEY  (required)
  AFTER_SHA          (required) head commit of the push
  BEFORE_SHA         previous head (all zeros / empty for new branches)
  CLAUDE_MODEL       optional, default below
  RUNNER_TEMP        where pr_body.md is written
  GITHUB_OUTPUT      set by Actions; has_issues=true|false is written to it
"""

import os
import subprocess
from pathlib import Path

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
BEFORE = os.environ.get("BEFORE_SHA", "")
AFTER = os.environ["AFTER_SHA"]
ZERO = "0" * 40
MAX_FILE_CHARS = 150_000  # skip absurdly large files rather than blow the budget
CATEGORY_ORDER = {"math": 0, "latex": 1}
CATEGORY_LABEL = {"math": "Math", "latex": "LaTeX"}

SYSTEM = """Review a LaTeX lecture-notes diff (full file given for context). Report errors in the added/changed lines only.

category "math": real mathematical errors (algebra, signs, wrong formulas or claims, invalid proof steps, wrong constants/indices, contradictions with earlier text).
category "latex": errors that break compilation or render wrongly (unbalanced braces, mismatched \\begin/\\end or \\left/\\right, math/text-mode mistakes, unescaped %&#_, bad &/\\\\ in align/tabular, duplicate \\label, \\ref/\\cite to missing targets, undefined macros).

Rules:
- No style/notation preferences or prose typos. Report only what you are confident is wrong.
- confidence "high" only if certain and the fix is unambiguous.
- `original`: short, unique, verbatim substring of the file; `replacement`: its fix. Leave both empty if no clean local fix.
- Be terse: `location` at most 8 words, `explanation` one sentence. No issues: empty list.
Answer only by calling report_issues."""

TOOL = {
    "name": "report_issues",
    "description": "Report math and LaTeX errors in the changed lines.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": ["math", "latex"]},
                        "location": {
                            "type": "string",
                            "description": "Max 8 words, e.g. 'Prop 3.2, proof line 2'",
                        },
                        "explanation": {
                            "type": "string",
                            "description": "One sentence: what is wrong and the correct version.",
                        },
                        "confidence": {"type": "string", "enum": ["high", "medium"]},
                        "original": {"type": "string"},
                        "replacement": {"type": "string"},
                    },
                    "required": ["category", "location", "explanation", "confidence"],
                },
            }
        },
        "required": ["issues"],
    },
}


def sh(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def is_new_branch() -> bool:
    return not BEFORE or BEFORE == ZERO


def changed_tex_files() -> list[str]:
    try:
        if is_new_branch():
            out = sh("git", "diff-tree", "--no-commit-id", "--name-only", "-r",
                     "--diff-filter=AM", AFTER)
        else:
            out = sh("git", "diff", "--name-only", "--diff-filter=AM", BEFORE, AFTER)
    except subprocess.CalledProcessError:
        # e.g. force-push where BEFORE is no longer in history: fall back to last commit
        out = sh("git", "diff-tree", "--no-commit-id", "--name-only", "-r",
                 "--diff-filter=AM", AFTER)
    return [f for f in out.splitlines() if f.endswith(".tex")]


def file_diff(path: str) -> str:
    try:
        if is_new_branch():
            return sh("git", "show", "--format=", AFTER, "--", path)
        return sh("git", "diff", BEFORE, AFTER, "--", path)
    except subprocess.CalledProcessError:
        return sh("git", "show", "--format=", AFTER, "--", path)


def review(client: anthropic.Anthropic, path: str, text: str, diff: str) -> list[dict]:
    user = (
        f"File: {path}\n\n<diff>\n{diff}\n</diff>\n\n"
        f"<current_file>\n{text}\n</current_file>"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=3000,  # short report only; raise if you often get truncation warnings
        system=SYSTEM,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "report_issues"},
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "max_tokens":
        print(f"  WARNING: response for {path} was truncated; some issues may be missing")
    for block in resp.content:
        if block.type == "tool_use" and block.name == "report_issues":
            return block.input.get("issues", [])
    return []


def main() -> None:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    sections: list[str] = []
    applied_total = 0
    counts = {"math": 0, "latex": 0}

    for path in changed_tex_files():
        p = Path(path)
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        if len(text) > MAX_FILE_CHARS:
            print(f"Skipping {path}: too large")
            continue
        diff = file_diff(path)
        if not diff.strip():
            continue

        print(f"Reviewing {path} ...")
        issues = review(client, path, text, diff)
        if not issues:
            print("  no errors found")
            continue

        new_text = text
        lines = [f"### `{path}`", ""]
        # maths first, then LaTeX formatting, keeping the model's order within each
        issues = sorted(issues, key=lambda x: CATEGORY_ORDER.get(x.get("category"), 99))
        for i, issue in enumerate(issues, 1):
            category = issue.get("category") if issue.get("category") in CATEGORY_LABEL else "math"
            counts[category] += 1
            orig = issue.get("original") or ""
            repl = issue.get("replacement") or ""
            auto = (
                issue.get("confidence") == "high"
                and orig
                and repl != orig
                and new_text.count(orig) == 1
            )
            if auto:
                new_text = new_text.replace(orig, repl, 1)
                applied_total += 1
            status = "fix applied in this PR" if auto else "**not auto-applied, needs your attention**"
            lines.append(
                f"**{i}. [{CATEGORY_LABEL[category]}] {issue['location']}** "
                f"({issue.get('confidence', '?')} confidence, {status})"
            )
            lines.append("")
            lines.append(issue["explanation"])
            if orig and repl and repl != orig:
                lines += ["", "```diff", *[f"- {l}" for l in orig.splitlines()],
                          *[f"+ {l}" for l in repl.splitlines()], "```"]
            lines.append("")

        if new_text != text:
            p.write_text(new_text, encoding="utf-8")
        sections.append("\n".join(lines))

    out_path = os.environ.get("GITHUB_OUTPUT")
    has_issues = bool(sections)
    if out_path:
        with open(out_path, "a") as f:
            f.write(f"has_issues={'true' if has_issues else 'false'}\n")

    if has_issues:
        body = (
            f"Automated review of commit `{AFTER[:7]}` found {counts['math']} possible "
            f"mathematical error(s) and {counts['latex']} LaTeX formatting error(s).\n\n"
            f"{applied_total} high-confidence fix(es) are applied as edits in this PR; "
            "everything else is listed below for you to judge. "
            "Please check each claim yourself, since the model can be wrong.\n\n"
            + "\n".join(sections)
        )
        Path(os.environ.get("RUNNER_TEMP", ".")).joinpath("pr_body.md").write_text(body, encoding="utf-8")
        print(
            f"Found {counts['math']} math and {counts['latex']} LaTeX issue(s); "
            f"applied {applied_total} fix(es)."
        )
    else:
        print("No issues found.")


if __name__ == "__main__":
    main()
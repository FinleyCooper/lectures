


Review tex · PY
"""Review changed .tex files for mathematical errors using the Claude API.
 
Runs inside GitHub Actions on push. For every .tex file touched by the push it
asks Claude to find genuine mathematical errors, applies high-confidence fixes
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
 
SYSTEM = """You are a meticulous mathematics referee reviewing lecture notes written in LaTeX.
 
You are given a git diff of a .tex file and the full current file for context.
Find genuine MATHEMATICAL errors in the lines that were added or changed: wrong
algebra, sign errors, incorrect formulas, false claims or lemmas, invalid proof
steps, wrong constants or exponents, incorrect definitions, off-by-one indices,
statements that contradict earlier parts of the file.
 
Rules:
- Do NOT report style, typography, notation preferences, typos in prose,
  LaTeX compile problems, or things that are merely terse.
- Only report something if you are confident it is wrong. Work through
  calculations before flagging them. If unsure, omit it, or use confidence "medium".
- Use confidence "high" only when you are certain AND the fix is unambiguous.
- `original` must be an EXACT, verbatim, contiguous substring of the current file
  (copy whitespace and backslashes exactly) that is short but unique in the file.
  `replacement` is what it should be replaced with. If no clean local fix exists,
  leave both empty and just explain.
- If there are no errors, return an empty list.
Always answer by calling the report_math_errors tool."""
 
TOOL = {
    "name": "report_math_errors",
    "description": "Report mathematical errors found in the changed lines.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "Where, e.g. 'Proposition 3.2, second line of proof'",
                        },
                        "explanation": {
                            "type": "string",
                            "description": "What is wrong and why, concisely, with the correct reasoning.",
                        },
                        "confidence": {"type": "string", "enum": ["high", "medium"]},
                        "original": {"type": "string"},
                        "replacement": {"type": "string"},
                    },
                    "required": ["location", "explanation", "confidence"],
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
        max_tokens=8000,
        system=SYSTEM,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "report_math_errors"},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "report_math_errors":
            return block.input.get("issues", [])
    return []
 
 
def main() -> None:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    sections: list[str] = []
    applied_total = 0
 
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
        for i, issue in enumerate(issues, 1):
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
            lines.append(f"**{i}. {issue['location']}** ({issue.get('confidence', '?')} confidence, {status})")
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
            f"Automated review of commit `{AFTER[:7]}` found possible mathematical errors.\n\n"
            f"{applied_total} high-confidence fix(es) are applied as edits in this PR; "
            "everything else is listed below for you to judge. "
            "Please check each claim yourself, since the model can be wrong.\n\n"
            + "\n".join(sections)
        )
        Path(os.environ.get("RUNNER_TEMP", ".")).joinpath("pr_body.md").write_text(body, encoding="utf-8")
        print(f"Found issues; applied {applied_total} fix(es).")
    else:
        print("No issues found.")
 
 
if __name__ == "__main__":
    main()
 
Claude finished the response

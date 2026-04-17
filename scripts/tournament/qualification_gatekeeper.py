#!/usr/bin/env python3
"""PR gatekeeper for PokerBots submissions.

This script validates changed submissions in a PR and runs each one against a baseline bot.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from tournament_utils import (
    discover_changed_files,
    make_repo_relative,
    parse_changed_submissions,
    run_isolated_match,
    validate_submission,
)


def _render_markdown_summary(results: list[dict], invalid_paths: list[str], changed_files: list[str]) -> str:
    lines: list[str] = []
    lines.append("<!-- pokerbots-qualification-report -->")
    lines.append("## PokerBots Qualification Report")
    lines.append("")

    if not changed_files:
        lines.append("No changed files were detected in this PR.")
        return "\n".join(lines)

    lines.append(f"Changed files inspected: **{len(changed_files)}**")
    if invalid_paths:
        lines.append("")
        lines.append("### Invalid Submission Paths")
        for path in invalid_paths:
            lines.append(f"- `{path}`")

    if not results:
        lines.append("")
        lines.append("No submission bots were detected under `submission/<roll_no>/(python_bot|cpp_bot)`.")
        return "\n".join(lines)

    lines.append("")
    lines.append("### Per-Submission Results")
    lines.append("| Submission | Validation | Match | Baseline Bankroll | Submission Bankroll | Verdict |")
    lines.append("|---|---|---|---:|---:|---|")

    for entry in results:
        validation = "PASS" if entry["validation_ok"] else "FAIL"
        match_ok = "PASS" if entry["match_ok"] else "FAIL"
        verdict = "PASS" if entry["qualified"] else "FAIL"
        lines.append(
            "| {submission} | {validation} | {match} | {b0} | {b1} | {verdict} |".format(
                submission=entry["bot_id"],
                validation=validation,
                match=match_ok,
                b0=entry.get("baseline_bankroll", 0),
                b1=entry.get("submission_bankroll", 0),
                verdict=verdict,
            )
        )

    failing_details = [e for e in results if not e["qualified"]]
    if failing_details:
        lines.append("")
        lines.append("### Failures")
        for entry in failing_details:
            lines.append(f"- **{entry['bot_id']}**")
            for issue in entry.get("issues", []):
                lines.append(f"  - {issue}")

    return "\n".join(lines)


def _find_changed_files_under(changed_files: list[str], protected_path: str) -> list[str]:
    protected = protected_path.strip("/")
    if not protected:
        return []
    prefix = f"{protected}/"
    return [path for path in changed_files if path == protected or path.startswith(prefix)]


def _materialize_path_from_ref(repo_root: Path, git_ref: str, source_path: str, destination_root: Path) -> tuple[Path | None, str | None]:
    source = source_path.strip("/")
    if not source:
        return None, "Baseline path cannot be empty"

    listed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", git_ref, "--", source],
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None, (listed.stderr or listed.stdout or "Failed to list baseline files").strip()

    tracked_files = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not tracked_files:
        return None, f"Baseline path '{source}' not found at ref '{git_ref}'"

    for rel_path in tracked_files:
        file_blob = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{git_ref}:{rel_path}"],
            check=False,
            capture_output=True,
        )
        if file_blob.returncode != 0:
            stderr_text = file_blob.stderr.decode("utf-8", errors="replace").strip()
            return None, stderr_text or f"Failed to materialize '{rel_path}' from '{git_ref}'"

        out_path = destination_root / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(file_blob.stdout)

    return (destination_root / source), None


def _write_outputs(
    output_dir: Path,
    *,
    args: argparse.Namespace,
    changed_files: list[str],
    invalid_paths: list[str],
    result_rows: list[dict],
) -> None:
    summary_md = _render_markdown_summary(result_rows, invalid_paths, changed_files)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.md").write_text(summary_md + "\n", encoding="utf-8")
    (output_dir / "results.json").write_text(
        json.dumps(
            {
                "base_ref": args.base_ref,
                "baseline_path": args.baseline_path,
                "num_rounds": args.num_rounds,
                "min_submission_bankroll": args.min_submission_bankroll,
                "changed_files": changed_files,
                "invalid_paths": invalid_paths,
                "results": result_rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate PR submissions and run baseline qualification")
    parser.add_argument("--repo-root", default=".", help="Path to repository root")
    parser.add_argument("--base-ref", default="origin/main", help="Git base ref for PR comparison")
    parser.add_argument("--baseline-path", default="python_skeleton", help="Baseline bot directory")
    parser.add_argument("--num-rounds", type=int, default=300, help="Hands to run for qualification")
    parser.add_argument(
        "--min-submission-bankroll",
        type=int,
        default=1,
        help="Submission must end with bankroll >= this value to pass",
    )
    parser.add_argument(
        "--output-dir",
        default=".qualification",
        help="Directory where JSON/markdown summaries and logs are written",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    changed_files = discover_changed_files(repo_root, args.base_ref)
    result_rows: list[dict] = []

    protected_changes = _find_changed_files_under(changed_files, args.baseline_path)
    if protected_changes:
        result_rows.append(
            {
                "bot_id": "SECURITY_VULNERABILITY",
                "submission_path": args.baseline_path,
                "validation_ok": False,
                "match_ok": False,
                "qualified": False,
                "baseline_bankroll": 0,
                "submission_bankroll": 0,
                "issues": [f"Unauthorized modification of protected baseline path: {p}" for p in protected_changes],
                "log_path": None,
            }
        )
        _write_outputs(
            output_dir,
            args=args,
            changed_files=changed_files,
            invalid_paths=[],
            result_rows=result_rows,
        )
        return 1

    submissions, invalid_paths = parse_changed_submissions(changed_files)

    trusted_baseline_root = output_dir / "trusted_baseline"
    if trusted_baseline_root.exists():
        shutil.rmtree(trusted_baseline_root)
    trusted_baseline_root.mkdir(parents=True, exist_ok=True)

    baseline_abs, baseline_error = _materialize_path_from_ref(
        repo_root,
        args.base_ref,
        args.baseline_path,
        trusted_baseline_root,
    )
    baseline_exists = baseline_abs is not None and baseline_abs.is_dir()

    for submission in submissions:
        row = {
            "bot_id": submission.bot_id,
            "submission_path": submission.path.as_posix(),
            "validation_ok": False,
            "match_ok": False,
            "qualified": False,
            "baseline_bankroll": 0,
            "submission_bankroll": 0,
            "issues": [],
            "log_path": None,
        }

        validation = validate_submission(submission, repo_root)
        row["validation_ok"] = validation.ok
        if not validation.ok:
            row["issues"].extend(validation.errors)
            result_rows.append(row)
            continue

        if not baseline_exists:
            row["issues"].append(
                baseline_error or f"Baseline directory not found at {args.base_ref}: {args.baseline_path}"
            )
            result_rows.append(row)
            continue

        submission_abs = (repo_root / submission.path).resolve()
        match = run_isolated_match(
            repo_root=repo_root,
            player_1_source=baseline_abs,
            player_2_source=submission_abs,
            output_dir=logs_dir,
            player_1_name="BASELINE",
            player_2_name="SUBMISSION",
            num_rounds=args.num_rounds,
            timeout_seconds=1200,
        )

        row["match_ok"] = match.ok
        row["baseline_bankroll"] = match.player_1_bankroll
        row["submission_bankroll"] = match.player_2_bankroll
        if match.log_path is not None:
            row["log_path"] = make_repo_relative(match.log_path, repo_root)

        if not match.ok:
            row["issues"].append(match.failure_reason or "Unknown match failure")
        elif match.player_2_bankroll < args.min_submission_bankroll:
            row["issues"].append(
                "Submission bankroll below threshold: "
                f"{match.player_2_bankroll} < {args.min_submission_bankroll}"
            )
        else:
            row["qualified"] = True

        result_rows.append(row)

    if invalid_paths:
        result_rows.append(
            {
                "bot_id": "PATH_RULES",
                "submission_path": "submission/<roll_no>/(python_bot|cpp_bot)",
                "validation_ok": False,
                "match_ok": False,
                "qualified": False,
                "baseline_bankroll": 0,
                "submission_bankroll": 0,
                "issues": [f"Invalid changed path: {p}" for p in invalid_paths],
                "log_path": None,
            }
        )

    _write_outputs(
        output_dir,
        args=args,
        changed_files=changed_files,
        invalid_paths=invalid_paths,
        result_rows=result_rows,
    )

    qualified_all = all(row.get("qualified", False) for row in result_rows)
    return 0 if qualified_all else 1


if __name__ == "__main__":
    raise SystemExit(main())

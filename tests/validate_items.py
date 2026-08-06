"""
validate_items.py
Runs in the CI 'validate' stage, before any deployment. Checks:
  1. Every environment key referenced in config/environments.yml has a corresponding
     entry in the repo's parameter.yml (catches the silent-skip gotcha at PR time
     instead of at deploy time).
  2. Basic structural sanity of the workspace folder (expected item folders exist).

Exits non-zero on failure so the CI job fails the PR check.
"""

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_parameter_yml_coverage(repo_dir: Path, environments_config: dict) -> list:
    errors = []
    parameter_file = repo_dir / "parameter.yml"

    expected_keys = {
        env["fabric_environment_key"]
        for env in environments_config.get("environments", {}).values()
    }

    if not parameter_file.exists():
        print(f"[validate] No parameter.yml found at {parameter_file} — "
              f"OK only if this workspace genuinely has no environment-specific values.")
        return errors

    parameter_content = load_yaml(parameter_file)
    found_keys = set()
    for entry in parameter_content.get("find_replace", []):
        found_keys.update(entry.get("replace_value", {}).keys())
    for entry in parameter_content.get("spark_pool", []):
        found_keys.update(entry.get("replace_value", {}).keys())

    missing = expected_keys - found_keys
    if missing and found_keys:
        errors.append(
            f"parameter.yml is missing replace_value entries for environment(s): {sorted(missing)}. "
            f"Deploying to these environments would silently skip parameterized values."
        )

    return errors


def validate_workspace_structure(repo_dir: Path) -> list:
    errors = []
    if not repo_dir.exists():
        errors.append(f"Workspace directory not found: {repo_dir}")
        return errors

    item_folders = [p for p in repo_dir.iterdir() if p.is_dir() and "." in p.name]
    if not item_folders:
        errors.append(f"No Fabric item folders found under {repo_dir} "
                       f"(expected folders like 'MyNotebook.Notebook', 'MyReport.Report').")

    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", default="workspace")
    parser.add_argument("--config", default="config/environments.yml")
    args = parser.parse_args()

    repo_dir = REPO_ROOT / args.repo_dir
    config_path = REPO_ROOT / args.config

    environments_config = load_yaml(config_path)

    all_errors = []
    all_errors.extend(validate_workspace_structure(repo_dir))
    all_errors.extend(validate_parameter_yml_coverage(repo_dir, environments_config))

    reports_dir = REPO_ROOT / "tests" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "validation_report.txt"

    if all_errors:
        report_path.write_text("FAILED\n" + "\n".join(all_errors), encoding="utf-8")
        print("[validate] FAILED:")
        for err in all_errors:
            print(f"  - {err}")
        sys.exit(1)

    report_path.write_text("PASSED — no issues found.", encoding="utf-8")
    print("[validate] PASSED — no issues found.")


if __name__ == "__main__":
    main()

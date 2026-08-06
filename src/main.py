"""
main.py
Orchestrator for the full Fabric workspace + item lifecycle.

Usage:
    python src/main.py --env dev  --config config/environments.yml --repo-dir workspace
    python src/main.py --env test --config config/environments.yml --repo-dir workspace
    python src/main.py --env prod --config config/environments.yml --repo-dir workspace

What this covers, end to end, per run:
    1. Authenticate (OIDC in CI, service principal secret, or az login locally)
    2. Ensure the target workspace exists (create if missing, reuse if present)
    3. Assign/re-assign the workspace to its designated capacity
    4. Sync RBAC role assignments (additive - see sync_role_assignments docstring)
    5. Connect to GitHub + initialize Git sync (Dev only, and only if not already connected)
    6. Deploy items via fabric-cicd (publish + remove orphans)

Re-running this script for the same environment is safe - every step is idempotent
or explicitly guarded (see comments inline).
"""

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auth import get_credential, verify_auth, FABRIC_SCOPE  # noqa: E402
from workspace_lifecycle import (  # noqa: E402
    ensure_workspace,
    assign_capacity,
    sync_role_assignments,
    connect_git,
)
from deploy_items import deploy_items  # noqa: E402


def load_environment_config(config_path: str, env_key: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        all_config = yaml.safe_load(f)

    try:
        return all_config["environments"][env_key]
    except KeyError as exc:
        available = list(all_config.get("environments", {}).keys())
        raise SystemExit(
            f"Environment '{env_key}' not found in {config_path}. Available: {available}"
        ) from exc


def validate_parameter_yml_key(repo_dir: str, expected_key: str) -> None:
    """
    Pre-flight guard against the most common fabric-cicd gotcha: if `environment`
    doesn't exactly match a key in parameter.yml, replacements are silently skipped
    rather than raising an error. Fail loudly here instead of discovering it later
    as "why is Test still pointing at the Dev lakehouse".
    """
    parameter_file = Path(repo_dir) / "parameter.yml"
    if not parameter_file.exists():
        print(f"[validate] No parameter.yml found at {parameter_file} - skipping key check "
              f"(fine if this environment genuinely has no parameterized values).")
        return

    with open(parameter_file, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f) or {}

    found_keys = set()
    for entry in content.get("find_replace", []):
        replace_value = entry.get("replace_value", {})
        found_keys.update(replace_value.keys())
    for entry in content.get("spark_pool", []):
        replace_value = entry.get("replace_value", {})
        found_keys.update(replace_value.keys())

    if found_keys and expected_key not in found_keys:
        raise SystemExit(
            f"[validate] Environment key '{expected_key}' not found in parameter.yml "
            f"(found keys: {sorted(found_keys)}). Deploying now would silently skip all "
            f"parameterized replacements. Fix the key mismatch before proceeding."
        )
    print(f"[validate] parameter.yml contains a matching entry for '{expected_key}'.")


def run(env_key: str, config_path: str, repo_dir: str) -> None:
    config = load_environment_config(config_path, env_key)
    fabric_env_key = config["fabric_environment_key"]

    print(f"=== Fabric CI/CD — environment: {env_key} ({fabric_env_key}) ===")

    # 1. Authenticate
    credential = get_credential()
    verify_auth(credential)  # workspace-specific check happens after we know the workspace ID

    token = credential.get_token(FABRIC_SCOPE).token

    # 2. Ensure workspace exists
    workspace = ensure_workspace(
        token=token,
        display_name=config["workspace_name"],
        description=config.get("description", ""),
        capacity_id=config["capacity_id"],
    )
    workspace_id = workspace["id"]

    # Re-verify auth now that we know the specific workspace ID
    verify_auth(credential, workspace_id)

    # 3. Capacity assignment (idempotent — reassigns even if unchanged)
    assign_capacity(token, workspace_id, config["capacity_id"])

    # 4. RBAC sync (additive only — see sync_role_assignments docstring for why
    #    removals are deliberately excluded from routine pipeline runs)
    sync_role_assignments(token, workspace_id, config.get("rbac", []))

    # 5. Git connection — Dev only, and only meaningful on first run
    git_config = config.get("git", {})
    if git_config.get("connect"):
        print(f"[lifecycle] Git connect requested for {env_key} — verify this workspace "
              f"isn't already connected before re-running in production use; connecting an "
              f"already-connected workspace will raise an API error.")
        connect_git(
            token=token,
            workspace_id=workspace_id,
            owner_name=git_config["owner_name"],
            repository_name=git_config["repository_name"],
            branch_name=git_config["branch_name"],
            directory_name=git_config["directory_name"],
            initialization_strategy=git_config.get("initialization_strategy", "PreferRemote"),
        )
    else:
        print(f"[lifecycle] Git connect skipped for {env_key} (pipeline-only environment, by design).")

    # 6. Validate parameter.yml key match, then deploy items
    validate_parameter_yml_key(repo_dir, fabric_env_key)

    deploy_items(
        workspace_id=workspace_id,
        environment=fabric_env_key,
        repository_directory=repo_dir,
        credential=credential,
    )

    print(f"=== Completed: {env_key} ===")


def main():
    parser = argparse.ArgumentParser(description="Fabric CI/CD pipeline orchestrator")
    parser.add_argument("--env", required=True, choices=["dev", "test", "prod"],
                         help="Target environment key from environments.yml")
    parser.add_argument("--config", default="config/environments.yml",
                         help="Path to environments.yml")
    parser.add_argument("--repo-dir", default="workspace",
                         help="Path to the Fabric Git-synced workspace folder "
                              "(must contain parameter.yml if using parameterization)")
    args = parser.parse_args()

    run(args.env, args.config, args.repo_dir)


if __name__ == "__main__":
    main()

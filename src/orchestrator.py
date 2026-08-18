"""
orchestrator.py
Business logic tying the individual services (auth, workspace lifecycle,
deployment pipeline, item deploy) into the two operations main.py exposes.
Kept separate from main.py so the CLI stays a thin argument-parsing shell.
"""

from pathlib import Path

import yaml

from auth import get_credential, verify_auth, FABRIC_SCOPE
from config import PipelineConfig, StageConfig
from workspace_lifecycle import ensure_workspace, assign_capacity, sync_role_assignments, connect_git
from deployment_pipeline_service import ensure_deployment_pipeline, ensure_stage_workspace_assignment
from deploy_items import deploy_items


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
        found_keys.update(entry.get("replace_value", {}).keys())
    for entry in content.get("spark_pool", []):
        found_keys.update(entry.get("replace_value", {}).keys())

    if found_keys and expected_key not in found_keys:
        raise SystemExit(
            f"[validate] Environment key '{expected_key}' not found in parameter.yml "
            f"(found keys: {sorted(found_keys)}). Deploying now would silently skip all "
            f"parameterized replacements. Fix the key mismatch before proceeding."
        )
    print(f"[validate] parameter.yml contains a matching entry for '{expected_key}'.")


def provision_stage_workspace(credential, token: str, stage: StageConfig) -> dict:
    """
    Ensures the stage's workspace exists, is on the right capacity, has the
    right RBAC, and (only if the stage's config requests it) is Git-connected.
    Shared by both `deploy` and `setup-pipeline` so workspace provisioning
    logic lives in exactly one place.
    """
    workspace = ensure_workspace(
        token=token,
        display_name=stage.workspace_name,
        description=stage.description,
        capacity_id=stage.capacity_id,
    )
    workspace_id = workspace["id"]

    # Re-verify auth now that we know the specific workspace ID.
    verify_auth(credential, workspace_id)

    # Capacity assignment (idempotent - reassigns even if unchanged).
    assign_capacity(token, workspace_id, stage.capacity_id)

    # RBAC sync (additive only - see sync_role_assignments docstring for why
    # removals are deliberately excluded from routine pipeline runs).
    sync_role_assignments(token, workspace_id, stage.rbac)

    # Git connection - only for stages whose config explicitly requests it,
    # and only meaningful on first run.
    if stage.git.get("connect"):
        print(f"[orchestrator] Git connect requested for stage '{stage.key}' - verify this "
              f"workspace isn't already connected before re-running; connecting an "
              f"already-connected workspace will raise an API error.")
        connect_git(
            token=token,
            workspace_id=workspace_id,
            owner_name=stage.git["owner_name"],
            repository_name=stage.git["repository_name"],
            branch_name=stage.git["branch_name"],
            directory_name=stage.git["directory_name"],
            initialization_strategy=stage.git.get("initialization_strategy", "PreferRemote"),
        )
    else:
        print(f"[orchestrator] Git connect skipped for stage '{stage.key}' (pipeline-only, by design).")

    return workspace


def deploy_stage(stage: StageConfig, repo_dir: str) -> None:
    """Full per-stage run: authenticate, provision the workspace, deploy items into it."""
    print(f"=== Fabric CI/CD — stage: {stage.key} ({stage.fabric_environment_key}) ===")

    credential = get_credential()
    verify_auth(credential)
    token = credential.get_token(FABRIC_SCOPE).token

    workspace = provision_stage_workspace(credential, token, stage)

    validate_parameter_yml_key(repo_dir, stage.fabric_environment_key)

    deploy_items(
        workspace_id=workspace["id"],
        environment=stage.fabric_environment_key,
        repository_directory=repo_dir,
        credential=credential,
    )

    print(f"=== Completed: {stage.key} ===")


def setup_deployment_pipeline(pipeline_config: PipelineConfig) -> None:
    """
    Ensures every configured stage's workspace exists, ensures the Fabric
    deployment pipeline exists with a matching stage for each one, and maps
    each workspace to its stage. Run this once per topology change (new
    stage added, workspace renamed) - it provisions workspaces but does not
    deploy items into them; that's what `deploy` is for.
    """
    credential = get_credential()
    verify_auth(credential)
    token = credential.get_token(FABRIC_SCOPE).token

    print(f"=== Setting up deployment pipeline '{pipeline_config.name}' "
          f"({len(pipeline_config.stages)} stage(s): {[s.key for s in pipeline_config.stages]}) ===")

    stage_workspace_ids = {}
    for stage in pipeline_config.stages:
        workspace = provision_stage_workspace(credential, token, stage)
        stage_workspace_ids[stage.key] = workspace["id"]

    pipeline = ensure_deployment_pipeline(
        token=token,
        display_name=pipeline_config.name,
        description=pipeline_config.description,
        stage_names=[s.pipeline_stage_name for s in pipeline_config.stages],
    )

    for stage in pipeline_config.stages:
        ensure_stage_workspace_assignment(
            token=token,
            pipeline_id=pipeline["id"],
            stage_display_name=stage.pipeline_stage_name,
            workspace_id=stage_workspace_ids[stage.key],
        )

    print(f"=== Deployment pipeline '{pipeline_config.name}' ready ===")

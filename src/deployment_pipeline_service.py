"""
deployment_pipeline_service.py
Manages Fabric's native Deployment Pipeline feature: the pipeline object
itself, its ordered stages (e.g. Development/Test/Production), and which
workspace is mapped to each stage. This is the topology layer - it does not
move or deploy any item content between stages; item content still flows
through deploy_items.py (fabric-cicd) directly into each stage's workspace.

NOTE ON API ACCURACY: the endpoint shapes below (create pipeline, create
stage, assignWorkspace) reflect the Fabric REST API (Core) - Deployment
Pipelines reference as documented on Microsoft Learn. Verify field names
against https://learn.microsoft.com/en-us/rest/api/fabric/core/deployment-pipelines
before relying on this in production - this is a newer corner of the API and
more likely to have shifted than the workspace endpoints.
"""

import requests

from fabric_client import BASE_URL, headers, raise_for_status, get_paginated


def list_deployment_pipelines(token: str) -> list[dict]:
    return get_paginated(token, f"{BASE_URL}/deploymentPipelines")


def get_deployment_pipeline_by_name(token: str, display_name: str) -> dict | None:
    for pipeline in list_deployment_pipelines(token):
        if pipeline.get("displayName") == display_name:
            return pipeline
    return None


def create_deployment_pipeline(token: str, display_name: str, description: str = "") -> dict:
    payload = {"displayName": display_name, "description": description}
    response = requests.post(f"{BASE_URL}/deploymentPipelines", headers=headers(token), json=payload, timeout=30)
    raise_for_status(response, f"Create deployment pipeline '{display_name}'")
    print(f"[pipeline] Created deployment pipeline '{display_name}'.")
    return response.json()


def list_pipeline_stages(token: str, pipeline_id: str) -> list[dict]:
    return get_paginated(token, f"{BASE_URL}/deploymentPipelines/{pipeline_id}/stages")


def create_pipeline_stage(token: str, pipeline_id: str, display_name: str, description: str = "",
                           is_public: bool = False) -> dict:
    """
    Stages are created in call order and Fabric assigns them sequential order -
    the first stage created is the pipeline's most-upstream (e.g. Dev) stage.

    is_public: whether non-admin viewers with workspace access can see this
    stage's content without being a pipeline admin. False (private) is the
    safer default for Test/Prod.
    """
    payload = {"displayName": display_name, "description": description, "isPublic": is_public}
    response = requests.post(
        f"{BASE_URL}/deploymentPipelines/{pipeline_id}/stages",
        headers=headers(token),
        json=payload,
        timeout=30,
    )
    raise_for_status(response, f"Create stage '{display_name}' on pipeline {pipeline_id}")
    print(f"[pipeline] Created stage '{display_name}' on pipeline {pipeline_id}.")
    return response.json()


def get_stage_by_name(token: str, pipeline_id: str, stage_display_name: str) -> dict | None:
    for stage in list_pipeline_stages(token, pipeline_id):
        if stage.get("displayName") == stage_display_name:
            return stage
    return None


def ensure_deployment_pipeline(token: str, display_name: str, description: str, stage_names: list[str]) -> dict:
    """
    Idempotent create-or-get for the pipeline plus its stages. If the pipeline
    doesn't exist, creates it with every stage in `stage_names`, in order. If
    it already exists, adds any stage in `stage_names` that isn't already
    present - additive only, same philosophy as sync_role_assignments: a stage
    someone added manually (or a rename) is never removed automatically.
    """
    pipeline = get_deployment_pipeline_by_name(token, display_name)
    if pipeline:
        print(f"[pipeline] Deployment pipeline '{display_name}' already exists (id={pipeline['id']}) - reusing.")
    else:
        pipeline = create_deployment_pipeline(token, display_name, description)

    existing_stage_names = {s["displayName"] for s in list_pipeline_stages(token, pipeline["id"])}
    for stage_name in stage_names:
        if stage_name in existing_stage_names:
            print(f"[pipeline] Stage '{stage_name}' already exists - skipping.")
            continue
        create_pipeline_stage(token, pipeline["id"], stage_name)

    return pipeline


def assign_workspace_to_stage(token: str, pipeline_id: str, stage_id: str, workspace_id: str) -> None:
    response = requests.post(
        f"{BASE_URL}/deploymentPipelines/{pipeline_id}/stages/{stage_id}/assignWorkspace",
        headers=headers(token),
        json={"workspaceId": workspace_id},
        timeout=30,
    )
    raise_for_status(response, f"Assign workspace {workspace_id} to stage {stage_id} on pipeline {pipeline_id}")
    print(f"[pipeline] Workspace {workspace_id} assigned to stage {stage_id}.")


def ensure_stage_workspace_assignment(token: str, pipeline_id: str, stage_display_name: str, workspace_id: str) -> None:
    """
    Idempotent workspace-to-stage mapping. Skips the API call if the stage is
    already mapped to this exact workspace; reassigns if it's mapped to a
    different one (config changed) or unmapped.
    """
    stage = get_stage_by_name(token, pipeline_id, stage_display_name)
    if stage is None:
        raise RuntimeError(
            f"Stage '{stage_display_name}' not found on pipeline {pipeline_id} - "
            f"call ensure_deployment_pipeline first."
        )

    if stage.get("workspaceId") == workspace_id:
        print(f"[pipeline] Stage '{stage_display_name}' already mapped to workspace {workspace_id} - skipping.")
        return

    assign_workspace_to_stage(token, pipeline_id, stage["id"], workspace_id)

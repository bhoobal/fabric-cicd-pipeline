"""
workspace_lifecycle.py
Covers the parts of the workspace lifecycle that fabric-cicd itself does NOT do:
fabric-cicd deploys ITEMS into an existing workspace; it does not create workspaces,
assign capacity, manage RBAC, or set up Git connections. This module fills that gap
using the Fabric REST API directly, so a workspace can go from "does not exist yet"
to "fully configured and ready for item deployment" in one automated run.

NOTE ON API ACCURACY: the endpoint shapes below (create workspace, assignToCapacity,
roleAssignments, git/connect + git/initialize) reflect the Fabric REST API (Core)
reference as documented on Microsoft Learn. Git integration APIs in particular have
changed shape before and are the most likely to drift - verify field names against
https://learn.microsoft.com/en-us/rest/api/fabric/core/git before relying on this
in production, especially the git_connect step.
"""

import requests

from fabric_client import BASE_URL, headers, raise_for_status, get_paginated, poll_long_running_operation


def get_workspace_by_name(token: str, display_name: str) -> dict | None:
    """Returns the workspace dict if found by exact displayName match, else None."""
    for ws in get_paginated(token, f"{BASE_URL}/workspaces"):
        if ws.get("displayName") == display_name:
            return ws
    return None


def create_workspace(token: str, display_name: str, description: str = "", capacity_id: str | None = None) -> dict:
    """Creates a new workspace. Optionally assigns capacity at creation time."""
    payload = {"displayName": display_name, "description": description}
    if capacity_id:
        payload["capacityId"] = capacity_id

    response = requests.post(f"{BASE_URL}/workspaces", headers=headers(token), json=payload, timeout=30)
    raise_for_status(response, f"Create workspace '{display_name}'")
    print(f"[lifecycle] Created workspace '{display_name}'.")
    return response.json()


def ensure_workspace(token: str, display_name: str, description: str = "", capacity_id: str | None = None) -> dict:
    """
    Idempotent create-or-get. Returns the existing workspace if a displayName match
    is found, otherwise creates a new one. This is what makes the pipeline safe to
    re-run - it will not create duplicate workspaces.
    """
    existing = get_workspace_by_name(token, display_name)
    if existing:
        print(f"[lifecycle] Workspace '{display_name}' already exists (id={existing['id']}) - reusing.")
        return existing

    return create_workspace(token, display_name, description, capacity_id)


def assign_capacity(token: str, workspace_id: str, capacity_id: str) -> None:
    """
    Assigns (or re-assigns) a workspace to a capacity. Safe to call on every run -
    if the workspace is already on the correct capacity this is a no-op from Fabric's
    perspective, and if it changed (e.g. capacity was resized/replaced) this brings
    it back in line with config.
    """
    response = requests.post(
        f"{BASE_URL}/workspaces/{workspace_id}/assignToCapacity",
        headers=headers(token),
        json={"capacityId": capacity_id},
        timeout=30,
    )
    raise_for_status(response, f"Assign workspace {workspace_id} to capacity {capacity_id}")
    print(f"[lifecycle] Workspace {workspace_id} assigned to capacity {capacity_id}.")


def list_role_assignments(token: str, workspace_id: str) -> list[dict]:
    return get_paginated(token, f"{BASE_URL}/workspaces/{workspace_id}/roleAssignments")


def add_role_assignment(token: str, workspace_id: str, principal_id: str, principal_type: str, role: str) -> None:
    """
    principal_type: "User" | "Group" | "ServicePrincipal" | "ServicePrincipalProfile"
    role: "Admin" | "Member" | "Contributor" | "Viewer"
    """
    payload = {"principal": {"id": principal_id, "type": principal_type}, "role": role}
    response = requests.post(
        f"{BASE_URL}/workspaces/{workspace_id}/roleAssignments",
        headers=headers(token),
        json=payload,
        timeout=30,
    )
    raise_for_status(response, f"Add role assignment ({principal_type} {principal_id} -> {role})")
    print(f"[lifecycle] Granted {role} to {principal_type} {principal_id} on workspace {workspace_id}.")


def sync_role_assignments(token: str, workspace_id: str, desired_roles: list[dict]) -> None:
    """
    Idempotent RBAC sync (additive only, by design). Grants any desired role that
    isn't already present. Deliberately does NOT remove assignments not listed in
    `desired_roles` - automated removal of access is a destructive action that
    should go through a separate, explicitly reviewed step, not a routine deploy run.

    desired_roles: [{"principal_id": "...", "principal_type": "ServicePrincipal", "role": "Contributor"}, ...]
    """
    existing = list_role_assignments(token, workspace_id)
    existing_principal_ids = {a["principal"]["id"] for a in existing}

    for desired in desired_roles:
        if desired["principal_id"] in existing_principal_ids:
            print(f"[lifecycle] {desired['principal_type']} {desired['principal_id']} already has a role - skipping.")
            continue
        add_role_assignment(
            token,
            workspace_id,
            desired["principal_id"],
            desired["principal_type"],
            desired["role"],
        )


def connect_git(
    token: str,
    workspace_id: str,
    owner_name: str,
    repository_name: str,
    branch_name: str,
    directory_name: str,
    initialization_strategy: str = "PreferRemote",
) -> None:
    """
    Connects a workspace to a GitHub repository and initializes the sync.
    Only call this for the Dev environment per the design - Test/Prod stay
    pipeline-only and should never be Git-connected directly.

    initialization_strategy:
      "PreferRemote"    - pulls Git content into the (empty) workspace - typical for
                          first-time setup where the repo is the source of truth
      "PreferWorkspace" - pushes existing workspace content into Git - typical only
                          if the workspace had content before Git integration existed
    """
    connect_payload = {
        "gitProviderDetails": {
            "gitProviderType": "GitHub",
            "ownerName": owner_name,
            "repositoryName": repository_name,
            "branchName": branch_name,
            "directoryName": directory_name,
        }
    }
    response = requests.post(
        f"{BASE_URL}/workspaces/{workspace_id}/git/connect",
        headers=headers(token),
        json=connect_payload,
        timeout=30,
    )
    raise_for_status(response, f"Connect workspace {workspace_id} to GitHub repo {owner_name}/{repository_name}")
    print(f"[lifecycle] Workspace {workspace_id} connected to {owner_name}/{repository_name}@{branch_name}:{directory_name}.")

    # Initialize the connection - this is a long-running operation in Fabric;
    # poll briefly rather than assuming it completes synchronously.
    init_response = requests.post(
        f"{BASE_URL}/workspaces/{workspace_id}/git/initializeConnection",
        headers=headers(token),
        json={"initializationStrategy": initialization_strategy},
        timeout=30,
    )
    raise_for_status(init_response, f"Initialize git connection for workspace {workspace_id}")

    if init_response.status_code == 202:
        poll_long_running_operation(token, init_response.headers.get("Location"))

    print(f"[lifecycle] Git initialization complete for workspace {workspace_id}.")

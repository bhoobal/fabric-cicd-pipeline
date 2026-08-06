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

import time
import requests

BASE_URL = "https://api.fabric.microsoft.com/v1"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _raise_for_status(response: requests.Response, context: str) -> None:
    if response.status_code >= 300:
        raise RuntimeError(f"{context} failed [{response.status_code}]: {response.text}")


def get_workspace_by_name(token: str, display_name: str) -> dict | None:
    """Returns the workspace dict if found by exact displayName match, else None."""
    url = f"{BASE_URL}/workspaces"
    continuation_token = None

    while True:
        params = {"continuationToken": continuation_token} if continuation_token else {}
        response = requests.get(url, headers=_headers(token), params=params, timeout=30)
        _raise_for_status(response, "List workspaces")
        body = response.json()

        for ws in body.get("value", []):
            if ws.get("displayName") == display_name:
                return ws

        continuation_token = body.get("continuationToken")
        if not continuation_token:
            return None


def create_workspace(token: str, display_name: str, description: str = "", capacity_id: str | None = None) -> dict:
    """Creates a new workspace. Optionally assigns capacity at creation time."""
    payload = {"displayName": display_name, "description": description}
    if capacity_id:
        payload["capacityId"] = capacity_id

    response = requests.post(f"{BASE_URL}/workspaces", headers=_headers(token), json=payload, timeout=30)
    _raise_for_status(response, f"Create workspace '{display_name}'")
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
        headers=_headers(token),
        json={"capacityId": capacity_id},
        timeout=30,
    )
    _raise_for_status(response, f"Assign workspace {workspace_id} to capacity {capacity_id}")
    print(f"[lifecycle] Workspace {workspace_id} assigned to capacity {capacity_id}.")


def list_role_assignments(token: str, workspace_id: str) -> list[dict]:
    assignments = []
    continuation_token = None
    url = f"{BASE_URL}/workspaces/{workspace_id}/roleAssignments"

    while True:
        params = {"continuationToken": continuation_token} if continuation_token else {}
        response = requests.get(url, headers=_headers(token), params=params, timeout=30)
        _raise_for_status(response, f"List role assignments for {workspace_id}")
        body = response.json()
        assignments.extend(body.get("value", []))
        continuation_token = body.get("continuationToken")
        if not continuation_token:
            return assignments


def add_role_assignment(token: str, workspace_id: str, principal_id: str, principal_type: str, role: str) -> None:
    """
    principal_type: "User" | "Group" | "ServicePrincipal" | "ServicePrincipalProfile"
    role: "Admin" | "Member" | "Contributor" | "Viewer"
    """
    payload = {"principal": {"id": principal_id, "type": principal_type}, "role": role}
    response = requests.post(
        f"{BASE_URL}/workspaces/{workspace_id}/roleAssignments",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    _raise_for_status(response, f"Add role assignment ({principal_type} {principal_id} -> {role})")
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
        headers=_headers(token),
        json=connect_payload,
        timeout=30,
    )
    _raise_for_status(response, f"Connect workspace {workspace_id} to GitHub repo {owner_name}/{repository_name}")
    print(f"[lifecycle] Workspace {workspace_id} connected to {owner_name}/{repository_name}@{branch_name}:{directory_name}.")

    # Initialize the connection - this is a long-running operation in Fabric;
    # poll briefly rather than assuming it completes synchronously.
    init_response = requests.post(
        f"{BASE_URL}/workspaces/{workspace_id}/git/initializeConnection",
        headers=_headers(token),
        json={"initializationStrategy": initialization_strategy},
        timeout=30,
    )
    _raise_for_status(init_response, f"Initialize git connection for workspace {workspace_id}")

    if init_response.status_code == 202:
        _poll_long_running_operation(token, init_response.headers.get("Location"))

    print(f"[lifecycle] Git initialization complete for workspace {workspace_id}.")


def _poll_long_running_operation(token: str, operation_url: str | None, timeout_seconds: int = 300) -> None:
    """Fabric's long-running operations return a 202 + Location header to poll."""
    if not operation_url:
        return
    start = time.time()
    while time.time() - start < timeout_seconds:
        response = requests.get(operation_url, headers=_headers(token), timeout=30)
        body = response.json()
        status = body.get("status")
        if status == "Succeeded":
            return
        if status == "Failed":
            raise RuntimeError(f"Long-running operation failed: {body}")
        time.sleep(5)
    raise TimeoutError(f"Long-running operation did not complete within {timeout_seconds}s: {operation_url}")

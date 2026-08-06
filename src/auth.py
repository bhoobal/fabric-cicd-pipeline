"""
auth.py
Authentication helper for the Fabric CI/CD pipeline.

Resolves a token credential in this priority order:
  1. GitHub Actions OIDC federation (no stored secret) - used when running in GitHub Actions
     with `permissions: id-token: write` and a federated credential configured on the
     Entra ID app registration.
  2. Service principal with client secret (from env vars) - fallback for platforms without
     OIDC support, or local testing against a non-personal identity.
  3. Azure CLI credential (`az login`) - local development only. Never used in CI.

Environment variables expected:
  FABRIC_TENANT_ID           - Entra ID tenant ID (all paths)
  FABRIC_CLIENT_ID           - Application (client) ID of the service principal (paths 1 & 2)
  FABRIC_CLIENT_SECRET       - Client secret (path 2 only - omit this var to force OIDC path 1
                                when ACTIONS_ID_TOKEN_REQUEST_TOKEN is present)
  GITHUB_ACTIONS             - Set automatically by GitHub Actions runners to "true"
  ACTIONS_ID_TOKEN_REQUEST_TOKEN / ACTIONS_ID_TOKEN_REQUEST_URL
                              - Set automatically by GitHub Actions when id-token: write
                                permission is granted to the job
"""

import os
import requests
from azure.identity import AzureCliCredential, ClientSecretCredential, ClientAssertionCredential

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
OIDC_AUDIENCE = "api://AzureADTokenExchange"


def _get_github_oidc_token() -> str:
    """Fetch a GitHub Actions OIDC token to exchange for an Entra ID token."""
    request_token = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    request_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]

    response = requests.get(
        f"{request_url}&audience={OIDC_AUDIENCE}",
        headers={"Authorization": f"bearer {request_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["value"]


def get_credential():
    """
    Returns an azure-identity credential object appropriate for the environment
    this script is currently running in.
    """
    running_in_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    has_oidc_request_token = "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in os.environ
    tenant_id = os.environ.get("FABRIC_TENANT_ID")
    client_id = os.environ.get("FABRIC_CLIENT_ID")
    client_secret = os.environ.get("FABRIC_CLIENT_SECRET")

    if running_in_github_actions and has_oidc_request_token and not client_secret:
        if not (tenant_id and client_id):
            raise EnvironmentError(
                "FABRIC_TENANT_ID and FABRIC_CLIENT_ID must be set to use GitHub OIDC federation."
            )
        print("[auth] Using GitHub Actions OIDC federated credential (no stored secret).")
        return ClientAssertionCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            func=_get_github_oidc_token,
        )

    if client_secret:
        if not (tenant_id and client_id):
            raise EnvironmentError(
                "FABRIC_TENANT_ID and FABRIC_CLIENT_ID must be set alongside FABRIC_CLIENT_SECRET."
            )
        print("[auth] Using service principal client secret credential.")
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    print("[auth] Falling back to Azure CLI credential (local development only - "
          "run `az login` first). This path should never be used in CI/CD.")
    return AzureCliCredential()


def verify_auth(credential, workspace_id: str | None = None) -> None:
    """
    Pre-flight check: confirms a token can be acquired and, if a workspace_id is
    given, that this identity can actually see that workspace. Raises SystemExit
    with a clear message on failure rather than letting fabric-cicd fail deep
    inside a deployment call.
    """
    try:
        token = credential.get_token(FABRIC_SCOPE)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a pre-flight check
        raise SystemExit(f"[auth] Token acquisition failed before deployment started: {exc}") from exc

    print("[auth] Token acquired successfully.")

    if not workspace_id:
        return

    response = requests.get(
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}",
        headers={"Authorization": f"Bearer {token.token}"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(
            f"[auth] Token acquired, but cannot access workspace {workspace_id}. "
            f"Status: {response.status_code}, Response: {response.text}\n"
            f"Check: (1) tenant setting 'Service principals can use Fabric APIs' is enabled, "
            f"(2) this identity has a workspace role assignment."
        )

    print(f"[auth] Confirmed access to workspace: {response.json().get('displayName')}")

"""
fabric_client.py
Minimal, shared HTTP layer for the Fabric REST API (Core). Centralizes the
base URL, auth headers, error handling, pagination, and long-running-operation
polling so workspace_lifecycle.py and deployment_pipeline_service.py don't
each reimplement them.
"""

import time
import requests

BASE_URL = "https://api.fabric.microsoft.com/v1"


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def raise_for_status(response: requests.Response, context: str) -> None:
    if response.status_code >= 300:
        raise RuntimeError(f"{context} failed [{response.status_code}]: {response.text}")


def get_paginated(token: str, url: str, timeout: int = 30) -> list:
    """GETs every page of a Fabric list endpoint (continuationToken-based) and
    returns the concatenated `value` arrays."""
    items = []
    continuation_token = None

    while True:
        params = {"continuationToken": continuation_token} if continuation_token else {}
        response = requests.get(url, headers=headers(token), params=params, timeout=timeout)
        raise_for_status(response, f"GET {url}")
        body = response.json()
        items.extend(body.get("value", []))
        continuation_token = body.get("continuationToken")
        if not continuation_token:
            return items


def poll_long_running_operation(token: str, operation_url: str | None, timeout_seconds: int = 300) -> None:
    """Fabric's long-running operations return a 202 + Location header to poll."""
    if not operation_url:
        return
    start = time.time()
    while time.time() - start < timeout_seconds:
        response = requests.get(operation_url, headers=headers(token), timeout=30)
        body = response.json()
        status = body.get("status")
        if status == "Succeeded":
            return
        if status == "Failed":
            raise RuntimeError(f"Long-running operation failed: {body}")
        time.sleep(5)
    raise TimeoutError(f"Long-running operation did not complete within {timeout_seconds}s: {operation_url}")

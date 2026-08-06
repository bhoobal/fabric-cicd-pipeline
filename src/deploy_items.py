"""
deploy_items.py
Thin, deliberate wrapper around fabric-cicd's publish/unpublish calls.
Kept separate from workspace_lifecycle.py because these two things have very
different risk profiles: lifecycle operations (create workspace, assign RBAC)
are infrequent and structural, while item deployment runs on every merge/tag
and should stay as simple and predictable as possible.
"""

from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

# Item types covered by this pipeline. Extend as your platform's scope grows.
# Semantic models and reports cover the PBIR/TMDL-based Power BI content
# referenced in the request; Notebook/DataPipeline/Environment/VariableLibrary
# cover the wider Fabric estate.
DEFAULT_ITEM_TYPES = [
    "Notebook",
    "DataPipeline",
    "Environment",
    "VariableLibrary",
    "SemanticModel",
    "Report",
]


def deploy_items(
    workspace_id: str,
    environment: str,
    repository_directory: str,
    credential,
    item_types: list[str] | None = None,
    remove_orphans: bool = True,
) -> None:
    """
    Publishes all in-scope items from the repo into the target workspace, then
    (optionally) removes items that exist in the workspace but no longer exist
    in the repo - this is what keeps the workspace from silently accumulating
    items someone deleted from source control.

    `environment` must exactly match a key in the workspace folder's parameter.yml
    (e.g. "DEV" / "TEST" / "PROD") - a mismatch causes parameterized values (lakehouse
    GUIDs, connection references) to be silently skipped rather than erroring, so this
    is validated by the caller (see main.py) before this function is invoked.
    """
    target_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        environment=environment,
        repository_directory=repository_directory,
        item_type_in_scope=item_types or DEFAULT_ITEM_TYPES,
        credential=credential,
    )

    print(f"[deploy] Publishing items to workspace {workspace_id} (environment={environment})...")
    publish_all_items(target_workspace)

    if remove_orphans:
        print(f"[deploy] Removing orphaned items (deleted from repo, still in workspace)...")
        unpublish_all_orphan_items(target_workspace)

    print(f"[deploy] Deployment to {environment} complete.")

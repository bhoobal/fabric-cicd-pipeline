# Fabric CI/CD Pipeline (fabric-cicd + GitHub Enterprise)

End-to-end pipeline covering the full workspace lifecycle — not just item deployment:

1. **Authenticate** — OIDC (CI), service principal secret, or `az login` (local)
2. **Create or update the workspace** — idempotent, safe to re-run
3. **Assign capacity**
4. **Sync RBAC role assignments** (additive)
5. **Connect to GitHub + initialize Git sync** — Dev only
6. **Deploy items** via fabric-cicd (publish + remove orphans) — Notebooks, Data Pipelines, Environments, Variable Libraries, Semantic Models (TMDL), Reports (PBIR)

## Layout

```
config/environments.yml     # Per-environment workspace name, capacity, RBAC, Git settings
src/auth.py                 # Credential resolution (OIDC / secret / CLI) + pre-flight token check
src/workspace_lifecycle.py  # Create/update workspace, capacity, RBAC, Git connect (raw Fabric REST API)
src/deploy_items.py         # fabric-cicd publish/unpublish wrapper
src/main.py                 # Orchestrator — run this
tests/validate_items.py     # CI pre-flight checks (parameter.yml key coverage, folder structure)
workspace/                  # Fabric Git-synced folder — your actual Notebooks/Reports/Models live here
workspace/parameter.yml     # fabric-cicd environment parameterization (must live here, not in config/)
.github/workflows/fabric-cicd.yml   # Dev (auto) → Test (1 reviewer) → Prod (2 reviewers)
```

## One-time setup (do this before the first run)

1. **Fabric tenant admin** enables *"Service principals can use Fabric APIs"* (Tenant settings → Developer settings), scoped to a security group containing your `dev`/`test`/`prod` service principals.
2. Create one **Entra ID app registration per environment** (`sp-fabric-dev`, `sp-fabric-test`, `sp-fabric-prod`). Note each Application (client) ID.
3. Configure a **federated credential** on each app registration for GitHub Actions OIDC (Entra ID app → Certificates & secrets → Federated credentials → GitHub Actions), scoped to this repo and the matching environment name (`dev`/`test`/`prod`).
4. In GitHub: **Settings → Environments**, create `dev`, `test`, `prod`. Add 1 required reviewer to `test`, 2 to `prod`. Add each `*_SP_CLIENT_ID` as an environment variable.
5. Add repo-level variable `FABRIC_TENANT_ID`.
6. Fill in real GUIDs in `config/environments.yml` (capacity IDs, RBAC principal IDs) and `workspace/parameter.yml` (Lakehouse/connection GUIDs, Spark pool IDs).
7. Replace the placeholder folders under `workspace/` with your actual committed Notebooks, Data Pipelines, Semantic Models (TMDL), and Reports (PBIR).

## Running locally (testing only — never for real Test/Prod promotion)

```bash
az login
pip install -r requirements.txt
python src/main.py --env dev --config config/environments.yml --repo-dir workspace
```

## Running in CI

Push to `main` → auto-deploys Dev. Tag `vX.Y.Z` → deploys Test (after reviewer approval) → Prod (after 2 reviewers approve). See `.github/workflows/fabric-cicd.yml`.

## Design notes worth knowing before you rely on this

- **Git connect is Dev-only, by design.** Test and Prod are never Git-connected — they're updated exclusively by this pipeline, which is what keeps "Git is the source of truth" actually true rather than aspirational.
- **RBAC sync is additive-only.** `sync_role_assignments` grants missing roles but never removes existing ones — access removal is a deliberate, separately-reviewed action, not something that should happen as a side effect of a routine deploy.
- **`environment` key mismatches fail silently in fabric-cicd itself** (unresolved values just don't get replaced) — `validate_items.py` and `main.py`'s `validate_parameter_yml_key` both guard against this by failing loudly instead, but only if you keep `config/environments.yml`'s `fabric_environment_key` values in sync with `workspace/parameter.yml`'s keys.
- **Git integration API field names in `workspace_lifecycle.py`'s `connect_git` should be verified against current Microsoft Learn docs before production use** — this is the area of the Fabric REST API most likely to have shifted since this was written.
- **No rollback.** A bad Prod deploy is fixed by a new corrective commit/tag, not a revert — standard "roll forward" discipline applies.
- **GitHub Enterprise Server (on-prem)** isn't supported by Fabric's native Git integration (Cloud only) — this only affects the Dev `connect_git` step; the GitHub Actions workflow itself works the same regardless of GitHub Enterprise Cloud vs. Server.

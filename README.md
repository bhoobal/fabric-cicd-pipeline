# Fabric CI/CD Pipeline (fabric-cicd + GitHub Enterprise)

End-to-end pipeline covering the full workspace and deployment-pipeline lifecycle — not just item deployment:

1. **Authenticate** — OIDC (CI), service principal secret, or `az login` (local)
2. **Create or update each stage's workspace** — idempotent, safe to re-run
3. **Assign capacity**
4. **Sync RBAC role assignments** (additive)
5. **Connect to GitHub + initialize Git sync** — Dev only, per config
6. **Create/update the Fabric deployment pipeline** and map each stage's workspace to it
7. **Deploy items** via fabric-cicd (publish + remove orphans) — Notebooks, Data Pipelines, Environments, Variable Libraries, Semantic Models (TMDL), Reports (PBIR)

Stages are entirely config-driven — `dev` → `test` → `prod` is this repo's default, not something the code hardcodes. Add, remove, or reorder stages in `config/environments.yml` without touching Python.

## Layout

```
config/environments.yml            # deployment_pipeline settings + ordered list of stages (workspace, capacity, RBAC, Git)
src/config.py                      # Loads environments.yml into PipelineConfig / StageConfig
src/auth.py                        # Credential resolution (OIDC / secret / CLI) + pre-flight token check
src/fabric_client.py               # Shared Fabric REST API HTTP layer (headers, pagination, LRO polling)
src/workspace_lifecycle.py         # Create/update workspace, capacity, RBAC, Git connect
src/deployment_pipeline_service.py # Create/update the Fabric deployment pipeline + stage-workspace mapping
src/deploy_items.py                # fabric-cicd publish/unpublish wrapper
src/orchestrator.py                # Ties the services together: provision a stage, deploy a stage, set up the pipeline
src/main.py                        # CLI entrypoint — run this (see Usage below)
tests/validate_items.py            # CI pre-flight checks (parameter.yml key coverage, folder structure)
workspace/                         # Fabric Git-synced folder — your actual Notebooks/Reports/Models live here
workspace/parameter.yml            # fabric-cicd environment parameterization (must live here, not in config/)
.github/workflows/fabric-cicd.yml  # Dev (auto) → Test (1 reviewer) → Prod (2 reviewers); manual setup-pipeline job
```

## One-time setup (do this before the first run)

1. **Fabric tenant admin** enables *"Service principals can use Fabric APIs"* (Tenant settings → Developer settings), scoped to a security group containing your `dev`/`test`/`prod` service principals.
2. Create one **Entra ID app registration per stage** (`sp-fabric-dev`, `sp-fabric-test`, `sp-fabric-prod`), plus one broader-scoped app registration for `setup-pipeline` if you'll run it in CI (it needs create/RBAC/capacity permissions across every stage, not just one). Note each Application (client) ID.
3. Configure a **federated credential** on each app registration for GitHub Actions OIDC (Entra ID app → Certificates & secrets → Federated credentials → GitHub Actions), scoped to this repo and the matching environment name (`dev`/`test`/`prod`/`pipeline-admin`).
4. In GitHub: **Settings → Environments**, create `dev`, `test`, `prod`, and `pipeline-admin`. Add 1 required reviewer to `test`, 2 to `prod`, and at least 1 to `pipeline-admin`. Add each `*_SP_CLIENT_ID` as an environment variable (`DEV_SP_CLIENT_ID`, `TEST_SP_CLIENT_ID`, `PROD_SP_CLIENT_ID`, `PIPELINE_ADMIN_SP_CLIENT_ID`).
5. Add repo-level variable `FABRIC_TENANT_ID`.
6. Fill in real values in `config/environments.yml` — deployment pipeline name/description, and per-stage workspace names, capacity IDs, RBAC principal IDs — and `workspace/parameter.yml` (Lakehouse/connection GUIDs, Spark pool IDs).
7. Replace the placeholder folders under `workspace/` with your actual committed Notebooks, Data Pipelines, Semantic Models (TMDL), and Reports (PBIR).
8. Run `python src/main.py setup-pipeline` once (locally or via the manual `setup-pipeline` CI job) to provision every stage's workspace and wire up the Fabric deployment pipeline before the first `deploy`.

## Running locally (testing only — never for real Test/Prod promotion)

```bash
az login
pip install -r requirements.txt

# One-time (or after adding/renaming a stage): provision workspaces + deployment pipeline mapping
python src/main.py setup-pipeline --config config/environments.yml

# Deploy items into one stage
python src/main.py deploy --stage dev --config config/environments.yml --repo-dir workspace

# Or deploy every configured stage in order
python src/main.py deploy --all-stages --config config/environments.yml --repo-dir workspace
```

## Running in CI

Push to `main` → auto-deploys Dev. Tag `vX.Y.Z` → deploys Test (after reviewer approval) → Prod (after 2 reviewers approve). Manually dispatch the workflow with `setup_pipeline: true` to (re)provision stage workspaces and the deployment pipeline mapping instead of deploying. See `.github/workflows/fabric-cicd.yml`.

## Design notes worth knowing before you rely on this

- **Stages are config-driven, not hardcoded.** `src/config.py` reads an ordered `stages:` list from `environments.yml`; `src/main.py` and `src/orchestrator.py` loop over whatever's defined there. Adding a `uat` stage between `test` and `prod` is a config change, not a code change (you'll also want to add a matching CI job and GitHub environment).
- **`setup-pipeline` and `deploy` are separate on purpose.** `setup-pipeline` provisions workspace topology and the Fabric deployment pipeline mapping across *every* stage in one run, which needs broader permissions than any single stage's deploy identity should have. `deploy` only ever touches one stage's workspace, using that stage's own scoped service principal.
- **Git connect is Dev-only, by design** (per this repo's default config — controlled per-stage via each stage's `git.connect` key). Test and Prod are never Git-connected — they're updated exclusively by this pipeline, which is what keeps "Git is the source of truth" actually true rather than aspirational.
- **RBAC sync is additive-only.** `sync_role_assignments` grants missing roles but never removes existing ones — access removal is a deliberate, separately-reviewed action, not something that should happen as a side effect of a routine deploy.
- **Deployment pipeline stage creation is additive-only too**, same reasoning: `ensure_deployment_pipeline` adds stages missing from config but never removes or reorders existing ones.
- **`fabric_environment_key` mismatches fail silently in fabric-cicd itself** (unresolved values just don't get replaced) — `validate_items.py` and `orchestrator.py`'s `validate_parameter_yml_key` both guard against this by failing loudly instead, but only if you keep each stage's `fabric_environment_key` in sync with `workspace/parameter.yml`'s keys.
- **API field names in `workspace_lifecycle.py` (Git integration) and `deployment_pipeline_service.py` (deployment pipelines) should be verified against current Microsoft Learn docs before production use** — these are the areas of the Fabric REST API most likely to have shifted since this was written. See the "NOTE ON API ACCURACY" comment at the top of each file.
- **No rollback.** A bad Prod deploy is fixed by a new corrective commit/tag, not a revert — standard "roll forward" discipline applies.
- **GitHub Enterprise Server (on-prem)** isn't supported by Fabric's native Git integration (Cloud only) — this only affects the Dev `connect_git` step; the GitHub Actions workflow itself works the same regardless of GitHub Enterprise Cloud vs. Server.

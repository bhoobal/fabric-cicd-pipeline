#!/usr/bin/env bash
# scripts/promote.sh
# Manual/local promotion helper. There is no "copy items from Dev workspace to
# Test workspace" step in this pipeline — Dev, Test, and Prod all deploy from
# the same workspace/ folder in this repo (see src/main.py); "promoting" just
# means running that deploy again with a different --env, so parameter.yml
# swaps in the target environment's GUIDs/connections instead of Dev's.
#
# This script is for promoting outside of CI (e.g. before GitHub environment
# secrets/reviewers are wired up, or for an ad hoc run). The normal path is:
# tag a commit (e.g. `git tag v1.0.0 && git push --tags`), which the
# deploy-test / deploy-prod jobs in .github/workflows/fabric-cicd.yml pick up
# automatically — see the `startsWith(github.ref, 'refs/tags/v')` conditions.
#
# Requires the same auth env vars as src/auth.py (FABRIC_TENANT_ID +
# FABRIC_CLIENT_ID/FABRIC_CLIENT_SECRET, or `az login` for local dev).
#
# Usage:
#   ./scripts/promote.sh test
#   ./scripts/promote.sh prod

set -euo pipefail

TARGET_ENV="${1:?Usage: promote.sh <test|prod>}"

if [[ "$TARGET_ENV" != "test" && "$TARGET_ENV" != "prod" ]]; then
  echo "Refusing to promote to '$TARGET_ENV' — valid targets are 'test' or 'prod'." >&2
  echo "(Dev isn't a promotion target: it's populated by Fabric's Git integration, not this script.)" >&2
  echo "(Stages beyond dev/test/prod can be added in config/environments.yml — update this check to match.)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Validating workspace/ before promoting to '$TARGET_ENV' ==="
python tests/validate_items.py --repo-dir workspace

echo
echo "About to deploy the current committed state of workspace/ ($(git rev-parse --short HEAD)) to '$TARGET_ENV'."
read -rp "Continue? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

echo "=== Deploying to '$TARGET_ENV' ==="
python src/main.py deploy --stage "$TARGET_ENV" --repo-dir workspace

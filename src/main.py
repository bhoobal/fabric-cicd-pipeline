"""
main.py
Single CLI entrypoint for the Fabric CI/CD pipeline. Two subcommands:

  setup-pipeline   Ensure every configured stage's workspace exists (capacity,
                    RBAC, Git connect where requested), ensure the Fabric
                    deployment pipeline exists with a matching stage per
                    config entry, and map each workspace to its stage. Run
                    this once per topology change - it does not deploy items.

  deploy            Provision one stage's workspace (idempotent, safe to
                    re-run) and deploy items into it via fabric-cicd. This is
                    what CI runs on every merge/tag, once per stage - or use
                    --all-stages to run every configured stage in order.

Stages are entirely config-driven (see config/environments.yml `stages:`) -
add, remove, or reorder stages there without touching this file.

Usage:
    python src/main.py setup-pipeline --config config/environments.yml
    python src/main.py deploy --stage dev  --config config/environments.yml --repo-dir workspace
    python src/main.py deploy --stage test --config config/environments.yml --repo-dir workspace
    python src/main.py deploy --stage prod --config config/environments.yml --repo-dir workspace
    python src/main.py deploy --all-stages --config config/environments.yml --repo-dir workspace

Re-running any of these is safe - every step is idempotent or explicitly
guarded (see the docstrings in orchestrator.py and the individual service
modules under src/ for specifics).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_pipeline_config, get_stage  # noqa: E402
import orchestrator  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fabric CI/CD pipeline")
    parser.add_argument("--config", default="config/environments.yml", help="Path to environments.yml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "setup-pipeline",
        help="Ensure stage workspaces exist and are mapped to the Fabric deployment pipeline",
    )

    deploy_parser = subparsers.add_parser("deploy", help="Deploy items to one stage or every stage")
    stage_group = deploy_parser.add_mutually_exclusive_group(required=True)
    stage_group.add_argument("--stage", help="Single stage key to deploy (must match a `key` in config)")
    stage_group.add_argument("--all-stages", action="store_true", help="Deploy every configured stage, in order")
    deploy_parser.add_argument(
        "--repo-dir", default="workspace",
        help="Path to the Fabric Git-synced workspace folder (must contain parameter.yml if using parameterization)",
    )

    return parser


def main():
    args = build_parser().parse_args()
    pipeline_config = load_pipeline_config(args.config)

    if args.command == "setup-pipeline":
        orchestrator.setup_deployment_pipeline(pipeline_config)
        return

    stages = pipeline_config.stages if args.all_stages else [get_stage(pipeline_config, args.stage)]
    for stage in stages:
        orchestrator.deploy_stage(stage, args.repo_dir)


if __name__ == "__main__":
    main()

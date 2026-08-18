"""
config.py
Loads config/environments.yml into an ordered list of stages plus the
deployment pipeline's own settings. Stage count and order are entirely
config-driven - dev/test/prod is a convention here, not something the code
assumes, so a project can add a "uat" stage or drop "test" without touching
any Python.
"""

from dataclasses import dataclass, field

import yaml


@dataclass
class StageConfig:
    key: str                       # short id used on the CLI, e.g. "dev"
    fabric_environment_key: str    # must match a key in workspace/parameter.yml
    workspace_name: str
    capacity_id: str
    description: str = ""
    pipeline_stage_name: str = ""  # display name in the Fabric deployment pipeline
    rbac: list = field(default_factory=list)   # passed straight through to sync_role_assignments
    git: dict = field(default_factory=dict)    # passed straight through to connect_git kwargs

    def __post_init__(self):
        if not self.pipeline_stage_name:
            self.pipeline_stage_name = self.key.capitalize()


@dataclass
class PipelineConfig:
    name: str
    description: str
    stages: list  # list[StageConfig], ordered upstream (dev) -> downstream (prod)


def load_pipeline_config(config_path: str) -> PipelineConfig:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    stages_raw = raw.get("stages")
    if not stages_raw:
        raise SystemExit(f"No 'stages' list defined in {config_path}.")

    stages = []
    for entry in stages_raw:
        try:
            stages.append(StageConfig(
                key=entry["key"],
                fabric_environment_key=entry["fabric_environment_key"],
                workspace_name=entry["workspace_name"],
                capacity_id=entry["capacity_id"],
                description=entry.get("description", ""),
                pipeline_stage_name=entry.get("pipeline_stage_name", ""),
                rbac=entry.get("rbac", []),
                git=entry.get("git", {}),
            ))
        except KeyError as exc:
            raise SystemExit(f"Stage entry {entry.get('key', '?')} in {config_path} is missing required key: {exc}") from exc

    duplicate_keys = {s.key for s in stages if [x.key for x in stages].count(s.key) > 1}
    if duplicate_keys:
        raise SystemExit(f"Duplicate stage key(s) in {config_path}: {sorted(duplicate_keys)}")

    dp_raw = raw.get("deployment_pipeline", {})
    return PipelineConfig(
        name=dp_raw.get("name", "Deployment Pipeline"),
        description=dp_raw.get("description", ""),
        stages=stages,
    )


def get_stage(pipeline_config: PipelineConfig, key: str) -> StageConfig:
    for stage in pipeline_config.stages:
        if stage.key == key:
            return stage
    available = [s.key for s in pipeline_config.stages]
    raise SystemExit(f"Stage '{key}' not found. Available: {available}")

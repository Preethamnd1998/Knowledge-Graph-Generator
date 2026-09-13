"""Loader for kg-config.env — a .env-style file extended with commentable
list blocks (KEY_START ... KEY_END) so non-technical users can enable or
disable a service/subproject by adding or removing a leading '#'.
"""
import os
from dataclasses import dataclass, field

VALID_MODES = ("all", "backend_only", "frontend_only", "selective")

# These three keys may be overridden by an environment variable of the same
# name, taking precedence over the file — mainly so a Docker container can
# set ROOT_PATH=/workspace without editing the mounted config file.
ENV_OVERRIDABLE_KEYS = ("ROOT_PATH", "OUTPUT_DIR", "REACT_PROJECT_PATH")


class ConfigError(Exception):
    pass


@dataclass
class ListSetting:
    enabled: list = field(default_factory=list)
    disabled: list = field(default_factory=list)

    @property
    def all_known(self):
        return self.enabled + self.disabled


@dataclass
class Config:
    root_path: str
    angular_project: str
    react_project_path: str
    index_mode: str
    output_dir: str
    single_database: bool
    incremental: bool
    backend_services: ListSetting
    angular_subprojects: ListSetting
    source_path: str


def _strip_inline_comment(value):
    # Values are paths / identifiers — a bare "#" with nothing after "=" is
    # the only comment form we need to worry about inside VALUE.
    return value.strip()


def load_config(path):
    if not os.path.isfile(path):
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        raw_lines = fh.readlines()

    scalars = {}
    lists = {}  # key -> ListSetting
    current_block = None

    for line_no, raw in enumerate(raw_lines, start=1):
        line = raw.rstrip("\n").rstrip("\r")
        stripped = line.strip()

        if current_block is not None:
            if stripped.endswith("_END") and stripped[:-4] == current_block:
                current_block = None
                continue
            if not stripped or stripped == "#":
                continue
            if stripped.startswith("#"):
                item = stripped.lstrip("#").strip()
                if item:
                    lists[current_block].disabled.append(item)
            else:
                lists[current_block].enabled.append(stripped)
            continue

        if not stripped or stripped.startswith("#"):
            continue

        if stripped.endswith("_START"):
            block_key = stripped[: -len("_START")]
            current_block = block_key
            lists.setdefault(block_key, ListSetting())
            continue

        if "=" not in stripped:
            raise ConfigError(
                f"{path}:{line_no}: expected KEY=VALUE, got: {stripped!r}"
            )

        key, _, value = stripped.partition("=")
        scalars[key.strip()] = _strip_inline_comment(value)

    if current_block is not None:
        raise ConfigError(
            f"{path}: block '{current_block}_START' was never closed with "
            f"'{current_block}_END'"
        )

    for key in ENV_OVERRIDABLE_KEYS:
        env_val = os.environ.get(key)
        if env_val:
            scalars[key] = env_val

    def require_scalar(key):
        if key not in scalars:
            raise ConfigError(f"{path}: missing required key '{key}'")
        return scalars[key]

    root_path = require_scalar("ROOT_PATH")
    angular_project = scalars.get("ANGULAR_PROJECT", "")
    react_project_path = scalars.get("REACT_PROJECT_PATH", "")
    index_mode = scalars.get("INDEX_MODE", "all").strip().lower()
    output_dir = scalars.get("OUTPUT_DIR", "./output")
    single_database_raw = scalars.get("SINGLE_DATABASE", "true").strip().lower()
    single_database = single_database_raw in ("true", "1", "yes", "y")
    # First run always fully indexes regardless (no prior .db to diff against).
    # Set INCREMENTAL=false to force a full rescan even when a prior .db exists
    # (e.g. after a schema/parser change where stale-but-fingerprint-matching
    # nodes could otherwise linger).
    incremental_raw = scalars.get("INCREMENTAL", "true").strip().lower()
    incremental = incremental_raw in ("true", "1", "yes", "y")

    if index_mode not in VALID_MODES:
        raise ConfigError(
            f"{path}: INDEX_MODE={index_mode!r} is invalid — must be one of "
            f"{', '.join(VALID_MODES)}"
        )

    config_dir = os.path.dirname(os.path.abspath(path))
    if output_dir and not os.path.isabs(output_dir):
        output_dir = os.path.abspath(os.path.join(config_dir, output_dir))

    return Config(
        root_path=root_path,
        angular_project=angular_project,
        react_project_path=react_project_path,
        index_mode=index_mode,
        output_dir=output_dir,
        single_database=single_database,
        incremental=incremental,
        backend_services=lists.get("BACKEND_SERVICES", ListSetting()),
        angular_subprojects=lists.get("ANGULAR_SUBPROJECTS", ListSetting()),
        source_path=os.path.abspath(path),
    )

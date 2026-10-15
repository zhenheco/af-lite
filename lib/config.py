"""Resolve and validate the supported af-lite config fields."""

import json
import math
import os
from pathlib import Path
import re
import subprocess

FIELDS = {"roles", "quota_stop", "quota_retry_minutes",
          "worker_timeout_minutes", "test_timeout_minutes", "stale_after_minutes",
          "skill_dirs", "auto_merge", "high_risk_paths", "tests", "record_retention"}

def primary_checkout_root(cwd):
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.returncode: return None
    common = Path(result.stdout.strip()).resolve()
    return common.parent if common.name == ".git" else None

def validate_config_location(path):
    directory = path.parent
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    if not any((parent / ".git").exists() for parent in (directory, *directory.parents)):
        return path
    try:
        result = subprocess.run(["git", "-C", str(directory), "rev-parse", "--path-format=absolute",
                                 "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError(f"cannot validate config location without git: {path}") from exc
    details = result.stdout.splitlines()
    if result.returncode == 0 and len(details) == 3:
        git_dir, common, branch = details
        if branch.startswith("af/") and Path(git_dir).resolve() != Path(common).resolve():
            raise RuntimeError(f"config path is inside linked worktree on af/* branch: {path}")
    return path

def resolve_config_path(explicit=None, cwd=None, env=None):
    env = os.environ if env is None else env
    selected = explicit or env.get("AF_CONFIG")
    if selected:
        path = Path(selected).expanduser().resolve()
    else:
        root = primary_checkout_root(Path.cwd() if cwd is None else Path(cwd))
        local = root / "af.config.json" if root else None
        path = local.resolve() if local and local.is_file() else (
            Path(env.get("HOME", str(Path.home()))) / ".config/af/af.config.json").resolve()
    return validate_config_location(path)

def load_config(explicit=None, cwd=None, env=None, require=True):
    env = os.environ if env is None else env
    path = resolve_config_path(explicit, cwd, env)
    if not path.is_file():
        if require or explicit or env.get("AF_CONFIG"):
            raise RuntimeError(f"config file not found: {path}")
        return {}, path
    try:
        config = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid config {path}: {exc}") from exc
    validate_config(config)
    return config, path

def require_object(value, name, allowed=None):
    if not isinstance(value, dict) or allowed is not None and set(value) - allowed:
        raise ValueError(f"{name} has invalid fields or type")
    return value

def require_strings(value, name):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")

def require_number(value, name, minimum, integer=False):
    kind = int if integer else (int, float)
    if value is None:
        return
    if (not isinstance(value, kind) or isinstance(value, bool) or value <= minimum
            or not integer and not math.isfinite(value)):
        qualifier = "nonnegative" if minimum < 0 else "positive"
        raise ValueError(f"{name} must be a {qualifier} {'integer' if integer else 'number'}")

def validate_config(config):
    require_object(config, "config", FIELDS)
    roles = require_object(config.get("roles", {}), "roles")
    settings = {"pi": {"provider", "model"}, "codex": {"profile"}, "host": {"model"}}
    for role, candidates in roles.items():
        if not isinstance(role, str) or not isinstance(candidates, list):
            raise ValueError("roles must map role names to candidate arrays")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError(f"role {role} candidate must be an object")
            via = candidate.get("via")
            required = settings.get(via)
            if required is None or set(candidate) - (required | {"via", "company"}):
                raise ValueError(f"role {role} candidate must use pi, codex, or host settings")
            if any(not isinstance(candidate.get(key), str) or not candidate[key] for key in required):
                raise ValueError(f"role {role} candidate is missing CLI settings")
            if not isinstance(candidate.get("company"), str) or not candidate["company"].strip():
                raise ValueError(f"role {role} candidate needs a company")

    for field in ("skill_dirs", "high_risk_paths"):
        if field in config:
            require_strings(config[field], field)
    if "auto_merge" in config and not isinstance(config["auto_merge"], bool):
        raise ValueError("auto_merge must be a boolean")

    for field in ("worker_timeout_minutes", "test_timeout_minutes"):
        require_number(config.get(field), field, 0)
    for field in ("stale_after_minutes", "quota_retry_minutes"):
        require_number(config.get(field), field, 0, integer=True)

    quota = require_object(config.get("quota_stop", {}), "quota_stop", {"exit_statuses", "patterns"})
    statuses = quota.get("exit_statuses", [75])
    if not isinstance(statuses, list) or any(not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 255
                                             for code in statuses):
        raise ValueError("quota_stop.exit_statuses must be integers from 0-255")
    patterns = quota.get("patterns", [])
    require_strings(patterns, "quota_stop.patterns")
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid quota pattern {pattern!r}: {exc}") from exc

    tests = require_object(config.get("tests", {}), "tests", {"focused", "full"})
    if any(not isinstance(command, str) or not command for command in tests.values()):
        raise ValueError("test commands must be nonempty strings")
    retention = require_object(config.get("record_retention", {}), "record_retention",
                               {"archive_after_days", "archive_dir", "delete_after_days"})
    for field in ("archive_after_days", "delete_after_days"):
        require_number(retention.get(field), f"record_retention.{field}", -1, integer=True)
    archive, delete = retention.get("archive_after_days", 30), retention.get("delete_after_days", 180)
    if archive is not None and delete is not None and delete < archive:
        raise ValueError("delete_after_days must not precede archive_after_days")
    if retention.get("archive_dir") is not None and not isinstance(retention["archive_dir"], str):
        raise ValueError("record_retention.archive_dir must be a path or null")

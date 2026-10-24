"""Check Git, configured Matt skills, optional CLIs, and setup docs."""

import json
import os
from pathlib import Path
import shutil
import subprocess

from config import load_config

REQUIRED_SKILLS = ("grill-with-docs", "to-spec", "to-tickets", "implement-spec", "implement", "tdd",
                   "code-review", "diagnosing-bugs", "pr", "retro")
OPTIONAL_TOOLS = ("pi", "codex", "claude", "gitleaks")

def skill_directories(config, repo):
    home = Path(os.environ.get("HOME", str(Path.home())))
    values = config.get("skill_dirs", [home / ".claude/skills", home / ".agents/skills", repo / "skills"])
    paths = [Path(value).expanduser() for value in values]
    return [path.resolve() if path.is_absolute() else (repo / path).resolve() for path in paths]

def check(args):
    git_available = shutil.which("git") is not None
    result = subprocess.run(["git", "-C", str(args.repo), "rev-parse", "--show-toplevel"],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) if git_available else None
    repo = Path(result.stdout.strip()).resolve() if result and result.returncode == 0 else None
    try:
        config, config_path = load_config(args.config, cwd=repo or Path(args.repo))
        config_report = {"valid": True, "path": str(config_path), "error": None}
    except (OSError, RuntimeError, ValueError) as exc:
        config = {}
        config_report = {"valid": False, "path": None, "error": str(exc)}

    directories = skill_directories(config, repo or Path(args.repo))
    found = [name for name in REQUIRED_SKILLS
             if any((path / name / "SKILL.md").is_file() for path in directories)]
    missing = [name for name in REQUIRED_SKILLS if name not in found]
    docs = [name for name in ("docs/agents/issue-tracker.md", "docs/agents/domain.md")
            if repo and not (repo / name).is_file()]
    report = {"ok": bool(git_available and repo and config_report["valid"] and not missing),
              "git": {"available": git_available, "repo": str(repo) if repo else None},
              "skills": {"required": list(REQUIRED_SKILLS), "found": found, "missing": missing},
              "repository_setup": {"missing_files": docs,
                                   "warnings": [f"missing {', '.join(docs)}; run /setup-matt-pocock-skills once in this repository"]
                                   if docs else []},
              "optional_tools": {name: shutil.which(name) is not None for name in OPTIONAL_TOOLS},
              "config": config_report}
    return report

def add_doctor_parser(commands):
    parser = commands.add_parser("doctor", help="check af-lite prerequisites")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--config")

def run_doctor(args):
    report = check(args)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1

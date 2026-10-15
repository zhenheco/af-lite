"""Create and safely remove af-owned sibling Git worktrees."""

import json
import re
import subprocess
from pathlib import Path

FLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode: raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result

def git_output(repo, *args, check=True):
    return git(repo, *args, check=check).stdout.strip()

def repository_root(repo):
    return Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()

def common_dir(repo):
    path = git_output(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(path).resolve()

def primary_checkout(repo):
    common = common_dir(repo)
    if common.name != ".git": raise RuntimeError("af worktrees require a non-bare Git checkout")
    return common.parent

def flow_path(repo, flow_id):
    return repo.parent / f"{repo.name}-{flow_id}"

def validate_flow_id(flow_id):
    if not FLOW_ID_RE.fullmatch(flow_id): raise ValueError("flow id must contain only letters, numbers, dot, dash, or underscore")

def add_worktree_parser(commands):
    parser = commands.add_parser("worktree", help="manage flow-owned worktrees")
    actions = parser.add_subparsers(dest="worktree_action", required=True)
    for name in ("create", "remove"):
        action = actions.add_parser(name)
        action.add_argument("--repo", required=True)
        action.add_argument("--flow-id", required=True)
        if name == "remove":
            action.add_argument("--force", action="store_true")

def create(repo_arg, flow_id):
    validate_flow_id(flow_id)
    repo = primary_checkout(repo_arg)
    branch = f"af/{flow_id}"
    target = flow_path(repo, flow_id)
    if target.exists(): raise RuntimeError(f"refusing to overwrite existing path: {target}")
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise RuntimeError(f"branch already exists: {branch}")

    base_ref = git_output(repo, "symbolic-ref", "--quiet", "HEAD", check=False)
    base_sha = git_output(repo, "rev-parse", "HEAD")
    git(repo, "worktree", "add", "-b", branch, str(target), base_sha)

    if base_ref:
        git(repo, "branch", "--set-upstream-to", base_ref, branch)
    print(json.dumps({"flow_id": flow_id, "branch": branch, "path": str(target), "base_sha": base_sha}))

def remove(repo_arg, flow_id, force=False):
    validate_flow_id(flow_id)
    repo = primary_checkout(repo_arg)
    branch = f"af/{flow_id}"
    target = flow_path(repo, flow_id)
    output = git_output(repo, "worktree", "list", "--porcelain")
    entries = (dict(line.partition(" ")[::2] for line in block.splitlines() if line)
               for block in output.split("\n\n"))
    entry = next((item for item in entries if Path(item.get("worktree", "")).resolve() == target.resolve()), None)
    if not entry or entry.get("branch") != f"refs/heads/{branch}":
        raise RuntimeError(f"flow worktree not found on {branch}: {target}")

    dirty = git(target, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching").stdout
    if dirty and not force:
        raise RuntimeError("refusing to remove a dirty flow worktree; review it or use --force")
    if not force:
        base = git(repo, "rev-parse", "--verify", f"{branch}@{{upstream}}", check=False)
        if base.returncode:
            raise RuntimeError("flow branch has no recorded base; use --force after reviewing it")
        unmerged = git(repo, "merge-base", "--is-ancestor", f"refs/heads/{branch}", base.stdout.strip(), check=False)
        if unmerged.returncode:
            raise RuntimeError("flow branch has commits not merged into its base; use --force after reviewing them")

    git(repo, "worktree", "remove", *(["--force"] if force else []), str(target))
    git(repo, "branch", "-D" if force else "-d", branch)
    print(json.dumps({"flow_id": flow_id, "removed": True, "branch": branch,
                      "path": str(target)}))

def run_worktree(args):
    if args.worktree_action == "create":
        create(args.repo, args.flow_id)
    else:
        remove(args.repo, args.flow_id, args.force)

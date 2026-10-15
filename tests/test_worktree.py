import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class WorktreeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env.pop("AF_CONFIG", None)
        self.repo = self.base / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "fixture")
        self.checkout_sha = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.temp.cleanup()

    def run_af(self, *args):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_create_and_remove_owned_worktree_without_touching_checkout(self):
        created = self.run_af("worktree", "create", "--repo", str(self.repo),
                              "--flow-id", "test-001")
        self.assertEqual(created.returncode, 0, created.stderr)
        record = json.loads(created.stdout)
        worktree = Path(record["path"])
        self.assertEqual(record["branch"], "af/test-001")
        self.assertTrue((worktree / "README.md").is_file())
        self.assertEqual(git(self.repo, "config", "--get", "branch.af/test-001.merge"),
                         git(self.repo, "symbolic-ref", "HEAD"))
        self.assertFalse((self.repo / ".git/.af/worktrees/test-001.json").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.checkout_sha)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        removed = self.run_af("worktree", "remove", "--repo", str(self.repo),
                              "--flow-id", "test-001")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "af/test-001"), "")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.checkout_sha)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_remove_allows_flow_branch_tip_already_merged_into_base(self):
        flow_id = "merged-flow"
        created = self.run_af("worktree", "create", "--repo", str(self.repo),
                              "--flow-id", flow_id)
        worktree = Path(json.loads(created.stdout)["path"])
        (worktree / "merged.txt").write_text("already in base\n")
        git(worktree, "add", "merged.txt")
        git(worktree, "commit", "-qm", "flow change")
        git(self.repo, "merge", "--ff-only", f"af/{flow_id}")
        removed = self.run_af("worktree", "remove", "--repo", str(self.repo), "--flow-id", flow_id)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", f"af/{flow_id}"), "")

if __name__ == "__main__":
    unittest.main()

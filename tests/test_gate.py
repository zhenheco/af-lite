import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class GateCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.test_log = self.base / "test-log"
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["TEST_LOG"] = str(self.test_log)
        self.env.pop("AF_CONFIG", None)
        self.repo = self.base / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        self.hooks = self.base / "empty-hooks"
        self.hooks.mkdir()
        git(self.repo, "config", "core.hooksPath", str(self.hooks))
        (self.repo / "README.md").write_text("fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "fixture")
        self.test_bin = self.bin / "fake-test"
        self.test_bin.write_text("#!/bin/sh\necho \"$1\" >> \"$TEST_LOG\"\nexit 0\n")
        self.test_bin.chmod(0o755)
        self.config = self.base / "af.config.json"
        self.config.write_text(json.dumps({"tests": {
            "focused": "fake-test focused", "full": "fake-test full"
        }, "auto_merge": True, "high_risk_paths": []}))

    def tearDown(self):
        self.temp.cleanup()

    def fake_gitleaks(self, exit_code=0):
        path = self.bin / "gitleaks"
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TEST_LOG\"\nexit {exit_code}\n")
        path.chmod(0o755)
        return path

    def disable_gitleaks(self):
        tools = self.base / "no-gitleaks"
        tools.mkdir(exist_ok=True)
        for name in ("bash", "python3", "git", "dirname", "env"):
            (tools / name).symlink_to(shutil.which(name))
        self.env["PATH"] = f"{self.bin}{os.pathsep}{tools}"

    def run_af(self, *args):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], cwd=self.repo, env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def create_flow(self, flow_id):
        created = self.run_af("worktree", "create", "--repo", self.repo,
                              "--flow-id", flow_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree = Path(json.loads(created.stdout)["path"])
        started = self.run_af("state", "start", "--repo", worktree, "--flow-id", flow_id,
                              "--base-sha", git(worktree, "rev-parse", "HEAD"),
                              "--config", self.config, "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)
        return worktree

    def test_runs_the_configured_focused_and_full_test_commands(self):
        worktree = self.create_flow("gate-tests")
        for suite in ("focused", "full"):
            result = self.run_af("gate", "test", "--repo", worktree, "--flow-id", "gate-tests",
                                 "--suite", suite)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["suite"], suite)
            self.assertNotIn("stdout", report)
            self.assertNotIn("stderr", report)
        self.assertEqual(self.test_log.read_text().splitlines(), ["focused", "full"])

    def test_test_and_decision_require_a_recorded_flow(self):
        test = self.run_af("gate", "test", "--repo", self.repo, "--config", self.config, "--suite", "focused")
        decision = self.run_af("gate", "decide", "--repo", self.repo, "--config", self.config,
                               "--base", git(self.repo, "rev-parse", "HEAD"),
                               "--tests", "pass", "--reviews", "pass")
        self.assertNotEqual(test.returncode, 0)
        self.assertNotEqual(decision.returncode, 0)

    def test_high_risk_checks_create_info_directory_with_custom_git_template(self):
        template = self.base / "git-template"
        template.mkdir()
        (self.home / ".gitconfig").write_text(f"[init]\n\ttemplateDir = {template}\n")
        worktree = self.create_flow("gate-template")
        (worktree / "config" / "secrets").mkdir(parents=True)
        (worktree / "config" / "secrets" / "token.json").write_text("fixture\n")
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": ["config/secrets"]}))

        committed = self.run_af("gate", "commit", "--repo", worktree, "--flow-id", "gate-template",
                                "--message", "commit high-risk fixture", "--paths", "config/secrets/token.json")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-template",
                             "--tests", "pass", "--reviews", "pass")

        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)
        self.assertEqual(decision["decision"], "ask")
        self.assertEqual(decision["high_risk_paths"], ["config/secrets/token.json"])

    def test_high_risk_paths_remain_visible_after_host_commit(self):
        worktree = self.create_flow("gate-risk")
        (worktree / "payments").mkdir()
        (worktree / "payments" / "api.py").write_text("value = 1\n")
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": ["payments/**"]}))
        committed = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-risk",
                                "--message", "commit high-risk fixture", "--paths", "payments/api.py")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        decision = self.run_af("gate", "decide", "--repo", str(worktree), "--flow-id", "gate-risk",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        self.assertEqual(json.loads(decision.stdout)["decision"], "ask")

    def test_decide_blocks_uncommitted_staged_unstaged_and_untracked_changes(self):
        worktree = self.create_flow("gate-dirty")

        def decide():
            result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-dirty",
                                 "--tests", "pass", "--reviews", "pass")
            self.assertEqual(result.returncode, 2, result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record["decision"], "blocked")
            self.assertEqual(record["reasons"], ["uncommitted changes: run af gate commit first"])

        (worktree / "README.md").write_text("unstaged\n")
        decide()
        git(worktree, "reset", "--hard", "HEAD")
        (worktree / "staged.txt").write_text("staged\n")
        git(worktree, "add", "staged.txt")
        decide()
        git(worktree, "reset", "--hard", "HEAD")
        (worktree / "untracked.txt").write_text("untracked\n")
        decide()

    def test_decide_returns_exit_two_for_failed_checks_and_zero_for_merge(self):
        worktree = self.create_flow("gate-exit-status")
        for tests, reviews in (("fail", "pass"), ("pass", "fail")):
            result = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-exit-status",
                                 "--tests", tests, "--reviews", reviews)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "blocked")

        merged = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-exit-status",
                             "--tests", "pass", "--reviews", "pass")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertEqual(json.loads(merged.stdout)["decision"], "merge")

    def test_decide_with_flow_id_reads_committed_changes_from_flow_branch(self):
        worktree = self.create_flow("gate-flow-head")
        status = self.run_af("state", "status", "--repo", str(self.repo), "--flow-id", "gate-flow-head")
        self.assertEqual(json.loads(status.stdout)["worktree_path"], str(worktree))
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": ["src/auth"]}))
        target = worktree / "src" / "auth" / "token.py"
        target.parent.mkdir(parents=True)
        target.write_text("value = 1\n")
        committed = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-flow-head",
                                "--message", "commit flow risk", "--paths", "src/auth/token.py")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        decision = self.run_af("gate", "decide", "--repo", str(self.repo), "--flow-id", "gate-flow-head",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        result = json.loads(decision.stdout)
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(result["high_risk_paths"], ["src/auth/token.py"])

    def test_high_risk_paths_follow_git_check_ignore_for_slash_patterns(self):
        worktree = self.create_flow("gate-patterns")
        patterns = ["src/auth", "src/auth/", "db/*", "config/secrets", "a/**/b",
                    "**/secrets", "/db/*.sql", "ignored/"]
        paths = ["src/auth/token.py", "db/migrations/1.sql", "config/secrets/client.json",
                 "a/x/b/file.txt", "nested/secrets/key.txt", "db/direct.sql", "nested/db/direct.sql",
                 "ignored/secret.txt"]
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(patterns) + "\n")
        for path in paths:
            target = worktree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture\n")
        tracked_paths = [path for path in paths if not path.startswith("ignored/")]
        git(worktree, "add", "-f", *tracked_paths)
        git(worktree, "commit", "-qm", "add risk pattern fixtures")
        expected = subprocess.run(
            ["git", "-C", str(self.repo), "check-ignore", "--no-index", "-z", "--stdin"],
            input=("\0".join(tracked_paths) + "\0").encode(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True).stdout.decode().split("\0")
        expected = sorted(path for path in expected if path)
        self.config.write_text(json.dumps({"auto_merge": True, "high_risk_paths": patterns}))
        decision = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-patterns",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(decision.returncode, 0, decision.stderr)
        result = json.loads(decision.stdout)
        self.assertEqual(result["high_risk_paths"], expected)
        self.assertEqual(result["decision"], "ask" if expected else "merge")

    def test_commit_scans_explicit_paths_and_commits_only_the_requested_file(self):
        self.fake_gitleaks()
        worktree = self.create_flow("gate-selected")
        (worktree / "safe.txt").write_text("ordinary content\n")
        (worktree / "other.txt").write_text("also changed\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-selected",
                             "--message", "commit selected path", "--paths", "safe.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["paths"], ["safe.txt"])
        changed = git(worktree, "show", "--pretty=format:", "--name-only", "HEAD").splitlines()
        self.assertEqual(changed, ["safe.txt"])
        self.assertTrue((worktree / "other.txt").exists())
        self.assertIn("protect --staged", self.test_log.read_text())

    def test_commit_accepts_internal_relative_symlink_and_scans_link_text(self):
        self.disable_gitleaks()
        worktree = self.create_flow("gate-relative-link")
        (worktree / "target.txt").write_text("safe target\n")
        (worktree / "alias.txt").symlink_to("target.txt")
        (worktree / "link.txt").symlink_to("alias.txt")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-relative-link",
                             "--message", "commit internal symlink", "--paths", "link.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(worktree, "show", "HEAD:link.txt"), "alias.txt")

        (worktree / "secret-link").symlink_to("ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789")
        before = git(worktree, "rev-parse", "HEAD")
        refused = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-relative-link",
                              "--message", "reject secret in symlink text", "--paths", "secret-link")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("secret pattern github-token", refused.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--name-only"), "")

    def test_commit_accepts_parent_relative_directory_symlink_inside_worktree(self):
        self.disable_gitleaks()
        target = self.repo / "agents" / "x"
        target.mkdir(parents=True)
        (target / "README.md").write_text("agent skill fixture\n")
        git(self.repo, "add", "agents/x/README.md")
        git(self.repo, "commit", "-qm", "add internal directory target")
        worktree = self.create_flow("gate-parent-relative-link")
        skills = worktree / "skills"
        skills.mkdir()
        (skills / "x").symlink_to("../agents/x")

        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-parent-relative-link",
                             "--message", "commit internal sibling directory symlink", "--paths", "skills/x")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(worktree, "show", "HEAD:skills/x"), "../agents/x")
        self.assertTrue((worktree / "skills" / "x").resolve().is_dir())

    def test_commit_refuses_unsafe_symlinks_and_restores_the_index(self):
        worktree = self.create_flow("gate-unsafe-links")
        (worktree / "preserved.txt").write_text("already staged\n")
        git(worktree, "add", "preserved.txt")
        original_index = git(worktree, "diff", "--cached", "--binary")
        cases = [
            ("absolute-link", "/etc/passwd"),
            ("escaping-link", "../../outside"),
        ]
        before = git(worktree, "rev-parse", "HEAD")
        for name, target in cases:
            (worktree / name).symlink_to(target)
            result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-unsafe-links",
                                 "--message", "refuse unsafe symlink", "--paths", name)
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn("symlink", result.stderr.lower(), name)
            self.assertIn("target", result.stderr.lower(), name)
            self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
            self.assertEqual(git(worktree, "diff", "--cached", "--binary"), original_index, name)
            (worktree / name).unlink()

    def test_commit_refuses_symlink_escape_through_an_intermediate_symlink(self):
        worktree = self.create_flow("gate-intermediate-escape")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file").write_text("outside\n")
        (worktree / "inner").symlink_to("../outside")
        outer = worktree / "outer"
        outer.symlink_to("inner/file")
        target = Path(os.readlink(outer))
        self.assertFalse(target.is_absolute())
        self.assertNotIn("..", target.parts)
        (worktree / "preserved.txt").write_text("already staged\n")
        git(worktree, "add", "preserved.txt")
        original_index = git(worktree, "diff", "--cached", "--binary")
        before = git(worktree, "rev-parse", "HEAD")

        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-intermediate-escape",
                             "--message", "refuse nested symlink escape", "--paths", "outer")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink target escapes the worktree", result.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--binary"), original_index)

    def test_commit_ignores_a_preexisting_token_on_an_unchanged_line(self):
        self.disable_gitleaks()
        token = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        fixture = self.repo / "existing_fixture.py"
        fixture.write_text(f"fixture = '{token}'\nvalue = 0\n")
        git(self.repo, "add", "existing_fixture.py")
        git(self.repo, "commit", "-qm", "add preexisting fake fixture")
        worktree = self.create_flow("gate-added-lines-existing")
        (worktree / "existing_fixture.py").write_text(f"fixture = '{token}'\nvalue = 1\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-added-lines-existing",
                             "--message", "change unrelated fixture line", "--paths", "existing_fixture.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(token, git(worktree, "show", "HEAD:existing_fixture.py"))

    def test_commit_rejects_tokens_on_added_lines_and_in_new_files(self):
        self.disable_gitleaks()
        fixture = self.repo / "fixture.py"
        fixture.write_text("value = 0\n")
        git(self.repo, "add", "fixture.py")
        git(self.repo, "commit", "-qm", "add fixture")
        worktree = self.create_flow("gate-added-lines-reject")
        token = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        before = git(worktree, "rev-parse", "HEAD")
        (worktree / "fixture.py").write_text(f"value = 0\nGITHUB_TOKEN = '{token}'\n")
        added = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-added-lines-reject",
                            "--message", "reject added token", "--paths", "fixture.py")
        self.assertNotEqual(added.returncode, 0)
        self.assertIn("secret pattern github-token", added.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--name-only"), "")

        (worktree / "fixture.py").write_text("value = 0\n")
        (worktree / "new_fixture.py").write_text(f"GITHUB_TOKEN = '{token}'\n")
        new_file = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-added-lines-reject",
                               "--message", "reject token in new file", "--paths", "new_fixture.py")
        self.assertNotEqual(new_file.returncode, 0)
        self.assertIn("secret pattern github-token", new_file.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(git(worktree, "diff", "--cached", "--name-only"), "")

    def test_gitleaks_scans_the_staged_index(self):
        self.fake_gitleaks()
        worktree = self.create_flow("gate-gitleaks")
        (worktree / "review.txt").write_text("ordinary text\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-gitleaks",
                             "--message", "use gitleaks", "--paths", "review.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["secret_scan"], "gitleaks")
        self.assertEqual(self.test_log.read_text().strip(), "protect --staged")

    def test_fallback_secret_scan_blocks_common_key_patterns_without_committing(self):
        isolated_path = self.base / "without-gitleaks"
        isolated_path.mkdir()
        for name, target in (("bash", shutil.which("bash")), ("python3", shutil.which("python3")),
                             ("env", "/usr/bin/env"), ("git", "/usr/bin/git"),
                             ("dirname", "/usr/bin/dirname")):
            (isolated_path / name).symlink_to(target)
        self.env["PATH"] = str(isolated_path)
        worktree = self.create_flow("gate-secret")
        secret_file = worktree / "leak.txt"
        secret_file.write_text("api_key=abcdefghijklmnopqrstuvwxyz123456\n")
        before = git(worktree, "rev-parse", "HEAD")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-secret",
                             "--message", "must not commit", "--paths", "leak.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret pattern", result.stderr.lower())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)

    def test_gitleaks_failure_and_auto_merge_disabled_both_stop(self):
        self.fake_gitleaks(exit_code=1)
        worktree = self.create_flow("gate-hook")
        (worktree / "review.txt").write_text("ordinary text\n")
        result = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "gate-hook",
                             "--message", "scanner refusal", "--paths", "review.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(worktree, "status", "--porcelain"), "?? review.txt")

        self.config.write_text(json.dumps({"auto_merge": False, "high_risk_paths": []}))
        worktree = self.create_flow("gate-auto-merge")
        decision = self.run_af("gate", "decide", "--repo", worktree, "--flow-id", "gate-auto-merge",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(json.loads(decision.stdout)["decision"], "ask")


if __name__ == "__main__":
    unittest.main()

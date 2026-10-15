import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "minimal_repo"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class FlowEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        self.env.pop("AF_CONFIG", None)
        self.repo = self.base / "project"
        shutil.copytree(FIXTURE, self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.write_exe("pi", "#!/bin/sh\n"
                       "case \"${FAKE_PI_MODE:-success}\" in\n"
                       "  quota) echo 'rate limit exceeded' >&2; exit 75 ;;\n"
                       "  change-quota) echo interrupted > changed-after-quota.txt; echo 'quota exceeded' >&2; exit 75 ;;\n"
                       "  *) echo pi-ok; exit 0 ;;\n"
                       "esac\n")
        self.write_exe("codex", "#!/bin/sh\n"
                       "if [ -n \"${FAKE_CODEX_MARKER:-}\" ]; then echo called >> \"$FAKE_CODEX_MARKER\"; fi\n"
                       "if [ -n \"${FAKE_CODEX_FILE:-}\" ]; then mkdir -p \"$(dirname \"$FAKE_CODEX_FILE\")\"; echo implementation > \"$FAKE_CODEX_FILE\"; fi\n"
                       "echo codex-ok; exit 0\n")
        self.write_exe("gh")
        self.write_exe("gitleaks")
        self.config = self.base / "af.config.json"

    def tearDown(self):
        self.temp.cleanup()

    def write_exe(self, name, body="#!/bin/sh\nexit 0\n"):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def write_config(self, roles, **extra):
        value = {"roles": roles,
                 "quota_stop": {"exit_statuses": [75], "patterns": ["quota exceeded", "rate limit exceeded"]},
                 "tests": {"focused": "true", "full": "true"},
                 "auto_merge": True, "high_risk_paths": [], **extra}
        self.config.write_text(json.dumps(value))

    def run_af(self, *args, env=None):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], cwd=self.repo,
                              env=env or self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def create_flow(self, flow_id, kind, chain):
        created = self.run_af("worktree", "create", "--repo", str(self.repo), "--flow-id", flow_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        worktree_record = json.loads(created.stdout)
        worktree = Path(worktree_record["path"])
        started = self.run_af("state", "start", "--repo", str(worktree), "--flow-id", flow_id,
                              "--base-sha", worktree_record["base_sha"], "--config", str(self.config),
                              "--chain", *chain)
        self.assertEqual(started.returncode, 0, started.stderr)
        return worktree, Path(json.loads(started.stdout)["state_path"])

    def advance(self, worktree, flow_id):
        result = self.run_af("state", "advance", "--repo", str(worktree), "--flow-id", flow_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["step"]["skill"]

    def complete(self, worktree, flow_id, **fields):
        args = ["state", "complete", "--repo", str(worktree), "--flow-id", flow_id,
                "--result", fields.get("result", "completed")]
        result = self.run_af(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_feature_flow_quota_failover_independent_review_and_high_risk_stop(self):
        self.write_config({
            "writer": [{"via": "pi", "provider": "openai", "model": "writer", "company": "OpenAI"},
                       {"via": "codex", "profile": "example-profile", "company": "Anthropic"}],
            "small_review": [{"via": "pi", "provider": "openai", "model": "same-company-review",
                              "company": "OpenAI"},
                             {"via": "codex", "profile": "independent-review", "company": "Anthropic"}],
        }, high_risk_paths=["payments/**"])
        chain = ["grill-with-docs", "to-spec", "to-tickets", "implement-spec", "pr", "retro"]
        worktree, state_path = self.create_flow("e2e-feature", "feature", chain)

        for skill in chain:
            self.assertEqual(self.advance(worktree, "e2e-feature"), skill)
            if skill == "implement-spec":
                env = dict(self.env, FAKE_PI_MODE="quota", FAKE_CODEX_FILE="payments/api.py")
                dispatched = self.run_af("dispatch", "--worktree", str(worktree), "--role", "writer",
                                         "--flow-id", "e2e-feature",
                                         "--prompt", "implement approved ticket",
                                         env=env)
                self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
                self.assertEqual([item["via"] for item in json.loads(dispatched.stdout)["attempts"]],
                                 ["pi", "codex"])
                self.complete(worktree, "e2e-feature", result="implemented")
                continue
            self.complete(worktree, "e2e-feature")

        review = self.run_af("select", "--role", "small_review", "--writer-company", "OpenAI",
                             "--config", str(self.config))
        self.assertEqual(review.returncode, 0, review.stderr)
        review_record = json.loads(review.stdout)
        self.assertEqual(review_record["selected"]["company"], "Anthropic")

        for suite in ("focused", "full"):
            tested = self.run_af("gate", "test", "--repo", str(worktree), "--flow-id", "e2e-feature",
                                 "--suite", suite)
            self.assertEqual(tested.returncode, 0, tested.stderr)
        committed = self.run_af("gate", "commit", "--repo", str(worktree), "--flow-id", "e2e-feature",
                                "--message", "commit high-risk implementation", "--paths", "payments/api.py")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        decision = self.run_af("gate", "decide", "--repo", str(worktree), "--flow-id", "e2e-feature",
                               "--tests", "pass", "--reviews", "pass")
        self.assertEqual(json.loads(decision.stdout)["decision"], "ask")
        self.assertEqual(json.loads(state_path.read_text())["status"], "complete")

    def test_bug_flow_resumes_interrupted_step_and_refuses_resend_after_edit(self):
        self.write_config({"writer": [{"via": "pi", "provider": "openai", "model": "writer",
                                       "company": "OpenAI"},
                                      {"via": "codex", "profile": "example-profile",
                                       "company": "Anthropic"}]})
        chain = ["diagnosing-bugs", "tdd", "code-review", "pr"]
        worktree, state_path = self.create_flow("e2e-bug", "bug", chain)
        self.assertEqual(self.advance(worktree, "e2e-bug"), chain[0])
        data = json.loads(state_path.read_text())
        data["steps"][0]["started"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state_path.write_text(json.dumps(data))
        resumed = self.run_af("state", "resume", "--repo", str(worktree), "--flow-id", "e2e-bug")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["next_step"], chain[0])
        self.assertEqual(self.advance(worktree, "e2e-bug"), chain[0])

        marker = self.base / "codex-called"
        env = dict(self.env, FAKE_PI_MODE="change-quota", FAKE_CODEX_MARKER=str(marker))
        refused = self.run_af("dispatch", "--worktree", str(worktree), "--role", "writer",
                              "--flow-id", "e2e-bug",
                              "--prompt", "diagnose and fix the bug", env=env)
        self.assertEqual(refused.returncode, 76)
        self.assertFalse(marker.exists())
        self.assertTrue((worktree / "changed-after-quota.txt").is_file())
        self.complete(worktree, "e2e-bug", result="resend refused after edit")

        self.assertEqual(self.advance(worktree, "e2e-bug"), chain[1])
        successful = self.run_af("dispatch", "--worktree", str(worktree), "--role", "writer",
                                 "--flow-id", "e2e-bug",
                                 "--prompt", "apply the verified correction")
        self.assertEqual(successful.returncode, 0, successful.stderr)
        self.complete(worktree, "e2e-bug", result="fixed")
        self.assertEqual(self.advance(worktree, "e2e-bug"), chain[2])
        self.complete(worktree, "e2e-bug", result="reviewed")
        self.assertEqual(self.advance(worktree, "e2e-bug"), chain[3])
        self.complete(worktree, "e2e-bug", result="pr ready")
        self.assertEqual(json.loads(state_path.read_text())["status"], "complete")

    def test_small_flow_selects_independent_reviewer_when_available(self):
        self.write_config({
            "writer": [{"via": "codex", "profile": "example-profile", "company": "OpenAI"}],
            "small_review": [{"via": "codex", "profile": "reviewer", "company": "OpenAI"},
                             {"via": "codex", "profile": "independent", "company": "Anthropic"}],
            "big_review": [{"via": "codex", "profile": "independent", "company": "Anthropic"}],
        })
        chain = ["tdd", "code-review", "pr"]
        worktree, state_path = self.create_flow("e2e-small", "small", chain)
        self.assertEqual(self.advance(worktree, "e2e-small"), chain[0])
        dispatched = self.run_af("dispatch", "--worktree", str(worktree), "--role", "writer",
                                 "--flow-id", "e2e-small",
                                 "--prompt", "make one focused change")
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        self.complete(worktree, "e2e-small", result="implemented")
        review = self.run_af("select", "--role", "small_review", "--writer-company", "OpenAI",
                             "--config", str(self.config))
        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertEqual(json.loads(review.stdout)["selected"]["company"], "Anthropic")
        self.assertEqual(json.loads(state_path.read_text())["current_step"], 1)


if __name__ == "__main__":
    unittest.main()

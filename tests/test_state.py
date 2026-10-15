import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class StateCommandTests(unittest.TestCase):
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
        (self.repo / "af.config.json").write_text("{}\n")

    def tearDown(self):
        self.temp.cleanup()

    def run_af(self, *args):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def create_worktree(self, flow_id):
        result = self.run_af("worktree", "create", "--repo", str(self.repo), "--flow-id", flow_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(json.loads(result.stdout)["path"])

    def start(self):
        self.worktree = self.create_worktree("state-001")
        result = self.run_af("state", "start", "--repo", str(self.worktree),
                             "--flow-id", "state-001", "--base-sha", git(self.repo, "rev-parse", "HEAD"),
                             "--chain", "grill-with-docs", "to-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_start_refuses_primary_checkout_without_creating_a_flow_record(self):
        result = self.run_af("state", "start", "--repo", str(self.repo),
                             "--flow-id", "state-001", "--base-sha", git(self.repo, "rev-parse", "HEAD"),
                             "--chain", "tdd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pass the worktree path returned by af worktree create", result.stderr)
        common_dir = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        state_path = common_dir / ".af" / "flows" / "state-001.json"
        self.assertFalse(state_path.exists())

        git(self.repo, "checkout", "-qb", "af/state-001")
        matching_branch = self.run_af("state", "start", "--repo", str(self.repo),
                                      "--flow-id", "state-001", "--base-sha", git(self.repo, "rev-parse", "HEAD"),
                                      "--chain", "tdd")
        self.assertNotEqual(matching_branch.returncode, 0)
        self.assertFalse(state_path.exists())

    def test_record_lives_in_common_git_dir_and_resumes_after_completed_step(self):
        record = self.start()
        state_path = Path(record["state_path"])
        self.assertEqual(json.loads(state_path.read_text())["worktree_path"], str(self.worktree.resolve()))
        status = self.run_af("state", "status", "--repo", self.repo, "--flow-id", "state-001")
        self.assertEqual(json.loads(status.stdout)["flow_id"], "state-001")
        missing_id = self.run_af("state", "status", "--repo", self.repo)
        self.assertNotEqual(missing_id.returncode, 0)
        self.assertTrue(state_path.is_file())
        self.assertEqual(state_path.parent.name, "flows")
        self.assertEqual({path.name for path in state_path.parent.iterdir()}, {"state-001.json"})
        common_dir = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertEqual(state_path.parent.parent, common_dir / ".af")

        first = self.run_af("state", "resume", "--repo", str(self.repo), "--flow-id", "state-001")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["next_step"], "grill-with-docs")

        started = self.run_af("state", "advance", "--repo", str(self.repo), "--flow-id", "state-001")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(json.loads(started.stdout)["step"]["status"], "running")
        second_runner = self.run_af("state", "advance", "--repo", str(self.repo), "--flow-id", "state-001")
        self.assertNotEqual(second_runner.returncode, 0)
        self.assertIn("running", second_runner.stderr)

        completed = self.run_af("state", "complete", "--repo", str(self.repo), "--flow-id", "state-001",
                                "--result", "implemented")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        data = json.loads(state_path.read_text())
        self.assertEqual(data["steps"][0]["result"], "implemented")
        for field in ("model_used", "commit", "tests", "review"):
            self.assertNotIn(field, data["steps"][0])

        resumed = self.run_af("state", "resume", "--repo", str(self.repo), "--flow-id", "state-001")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["next_step"], "to-spec")

    def test_stale_running_step_can_be_resumed_from_last_completed_step(self):
        record = self.start()
        state_path = Path(record["state_path"])
        self.run_af("state", "advance", "--repo", str(self.repo), "--flow-id", "state-001")
        data = json.loads(state_path.read_text())
        data["steps"][0]["started"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state_path.write_text(json.dumps(data))

        resumed = self.run_af("state", "resume", "--repo", str(self.repo), "--flow-id", "state-001")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(resumed.stdout)
        self.assertEqual(status["next_step"], "grill-with-docs")
        updated = json.loads(state_path.read_text())
        self.assertEqual(updated["steps"][0]["status"], "pending")
        self.assertEqual(updated["steps"][0]["result"], "stale runner released")

    def test_stale_after_minutes_comes_from_recorded_config(self):
        record = self.start()
        state_path = Path(record["state_path"])
        (self.repo / "af.config.json").write_text(json.dumps({"stale_after_minutes": 1}))
        self.run_af("state", "advance", "--repo", str(self.repo), "--flow-id", "state-001")
        data = json.loads(state_path.read_text())
        data["steps"][0]["started"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        state_path.write_text(json.dumps(data))

        resumed = self.run_af("state", "resume", "--repo", str(self.repo), "--flow-id", "state-001")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        updated = json.loads(state_path.read_text())
        self.assertEqual(updated["steps"][0]["status"], "pending")

    def test_flow_continues_using_its_recorded_config_after_environment_changes(self):
        record = self.start()
        self.env["AF_CONFIG"] = str(self.base / "missing-config.json")
        result = self.run_af("state", "advance", "--repo", str(self.repo), "--flow-id", "state-001")
        self.assertEqual(result.returncode, 0, result.stderr)
        state_data = json.loads(Path(record["state_path"]).read_text())
        self.assertEqual(state_data["steps"][0]["status"], "running")

    def test_start_records_the_flow_base_commit(self):
        base_sha = git(self.repo, "rev-parse", "HEAD")
        worktree = self.create_worktree("state-base")
        result = self.run_af("state", "start", "--repo", str(worktree), "--flow-id", "state-base",
                             "--base-sha", base_sha,
                             "--chain", "tdd")
        self.assertEqual(result.returncode, 0, result.stderr)
        state_path = Path(json.loads(result.stdout)["state_path"])
        self.assertEqual(json.loads(state_path.read_text())["base_sha"], base_sha)


if __name__ == "__main__":
    unittest.main()

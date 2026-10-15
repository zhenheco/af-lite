import json
import hashlib
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


class CleanupCommandTests(unittest.TestCase):
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
        self.common_dir = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.flows = self.common_dir / ".af" / "flows"
        self.flows.mkdir(parents=True)
        self.config = self.base / "af.config.json"
        self.config.write_text(json.dumps({"record_retention": {
            "archive_after_days": 30, "archive_dir": "archive", "delete_after_days": 180
        }}))

    def tearDown(self):
        self.temp.cleanup()

    def write_record(self, flow_id, age_days):
        created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        path = self.flows / f"{flow_id}.json"
        path.write_text(json.dumps({"version": 1, "flow_id": flow_id, "created_at": created,
                                    "updated_at": created, "status": "complete", "steps": []}))
        return path

    def write_logs(self, flow_id, base):
        folder = base / flow_id
        folder.mkdir(parents=True)
        out = folder / "tdd-1-20261015T000000.000000Z.out.log"
        err = folder / "tdd-1-20261015T000000.000000Z.err.log"
        out.write_text("worker output\n")
        err.write_text("worker error\n")
        return out, err

    def run_af(self, *args):
        return subprocess.run([str(ROOT / "bin" / "af"), *args], cwd=self.repo, env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_archives_after_threshold_and_deletes_after_retention(self):
        archiveable = self.write_record("archive-me", 40)
        expired = self.write_record("delete-me", 181)
        recent = self.write_record("keep-me", 5)
        repo_key = f"project-{hashlib.sha256(str(self.common_dir.resolve()).encode()).hexdigest()[:12]}"
        already_archived = self.common_dir / ".af" / "archive" / repo_key / "delete-archived.json"
        already_archived.parent.mkdir(parents=True)
        expired_at = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        already_archived.write_text(json.dumps({"version": 1, "flow_id": "delete-archived",
                                                "status": "complete", "steps": [],
                                                "created_at": expired_at, "updated_at": expired_at}))
        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        archive = self.common_dir / ".af" / "archive" / repo_key / "archive-me.json"
        self.assertEqual(record["moved"], ["archive-me"])
        self.assertEqual(set(record["deleted"]), {"delete-me", "delete-archived"})
        self.assertTrue(archive.is_file())
        self.assertFalse(archiveable.exists())
        self.assertFalse(expired.exists())
        self.assertFalse(already_archived.exists())
        self.assertTrue(recent.is_file())

    def test_no_archive_directory_keeps_records_until_delete_age(self):
        old_config = self.base / "no-archive.json"
        old_config.write_text(json.dumps({"record_retention": {
            "archive_after_days": 10, "archive_dir": None, "delete_after_days": 90
        }}))
        keep = self.write_record("keep-unarchived", 40)
        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(old_config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["moved"], [])
        self.assertTrue(keep.is_file())

    def test_cleanup_never_archives_or_deletes_an_expired_active_record(self):
        active = self.write_record("expired-active", 200)
        record = json.loads(active.read_text())
        record["status"] = "active"
        active.write_text(json.dumps(record))
        result = self.run_af("cleanup", "--repo", str(self.repo), "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertNotIn("expired-active", report["moved"])
        self.assertNotIn("expired-active", report["deleted"])
        self.assertNotIn("skipped", report)
        self.assertTrue(active.is_file())

    def test_cleanup_archives_and_deletes_flow_logs_with_their_records(self):
        archiveable = self.write_record("archive-with-logs", 40)
        archived_logs = self.write_logs("archive-with-logs", self.common_dir / ".af" / "logs")
        expired = self.write_record("delete-with-logs", 181)
        expired_logs = self.write_logs("delete-with-logs", self.common_dir / ".af" / "logs")

        active = self.write_record("active-with-logs", 181)
        active_logs = self.write_logs("active-with-logs", self.common_dir / ".af" / "logs")
        record = json.loads(active.read_text())
        record["status"] = "active"
        active.write_text(json.dumps(record))

        result = self.run_af("cleanup", "--repo", self.repo, "--config", self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        key = f"project-{hashlib.sha256(str(self.common_dir.resolve()).encode()).hexdigest()[:12]}"
        archive = self.common_dir / ".af" / "archive" / key
        self.assertTrue((archive / "archive-with-logs.json").is_file())
        saved_logs = archive / "archive-with-logs.logs"
        self.assertTrue((saved_logs / archived_logs[0].name).is_file())
        self.assertFalse((self.common_dir / ".af" / "logs" / "archive-with-logs").exists())
        self.assertFalse(expired.exists())
        self.assertFalse((self.common_dir / ".af" / "logs" / "delete-with-logs").exists())
        self.assertTrue(active.exists())
        self.assertTrue((self.common_dir / ".af" / "logs" / "active-with-logs" /
                         active_logs[0].name).is_file())

if __name__ == "__main__":
    unittest.main()

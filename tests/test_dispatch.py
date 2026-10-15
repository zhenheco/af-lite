import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class DispatchCommandTests(unittest.TestCase):
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
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "fixture")
        self.write_exe("pi", self.pi_script())
        self.write_exe("codex", "#!/bin/sh\necho codex-ok\n")
        created = self.run_af("worktree", "create", "--repo", self.repo,
                              "--flow-id", "dispatch-001")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.worktree = Path(json.loads(created.stdout)["path"])
        self.config = self.base / "af.config.json"
        self.write_config()
        started = self.run_af("state", "start", "--repo", self.worktree, "--flow-id", "dispatch-001",
                              "--base-sha", git(self.worktree, "rev-parse", "HEAD"),
                              "--config", self.config, "--chain", "tdd")
        self.assertEqual(started.returncode, 0, started.stderr)

    def tearDown(self):
        self.temp.cleanup()

    def pi_script(self):
        return ("#!/bin/sh\n"
                "case \"${FAKE_PI_MODE:-success}\" in\n"
                "  quota) echo 'rate limit exceeded' >&2; exit 75 ;;\n"
                "  change-quota) echo changed > changed.txt; echo 'quota exceeded' >&2; exit 75 ;;\n"
                "  commit-quota) git commit --allow-empty -qm worker; echo 'quota exceeded' >&2; exit 75 ;;\n"
                "  stderr-quota) echo 'RATE LIMIT EXCEEDED' >&2; exit 1 ;;\n"
                "  prompt-echo) cat >&2; echo 'rate limit exceeded'; exit 1 ;;\n"
                "  timeout) echo touched > timeout.txt; (sleep 0.6; echo late > late.txt) & echo started; sleep 10 ;;\n"
                "  stream) echo early-out; echo early-err >&2; sleep 0.5; echo final-out ;;\n"
                "  *) echo pi-ok ;;\n"
                "esac\n")

    def write_exe(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def write_config(self, roles=None, **extra):
        config = {"roles": roles or {"writer": [
            {"via": "pi", "provider": "openai", "model": "writer", "company": "OpenAI"},
            {"via": "codex", "profile": "example-profile", "company": "Anthropic"}]},
            "quota_stop": {"exit_statuses": [75],
                           "patterns": ["quota exceeded", "rate limit exceeded"]}, **extra}
        self.config.write_text(json.dumps(config))

    def run_af(self, *args, input=None, env=None):
        return subprocess.run([str(ROOT / "bin" / "af"), *map(str, args)], env=env or self.env,
                              input=input, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def dispatch(self, prompt="implement the fixture", env=None):
        return self.run_af("dispatch", "--worktree", self.worktree, "--flow-id", "dispatch-001",
                           "--role", "writer", "--prompt", prompt, env=env)

    def test_quota_failover_runs_next_worker_when_head_and_status_are_unchanged(self):
        self.env["FAKE_PI_MODE"] = "quota"
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual([item["via"] for item in record["attempts"]], ["pi", "codex"])
        self.assertEqual(Path(record["attempts"][-1]["log_paths"]["stdout"]).read_text(), "codex-ok\n")

    def test_quota_after_worktree_change_refuses_failover(self):
        self.env["FAKE_PI_MODE"] = "change-quota"
        result = self.dispatch()
        self.assertEqual(result.returncode, 76)
        record = json.loads(result.stdout)
        self.assertEqual(record["status"], "blocked")
        self.assertTrue(record["worktree_changed"])
        self.assertIn("quota stop", record["message"])
        self.assertTrue((self.worktree / "changed.txt").is_file())

    def test_worker_commit_is_soft_reset_and_reported_without_failover(self):
        self.env["FAKE_PI_MODE"] = "commit-quota"
        before = git(self.worktree, "rev-parse", "HEAD")
        result = self.dispatch()
        self.assertEqual(result.returncode, 76)
        record = json.loads(result.stdout)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(len(record["worker_commits_normalized"]), 1)
        self.assertEqual([item["via"] for item in record["attempts"]], ["pi"])

    def test_timeout_kills_worker_process_group(self):
        self.write_config(worker_timeout_minutes=0.005)
        self.env["FAKE_PI_MODE"] = "timeout"
        result = self.dispatch()
        self.assertEqual(result.returncode, 124)
        record = json.loads(result.stdout)
        self.assertTrue(record["timed_out"])
        self.assertTrue(record["worktree_changed"])
        self.assertEqual(Path(record["attempts"][0]["log_paths"]["stdout"]).read_text(), "started\n")
        time.sleep(0.35)
        self.assertFalse((self.worktree / "late.txt").exists())

    def test_codex_receives_exact_writer_prompt_on_stdin(self):
        prompt_file = self.base / "codex-prompt"
        args_file = self.base / "codex-args"
        self.write_exe("codex", "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$FAKE_CODEX_ARGS\"\n"
                       "cat > \"$FAKE_CODEX_PROMPT\"\necho codex-ok\n")
        self.write_config({"writer": [{"via": "codex", "profile": "example-profile",
                                        "company": "OpenAI"}]})
        self.env["FAKE_CODEX_ARGS"] = str(args_file)
        self.env["FAKE_CODEX_PROMPT"] = str(prompt_file)
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args_file.read_text().strip(),
                         f"exec -p example-profile -s workspace-write -C {self.worktree} -")
        self.assertEqual(prompt_file.read_text(),
                         "ROLE: You are the WRITER, started non-interactively for this worktree. "
                         "Implement directly here; do not dispatch another writer; do not ask the user; "
                         "leave changes uncommitted; never push.\n\nimplement the fixture")

    def test_review_role_receives_read_only_preamble(self):
        prompt_file = self.base / "review-prompt"
        self.write_exe("codex", "#!/bin/sh\ncat > \"$FAKE_REVIEW_PROMPT\"\necho reviewed\n")
        self.write_config({"small_review": [{"via": "codex", "profile": "reviewer",
                                             "company": "OpenAI"}]})
        self.env["FAKE_REVIEW_PROMPT"] = str(prompt_file)
        result = self.run_af("dispatch", "--worktree", self.worktree, "--flow-id", "dispatch-001",
                             "--role", "small_review", "--writer-company", "Anthropic",
                             "--prompt", "review these changes")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = prompt_file.read_text()
        self.assertTrue(prompt.startswith("ROLE: You are the REVIEWER, started non-interactively"))
        self.assertIn("do not edit files; do not commit; do not push", prompt)
        self.assertTrue(prompt.endswith("review these changes"))

    def test_host_candidate_returns_fresh_subagent_instructions_without_running_a_cli(self):
        candidate = {"via": "host", "model": "claude-sonnet", "company": "Anthropic"}
        self.write_config({"small_review": [candidate]})
        result = self.run_af("dispatch", "--worktree", self.worktree, "--flow-id", "dispatch-001",
                             "--role", "small_review", "--writer-company", "OpenAI",
                             "--prompt", "review these changes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "via": "host", "role": "small_review", "model": "claude-sonnet",
            "company": "Anthropic", "worktree": str(self.worktree),
            "prompt": "ROLE: You are the REVIEWER, started non-interactively for this worktree. "
                      "Review the requested changes only; do not edit files; do not commit; do not push; "
                      "do not ask the user.\n\nreview these changes",
            "instructions": "start a fresh subagent with this model in this worktree and give it the prompt",
            "attempts": []})
        common = Path(git(self.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertFalse((common / ".af" / "logs" / "dispatch-001").exists())

    def test_quota_failover_can_select_a_host_candidate(self):
        self.env["FAKE_PI_MODE"] = "quota"
        self.write_config({"writer": [
            {"via": "pi", "provider": "openai", "model": "writer", "company": "OpenAI"},
            {"via": "host", "model": "claude-sonnet", "company": "Anthropic"}]})
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["via"], "host")
        self.assertEqual(record["model"], "claude-sonnet")
        self.assertEqual([item["via"] for item in record["attempts"]], ["pi"])
        self.assertTrue(record["prompt"].startswith("ROLE: You are the WRITER"))

    def test_quota_regex_checks_stderr_without_prompt_or_stdout(self):
        self.env["FAKE_PI_MODE"] = "prompt-echo"
        result = self.dispatch("rate limit exceeded")
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(json.loads(result.stdout).get("status"), "waiting_for_quota")

        self.env["FAKE_PI_MODE"] = "stderr-quota"
        self.write_config(quota_stop={"exit_statuses": [], "patterns": ["rate limit exceeded"]})
        retried = self.dispatch()
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual([item["via"] for item in json.loads(retried.stdout)["attempts"]], ["pi", "codex"])

    def test_unconfigured_quota_patterns_do_not_trigger_failover(self):
        self.env["FAKE_PI_MODE"] = "stderr-quota"
        self.write_config(quota_stop={"exit_statuses": []})
        result = self.dispatch()
        self.assertEqual(result.returncode, 1)
        self.assertEqual([item["via"] for item in json.loads(result.stdout)["attempts"]], ["pi"])

    def test_worker_output_streams_into_flow_logs(self):
        self.env["FAKE_PI_MODE"] = "stream"
        command = [str(ROOT / "bin" / "af"), "dispatch", "--worktree", str(self.worktree),
                   "--flow-id", "dispatch-001", "--role", "writer", "--prompt", "implement the fixture"]
        process = subprocess.Popen(command, env=self.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        log_dir = common / ".af" / "logs" / "dispatch-001"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            matches = list(log_dir.glob("*.out.log")) if log_dir.is_dir() else []
            if matches and "early-out" in matches[0].read_text():
                break
            time.sleep(0.01)
        self.assertTrue(matches)
        output_path = matches[0]
        error_path = Path(str(output_path).replace(".out.log", ".err.log"))
        self.assertIn("early-err", error_path.read_text())
        self.assertIsNone(process.poll())
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, stderr)
        record = json.loads(stdout)
        self.assertNotIn("stdout", record)
        self.assertNotIn("stderr", record)
        self.assertIn("early-out", output_path.read_text())
        self.assertIn("early-err", error_path.read_text())
        self.assertEqual(record["attempts"][0]["log_paths"],
                         {"stdout": str(output_path), "stderr": str(error_path)})

    def test_missing_cli_is_skipped_in_configured_order(self):
        (self.bin / "pi").rename(self.bin / "pi-disabled")
        environment = dict(self.env, PATH=f"{self.bin}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
        self.assertIsNone(shutil.which("pi", path=environment["PATH"]))
        result = self.dispatch(env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item["via"] for item in json.loads(result.stdout)["attempts"]], ["codex"])

    def test_pi_prompt_on_stdin_and_flow_log(self):
        prompt_file = self.base / "pi-prompt"
        self.write_exe("pi", "#!/bin/sh\ncat > \"$FAKE_PI_PROMPT\"\necho pi-ok\n")
        self.env["FAKE_PI_PROMPT"] = str(prompt_file)
        result = self.dispatch("task prompt")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(Path(record["attempts"][0]["log_paths"]["stdout"]).read_text(), "pi-ok\n")
        self.assertIn("task prompt", prompt_file.read_text())

    def test_dispatch_requires_a_flow_id(self):
        (self.repo / "af.config.json").write_text(self.config.read_text())
        result = self.run_af("dispatch", "--worktree", self.repo, "--role", "writer",
                             "--prompt", "task")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

"""Sanitized result-envelope shapes and offline engine boundary tests."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from lightkeeper.engines.base import (Answer, AnswerError, AuthError, Diagnostics, InputError,
                                            QuotaError, TransportError)
from lightkeeper.engines.claude import ClaudeEngine, parse_envelope, reset_time
from lightkeeper.policy import load_policy
from lightkeeper.reviewer import ClaudeReviewer, Reviewer, ReviewerError, UnclearAnswer, PR_FRAME, system_prompt
from tests.helpers import make_pr, RULES

FIXTURES = json.loads((Path(__file__).parent / "fixtures/engines/claude.json").read_text())


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        # Credential-source tests must not depend on the developer's shell credentials.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_recorded_envelopes(self):
        self.assertEqual(parse_envelope(json.dumps(FIXTURES["success"]))["action"], "approve")
        for name, error in (("auth", AuthError), ("quota_reset", QuotaError), ("quota_unknown", QuotaError),
                            ("input", InputError), ("input_text", InputError), ("transport", TransportError)):
            with self.subTest(name=name), self.assertRaises(error) as caught:
                parse_envelope(json.dumps(FIXTURES[name]))
            self.assertEqual(caught.exception.diagnostics.stdout, json.dumps(FIXTURES[name]))
            if name == "quota_reset":
                self.assertEqual(caught.exception.reset_at, datetime.fromisoformat("2026-10-14T15:00:00+00:00").timestamp())
            elif name == "quota_unknown":
                self.assertIsNone(caught.exception.reset_at)

    def test_malformed_or_missing_answers(self):
        for value in ("garbage", "{broken", "[]", '{"result":"approve"}'):
            with self.subTest(value=value), self.assertRaises(AnswerError):
                parse_envelope(value)

    def test_fields_and_auth_quota_precede_input_strings(self):
        for record, expected in (({"subtype": "error_max_turns"}, InputError),
                                 ({"terminal_reason": "rate_limit"}, QuotaError),
                                 ({"is_error": True, "result": "not logged in; budget exhausted"}, AuthError),
                                 ({"is_error": True, "result": "quota budget exhausted"}, QuotaError)):
            with self.subTest(record=record), self.assertRaises(expected):
                parse_envelope(json.dumps(record))

    def test_reset_hints(self):
        now = datetime.fromisoformat("2026-10-13T14:00:00+00:00").timestamp()
        self.assertEqual(reset_time("resets 3pm (UTC)", now), now + 3600)
        self.assertEqual(reset_time("resets 1pm (UTC)", now), now + 23 * 3600)
        self.assertEqual(reset_time("resets 2026-10-13T15:00:00+01:00", now), now)
        for hint in ("resets later", "resets 3pm", "resets 13pm (UTC)", "resets 3pm (Unknown/Zone)"):
            self.assertIsNone(reset_time(hint, now))

    def test_nonzero_envelope_wins_over_stderr(self):
        proc = subprocess.CompletedProcess([], 1, json.dumps(FIXTURES["auth"]), "budget")
        engine = ClaudeEngine("opus", "test profile", runner=Mock(return_value=proc))
        with self.assertRaises(AuthError) as caught:
            engine.ask("system", "request", {}, 10, Path("."))
        self.assertEqual(caught.exception.fix_command, "CLAUDE_CONFIG_DIR='test profile' claude auth login")
        self.assertEqual(caught.exception.diagnostics.exit_code, 1)

    def test_plain_stderr_auth_and_quota(self):
        for detail, expected in (("not logged in", AuthError), ("usage limit exceeded", QuotaError),
                                 ("connection refused", TransportError), ("budget unavailable", TransportError)):
            runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", detail))
            with self.subTest(detail=detail), self.assertRaises(expected):
                ClaudeEngine("opus", runner=runner).ask("system", "prompt", {}, 10, Path("."))

    def test_invalid_key_and_token_auth_errors_and_credential_specific_fixes(self):
        for detail in ("INVALID API KEY", "Invalid Auth Token", "Fix External API Key", "fix external auth token"):
            for api_key in (None, "test-key"):
                for envelope in (False, True):
                    with self.subTest(detail=detail, api_key=bool(api_key), envelope=envelope), patch.dict(os.environ):
                        if api_key is not None:
                            os.environ["ANTHROPIC_API_KEY"] = api_key
                        stdout = json.dumps({"is_error": True, "result": detail}) if envelope else ""
                        runner = Mock(return_value=subprocess.CompletedProcess([], 1, stdout, detail))
                        engine = ClaudeEngine("opus", "/tmp/test-profile", runner=runner)
                        with self.assertRaises(AuthError) as caught:
                            engine.ask("system", "prompt", {}, 10, Path("."))
                        expected = ("unset ANTHROPIC_API_KEY or replace it" if api_key else
                                    "CLAUDE_CONFIG_DIR=/tmp/test-profile claude auth login")
                        self.assertEqual(caught.exception.fix_command, expected)
                        self.assertEqual(runner.call_args.kwargs["env"].get("ANTHROPIC_API_KEY"), api_key)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def probe(self, stdout, code=0):
        return ClaudeEngine("opus", runner=Mock(return_value=subprocess.CompletedProcess([], code, stdout, ""))).probe()

    def test_identity_shapes(self):
        for fields, account in (({"email": "reviewer@example.invalid"}, "reviewer@example.invalid"),
                                ({"account": {"id": "test-account"}}, "test-account"), ({}, None)):
            identity = self.probe(json.dumps({"loggedIn": True, **fields}))
            self.assertTrue(identity.ok)
            self.assertEqual(identity.account, account)
        self.assertFalse(self.probe('{"loggedIn":false}').ok)
        self.assertFalse(self.probe('{"loggedIn":true}', 1).ok)

    def test_malformed_and_timeout_never_raise(self):
        for stdout in ("bad", "[]", "{}", '{"loggedIn":"false"}'):
            identity = self.probe(stdout)
            self.assertFalse(identity.ok)
            self.assertIn("failed", identity.detail)
            self.assertEqual(identity.fix_command, "claude auth login")
        for exc in (subprocess.TimeoutExpired("claude", 20), FileNotFoundError()):
            self.assertFalse(ClaudeEngine("opus", runner=Mock(side_effect=exc)).probe().ok)

    def test_ask_and_probe_share_scrubbed_environment(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(FIXTURES["success"]), ""),
                                  subprocess.CompletedProcess([], 0, '{"loggedIn":true}', "")])
        engine = ClaudeEngine("opus", "test-profile", runner=runner)
        with patch.dict(os.environ, {"CLAUDECODE": "1", "SECRET_SENTINEL": "never", "USER": "test-user"}):
            answer = engine.ask("private-system", "stdin-sentinel", {}, 42, Path("."))
            engine.probe()
        ask, probe = runner.call_args_list
        self.assertEqual(ask.kwargs["env"], probe.kwargs["env"])
        self.assertEqual(ask.kwargs["env"]["CLAUDE_CONFIG_DIR"], "test-profile")
        self.assertNotIn("CLAUDECODE", ask.kwargs["env"])
        self.assertNotIn("SECRET_SENTINEL", ask.kwargs["env"])
        self.assertEqual(ask.kwargs["input"], "stdin-sentinel")
        self.assertNotIn("stdin-sentinel", ask.args[0])
        self.assertNotIn("private-system", answer.diagnostics.command_without_secrets)
        self.assertEqual(probe.args[0], ["claude", "auth", "status"])
        self.assertEqual((probe.kwargs["timeout"], probe.kwargs["stdin"]), (20, subprocess.DEVNULL))

    def test_probe_and_review_use_empty_temporary_working_directories(self):
        directories = []
        def run(command, **kwargs):
            cwd = Path(kwargs["cwd"])
            self.assertTrue(cwd.is_absolute())
            self.assertTrue(cwd.is_dir())
            if command[1:3] == ["auth", "status"]:
                self.assertEqual(list(cwd.iterdir()), [])
            else:
                system_file = Path(command[command.index("--append-system-prompt-file") + 1])
                self.assertEqual(list(cwd.iterdir()), [system_file])
                self.assertEqual(system_file.stat().st_mode & 0o777, 0o600)
                self.assertIn(RULES, system_file.read_text(encoding="utf-8"))
                self.assertNotIn(RULES, " ".join(command))
            directories.append(cwd)
            stdout = '{"loggedIn":true}' if command[1:3] == ["auth", "status"] else json.dumps(FIXTURES["success"])
            return subprocess.CompletedProcess(command, 0, stdout, "")
        engine = ClaudeEngine("opus", runner=run)
        Reviewer(engine, RULES).review_pr(make_pr())
        self.assertTrue(engine.probe().ok)
        self.assertEqual(len(directories), 2)
        self.assertNotEqual(directories[0], directories[1])
        self.assertTrue(all(not cwd.exists() for cwd in directories))

    def test_system_file_is_private_and_removed_after_success_or_failure(self):
        for error in (None, FileNotFoundError(), subprocess.TimeoutExpired("claude", 10)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def run(command, **kwargs):
                    system_file = Path(command[command.index("--append-system-prompt-file") + 1])
                    self.assertEqual(system_file.parent, root)
                    self.assertEqual(system_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(system_file.read_text(encoding="utf-8"), "private rules café")
                    self.assertNotIn("private rules", " ".join(command))
                    if error:
                        raise error
                    return subprocess.CompletedProcess(command, 0, json.dumps(FIXTURES["success"]), "")
                engine = ClaudeEngine("opus", runner=run)
                if error:
                    with self.assertRaises(TransportError):
                        engine.ask("private rules café", "request", {}, 10, root)
                else:
                    engine.ask("private rules café", "request", {}, 10, root)
                self.assertEqual(list(root.iterdir()), [])

    def test_policy_normalizes_profile_before_ask_and_probe_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "RULES.md").write_text(RULES)
            for profile in ("profiles/reviewer", "~/profiles/reviewer"):
                with self.subTest(profile=profile), patch.dict(os.environ, {"HOME": str(root)}):
                    path = root / "policy.toml"
                    path.write_text(f'login = "test-owner"\nrepos = ["org/repo"]\nclaude_config_dir = "{profile}"\n')
                    policy = load_policy(path)
                    expected = str((root / "profiles/reviewer").resolve())
                    self.assertEqual(policy.claude_config_dir, expected)
                    runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(FIXTURES["success"]), ""),
                                              subprocess.CompletedProcess([], 0, '{"loggedIn":true}', "")])
                    engine = ClaudeEngine("opus", policy.claude_config_dir, runner=runner)
                    Reviewer(engine, RULES).review_pr(make_pr())
                    engine.probe()
                    for call in runner.call_args_list:
                        self.assertEqual(call.kwargs["env"]["CLAUDE_CONFIG_DIR"], expected)


class SessionTests(unittest.TestCase):
    def test_generic_engine_and_legacy_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(FIXTURES["success"]), "stderr"))
            reviewer = ClaudeReviewer("opus", RULES, effort="high", sessions_dir=root, runner=runner)
            reviewer.review_pr(make_pr())
            session = next(root.iterdir())
            command = runner.call_args.args[0]
            meta = json.loads((session / "meta.json").read_text())
            self.assertEqual(meta, {"model": "opus", "effort": "high", "exit": 0,
                                   "bytes": len(runner.call_args.kwargs["input"].encode()),
                                   "command": [a for a in command if not a.startswith("You are")]})
            self.assertEqual((session / "prompt.md").read_text(),
                             system_prompt(PR_FRAME.replace("{remarks}", "blockers and important"), RULES) + "\n\n" + runner.call_args.kwargs["input"])
            self.assertEqual((session / "stdout.json").read_text(), json.dumps(FIXTURES["success"]))
            engine = Mock(label="other/model")
            engine.ask.return_value = Answer(FIXTURES["success"]["structured_output"], Diagnostics("raw", "", 0, ["other"]))
            self.assertEqual(Reviewer(engine, RULES).review_pr(make_pr()).outcome, "approve")

    def test_error_artifacts_and_typed_cause(self):
        for fixture, expected in (("auth", ReviewerError), ("input", UnclearAnswer), ("garbage", UnclearAnswer)):
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as directory:
                stdout = json.dumps(FIXTURES[fixture]) if fixture in FIXTURES else fixture
                proc = subprocess.CompletedProcess([], 0, stdout, "stderr")
                with self.assertRaises(expected) as caught:
                    ClaudeReviewer("opus", RULES, sessions_dir=Path(directory), runner=Mock(return_value=proc)).review_pr(make_pr())
                session = next(Path(directory).iterdir())
                self.assertEqual({p.name for p in session.iterdir()}, {"prompt.md", "stdout.json", "stderr.txt", "meta.json"})
                if expected is UnclearAnswer:
                    self.assertEqual(caught.exception.session, session)
                else:
                    self.assertIsInstance(caught.exception.cause, AuthError)

    def test_timeout_and_missing_cli_artifacts(self):
        for exc, files in ((subprocess.TimeoutExpired("claude", 10, output=b"partial\xe2", stderr=b"error"),
                            {"prompt.md", "stdout.json", "stderr.txt"}), (FileNotFoundError(), {"prompt.md"})):
            with self.subTest(exc=exc), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ReviewerError) as caught:
                    ClaudeReviewer("opus", RULES, sessions_dir=Path(directory), runner=Mock(side_effect=exc)).review_pr(make_pr())
                self.assertIsInstance(caught.exception.cause, TransportError)
                session = next(Path(directory).iterdir())
                self.assertEqual({p.name for p in session.iterdir()}, files)
                if isinstance(exc, subprocess.TimeoutExpired):
                    self.assertEqual((session / "stdout.json").read_bytes(), b"partial\xe2")

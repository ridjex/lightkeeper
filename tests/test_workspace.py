"""Offline material, stream, and Runner boundary tests."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from lightkeeper.__main__ import Runner
from lightkeeper.engines.base import AnswerError, InputError, TransportError
from lightkeeper.engines.claude import ClaudeEngine
from lightkeeper.github import Gh, GhError, GhTimeout
from lightkeeper.journal import Journal
from lightkeeper.models import Check, Comment, Decision, Review, marker_line
from lightkeeper.reviewer import Reviewer, UnclearAnswer, build_deploy_prompt, build_pr_prompt
from lightkeeper.settings import SettingsError, load_settings
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from lightkeeper.workspace import INLINE_MAX_BYTES, Workspace, escape
from tests.helpers import (APPLY_JOB, FakeGh, FakeReviewer, HEAD, PENDING_PROD, PLAN_JOB,
                           RULES, make_file, make_pr, make_run, make_settings)
from tests.test_reviewer import ANSWER_V2


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


def result(**fields):
    return {"type": "result", "structured_output": ANSWER_V2, "num_turns": 3, "total_cost_usd": .25, **fields}


def call(name="Read", value="description.md", identifier="read-1"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": identifier, "name": name,
         "input": {"file_path": value} if name == "Read" else {"path": value}}]}}


def reply(identifier="read-1", error=None):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": identifier, "is_error": error}]}}


class WorkspaceTests(unittest.TestCase):
    def test_generated_names_permissions_and_complete_material(self):
        paths = ("a\nb\tc", "../outside", ".claude/settings.json", "CLAUDE.md")
        files = tuple(make_file(path, previous_path="old\t\npath") for path in paths) + (make_file("binary", patch=None),)
        comments = tuple(Comment("author", "full " * 1200 + str(i)) for i in range(20))
        review = Review("reviewer", "COMMENTED", HEAD, "review " * 1200)
        check = Check("ci", "completed", "failure")
        log = "\n".join(f"line {i}" for i in range(200))
        material = Workspace()
        prompt = build_pr_prompt(make_pr(files=files, comments=comments, reviews=(review,), changed_files=8),
                                 [(check, log)], workspace=material, tree=(["tests/keep.py", "a\nb"], True))
        self.assertIn("file list incomplete: 5 of 8", prompt)
        self.assertIn("line 0", material.files["logs/01.log"])
        self.assertIn("80 earlier lines omitted", prompt)
        self.assertEqual(material.files["logs/01.log"], log)
        self.assertIn(comments[0].body, material.files["comments.md"])
        self.assertIn(comments[-1].body, material.files["comments.md"])
        self.assertIn(review.body, material.files["reviews.md"])
        self.assertTrue(material.files["tree.txt"].startswith("(listing truncated by GitHub)\n"))
        self.assertIn("a\\nb", material.files["tree.txt"])
        self.assertEqual(material.files["diff/0005.patch"].splitlines()[1], "(patch omitted by GitHub: binary or too large)")
        for index, path in enumerate(paths, 1):
            self.assertTrue(material.files[f"diff/{index:04d}.patch"].startswith(f"path: {escape(path)}\n"))
            self.assertIn(escape(path), material.files["files.tsv"])
        self.assertTrue(all(len(row.split("\t")) == 8 for row in material.files["files.tsv"].splitlines()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material.write(root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}, set(material.files))
            for path in root.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
            self.assertFalse((root / ".claude").exists())
            self.assertFalse((root / "CLAUDE.md").exists())

    def test_whole_section_boundary_counts_utf8_and_later_small_sections(self):
        material = Workspace({"one.md": "é" * 50, "two.md": "small"})
        sections = [("One", "one.md", "é" * 50), ("Two", "two.md", "small")]
        whole = material.prompt("# Request", sections, 900)
        # Account for the later section's pointer while the first section is considered.
        boundary = len(whole.replace("## Two\nsmall", "## Two\n(in two.md: 5 bytes)").encode("utf-8"))
        with patch("lightkeeper.workspace.INLINE_MAX_BYTES", boundary):
            self.assertEqual(material.prompt("# Request", sections, 900), whole)
        with patch("lightkeeper.workspace.INLINE_MAX_BYTES", boundary - 1):
            prompt = material.prompt("# Request", sections, 900)
            self.assertIn("(in one.md: 100 bytes)", prompt)
            self.assertIn("## Two\nsmall", prompt)
            self.assertNotIn("é", prompt)
            self.assertLessEqual(len(prompt.encode("utf-8")), boundary - 1)

    def test_giant_title_description_and_diff_never_skip_or_clip(self):
        material = Workspace()
        pr = make_pr(title="é" * 300_000, body="body " * 100_000, files=(make_file(patch="patch " * 100_000),))
        prompt = build_pr_prompt(pr, workspace=material)
        self.assertLessEqual(len(prompt.encode("utf-8")), INLINE_MAX_BYTES)
        self.assertIn(pr.title, material.files["request.md"])
        self.assertEqual(material.files["description.md"], pr.body)
        self.assertTrue(material.files["diff/0001.patch"].endswith(pr.files[0].patch))
        self.assertIn("(in description.md:", prompt)
        self.assertIn("(in diff/:", prompt)

    def test_deployment_logs_jobs_and_comparison(self):
        material = Workspace()
        log = "start\n" + "noise\n" * 40_000 + "finish"
        run = make_run(base_sha="a" * 40, files=(make_file("../bad\t\n"),))
        prompt = build_deploy_prompt(run, "production", [PLAN_JOB, APPLY_JOB], {1: log},
                                     workflow_text="jobs: {}", workspace=material)
        self.assertEqual(material.files["workflow.yml"], "jobs: {}")
        self.assertEqual(material.files["logs/01.log"], log)
        self.assertIn(f"1\tplan\tcompleted\tsuccess\t{len(log)}\tlogs/01.log", material.files["jobs.tsv"])
        self.assertIn("compare/files.tsv", material.files)
        self.assertIn("compare/diff/0001.patch", material.files)
        self.assertIn("earlier lines omitted", prompt)
        self.assertNotIn("start\n", prompt)

    def test_budget_defaults_validation_and_judgement_exclusion(self):
        settings = make_settings()
        self.assertEqual((settings.agent.timeout_s, settings.agent.budget_usd), (900, 5.0))
        changed = replace(settings, agent=replace(settings.agent, timeout_s=10, budget_usd=.1))
        self.assertEqual(settings.judgement("org/repo"), changed.judgement("org/repo"))
        file = settings.config_dir / "settings.toml"
        text = file.read_text()
        for value in ("0", "-1", "true", '"5"', "nan", "inf", "-inf"):
            with self.subTest(value=value):
                file.write_text(text.replace("budget_usd = 5.0", f"budget_usd = {value}"))
                with self.assertRaisesRegex(SettingsError, "agent.budget_usd"):
                    load_settings(settings.config_dir)
        file.write_text(text.replace("budget_usd = 5.0", "budget_usd = 2"))
        self.assertEqual(load_settings(settings.config_dir).agent.budget_usd, 2.0)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        Workspace({"description.md": "hello", "diff/0001.patch": "patch"}).write(self.root)

    def ask(self, stdout, error=None):
        runner = Mock(side_effect=error) if error else Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ""))
        return ClaudeEngine("opus", runner=runner).ask("rules", "overview", {}, 900, self.root)

    def test_only_successful_distinct_workspace_reads_are_opened(self):
        stdout = stream({"type": "system", "subtype": "init", "tools": ["Read", "Grep", "Glob"]},
                        call(value=str(self.root / "description.md")), reply(),
                        call(identifier="repeat"), reply("repeat"),
                        call(value="diff/0001.patch", identifier="bad"), reply("bad", True),
                        call("Grep", identifier="grep"), reply("grep"),
                        call(value="../outside", identifier="outside"), reply("outside"),
                        result(), {"type": "rate_limit_event"})
        answer = self.ask(stdout)
        self.assertEqual(answer.trace.opened, ("description.md",))
        self.assertEqual((answer.trace.turns, answer.trace.cost_usd), (3, .25))
        self.assertEqual(answer.diagnostics.stdout, stdout)

    def test_denials_from_system_and_result_are_deduplicated_and_escaped(self):
        denied = {"tool_name": "Read", "tool_input": {"file_path": "../outside\npath"}, "tool_use_id": "read-1"}
        stdout = stream(call(value="../outside\npath"), reply(error=True),
                        {"type": "system", "subtype": "permission_denied", **denied},
                        result(permission_denials=[denied, {"tool_name": "Glob", "tool_input": {"path": "../"}}]))
        trace = self.ask(stdout).trace
        self.assertEqual(trace.opened, ())
        self.assertEqual(len(trace.denied), 2)
        self.assertIn("outside\\npath", trace.denied[0])
        self.assertNotIn("\n", trace.denied[0])
        self.assertTrue(trace.denied[1].startswith("Glob "))

    def test_last_result_is_parsed_and_missing_result_is_error(self):
        self.assertEqual(self.ask(stream(result(), result(structured_output={"last": True}))).parsed, {"last": True})
        for stdout in ("", stream(call(), reply()), '{"structured_output":{}}', '{"type":"result"'):
            with self.subTest(stdout=stdout), self.assertRaises(AnswerError):
                self.ask(stdout)

    def test_a_cli_that_dies_mid_stream_is_retried_not_settled(self):
        died = subprocess.CompletedProcess([], 137, stream({"type": "system", "subtype": "init"}, call(), reply()), "Killed")
        with self.assertRaisesRegex(TransportError, "exited 137 without a result"):
            ClaudeEngine("opus", runner=Mock(return_value=died)).ask("rules", "overview", {}, 900, self.root)

    def test_empty_blob_is_not_called_omitted(self):
        material = Workspace()
        material.changes((make_file("pkg/__init__.py", 0, 0, status="added", patch=None,
                                    sha="e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"),))
        self.assertEqual(material.files["diff/0001.patch"].splitlines()[1], "(no patch: removed file, empty file or pure rename)")

    def test_bounds_report_opened_and_offered(self):
        for bound, expected in (("max_turns", "60-turn"), ("budget", "$5 budget")):
            stdout = stream(call(), reply(), result(subtype="error_" + bound, is_error=True,
                                                   terminal_reason=bound, structured_output=None))
            with self.subTest(bound=bound), self.assertRaisesRegex(InputError, expected.replace("$", r"\$")) as error:
                self.ask(stdout)
            self.assertIn("after opening 1 of 2 files", str(error.exception))
            self.assertEqual(error.exception.trace.opened, ("description.md",))

    def test_timeout_after_any_tool_is_input_error_without_tool_is_transport(self):
        for events, expected in (([call(), reply()], InputError), ([call("Glob")], InputError),
                                  ([{"type": "system", "subtype": "init"}], TransportError)):
            partial = (stream(*events) + '\n{"type":').encode()
            with self.subTest(events=events), self.assertRaises(expected) as error:
                self.ask("", subprocess.TimeoutExpired("fake", 900, output=partial, stderr=b"error"))
            self.assertEqual(error.exception.diagnostics.stdout, partial)
            if expected is InputError:
                self.assertIn("900-second time limit", str(error.exception))


class RunnerWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = make_settings()
        self.gh = FakeGh()
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.run = make_run()
        self.gh.runs = [self.run]
        self.gh.pending[("org/repo", 100)] = [PENDING_PROD]
        self.gh.jobs_by_attempt[("org/repo", 100, 1)] = [PLAN_JOB, APPLY_JOB]
        self.journal = Journal(Path(self.temp.name) / "journal.jsonl")
        self.notified = []
        self.observer = Observer(Projection(), Runtime(repos=("org/repo",)), mono=lambda: 0, wall=lambda: 1_000_000)

    def runner(self, reviewer):
        self.journal.observer = self.observer.feed
        return Runner(self.settings, self.gh, self.journal, reviewer, live_lock_held=True,
                      notifier=lambda _, message: self.notified.append(message), observer=self.observer)

    def records(self):
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()]

    def test_denied_approval_is_refused_journaled_and_alerted_for_both_kinds(self):
        data = {"opened": 1, "offered": 8, "denied": 1, "denied_calls": ['Read {"file_path": "../outside"}'],
                "turns": 4, "cost_usd": .5}
        reviewer = FakeReviewer([Decision("approve", "fine", "ai", data=data)] * 2)
        runner = self.runner(reviewer)
        self.assertEqual(runner.process_pr("org/repo", 7), "nothing")
        self.assertEqual(runner.process_run("org/repo", 100), "ignore")
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(len(self.notified), 2)
        self.assertTrue(all("a denied file-tool call (outside the workspace): Read" in message for message in self.notified))
        assessments = [rec for rec in self.records() if rec["event"] == "assessment"]
        self.assertEqual(len(assessments), 2)
        for rec in assessments:
            for name, value in data.items():
                self.assertEqual(rec[name], value)
        self.assertEqual(len(self.observer.snapshot.alerts), 2)
        detail = self.observer.snapshot.details[("org/repo", 7, HEAD)]
        self.assertEqual((detail.opened, detail.offered), (1, 8))
        self.assertEqual(runner.process_pr("org/repo", 7), "final")
        self.assertEqual(runner.process_run("org/repo", 100), "final")

    def test_denied_trace_does_not_block_other_actions_or_enter_public_comment(self):
        decision = Decision("comment", "look", "ai", why="Not approving yet: tests need work.",
                            data={"denied_calls": ['Read {"file_path": "../private"}'], "denied": 1})
        runner = self.runner(FakeReviewer([decision]))
        self.assertEqual(runner.process_pr("org/repo", 7), "applied")
        body = self.gh.writes[0][-1]
        self.assertNotIn("private", body)
        self.assertNotIn("opened", body)

    def test_incomplete_list_reaches_model_then_floor_and_notifies(self):
        pr = make_pr(changed_files=3)
        self.gh.pr_snapshots[("org/repo", 7)] = [pr]
        reviewer = FakeReviewer([Decision("approve", "fine", "ai")])
        runner = self.runner(reviewer)
        self.assertEqual(runner.process_pr("org/repo", 7), "nothing")
        self.assertEqual(reviewer.last_prs, [pr])
        self.assertIn("file list incomplete: 1 of 3", self.notified[0])
        self.assertFalse(self.gh.writes)

    def test_new_incomplete_count_before_post_defers_without_write(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(changed_files=1), make_pr(changed_files=3)]
        runner = self.runner(FakeReviewer([Decision("approve", "fine", "ai")]))
        self.assertEqual(runner.process_pr("org/repo", 7), "defer")
        self.assertFalse(self.gh.writes)

    def test_tree_errors_follow_comparison_policy(self):
        for exc, transient in ((GhTimeout(["api"], 60), False), (GhError(["api"], 1, "HTTP 404"), False),
                               (GhError(["api"], 1, "HTTP 422"), False), (GhError(["api"], 1, "HTTP 500"), True),
                               (GhError(["api"], 1, "HTTP 403 secondary rate limit"), True),
                               (GhError(["api"], 1, "HTTP 429"), True)):
            with self.subTest(exc=exc):
                self.gh.tree = Mock(side_effect=exc)
                reviewer = Mock()
                reviewer.review_pr.return_value = Decision("nothing", "reviewed", "ai")
                runner = self.runner(reviewer)
                if transient:
                    with self.assertRaises(type(exc)):
                        runner._ask_about_pr(make_pr())
                    reviewer.review_pr.assert_not_called()
                else:
                    runner._ask_about_pr(make_pr())
                    kwargs = reviewer.review_pr.call_args.kwargs
                    material = Workspace()
                    prompt = build_pr_prompt(make_pr(), workspace=material, **kwargs)
                    self.assertIn("tree.txt absent:", prompt)
                    self.assertNotIn("tree.txt", material.files)

    def test_bound_and_denial_messages_survive_an_older_blocking_review(self):
        old = Review("me", "CHANGES_REQUESTED", "a" * 40, marker_line("opus", "abcdef", "a" * 40))
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=(old,))]
        bound = "review stopped at the 60-turn limit after opening 1 of 8 files"
        denial = 'Read {"file_path": "../outside"}'
        for decision, expected in ((UnclearAnswer(bound, None), bound),
                                   (Decision("approve", "fine", "ai", data={"denied_calls": [denial]}), denial)):
            with self.subTest(expected=expected):
                self.journal = Journal(Path(self.temp.name) / ("bound.jsonl" if expected == bound else "denial.jsonl"))
                self.notified.clear()
                self.runner(FakeReviewer([decision])).process_pr("org/repo", 7)
                self.assertEqual(len(self.notified), 1)
                self.assertIn(expected, self.notified[0])

    def test_tree_endpoint_is_one_request_and_retains_truncation(self):
        cli = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
            "tree": [{"path": "tests", "type": "tree"}, {"path": "tests/a.py", "type": "blob"}], "truncated": True}), ""))
        self.assertEqual(Gh(runner=cli).tree("org/repo", HEAD), (["tests", "tests/a.py"], True))
        cli.assert_called_once()
        self.assertEqual(cli.call_args.args[0], ["gh", "api", f"repos/org/repo/git/trees/{HEAD}?recursive=1"])

    def test_bounds_settle_alert_and_notify_without_retry_for_both_kinds(self):
        for kind in ("pr", "deployment"):
            for bound in ("max_turns", "budget", "timeout"):
                with self.subTest(kind=kind, bound=bound):
                    key = f"{kind}-{bound}"
                    self.journal = Journal(Path(self.temp.name) / f"{key}.jsonl")
                    self.notified.clear()
                    cli = Mock()
                    partial = stream(call(), reply())
                    if bound == "timeout":
                        cli.side_effect = subprocess.TimeoutExpired("fake", 900, output=partial.encode())
                    else:
                        cli.return_value = subprocess.CompletedProcess([], 0, partial + "\n" + stream(
                            result(is_error=True, terminal_reason=bound, subtype="error_" + bound)), "")
                    engine = ClaudeEngine("opus", runner=cli)
                    engine.probe = Mock()
                    runner = self.runner(Reviewer(engine, RULES))
                    process = (lambda: runner.process_pr("org/repo", 7)) if kind == "pr" else (
                        lambda: runner.process_run("org/repo", 100))
                    self.assertEqual(process(), "unclear")
                    self.assertEqual(process(), "final")
                    cli.assert_called_once()
                    self.assertEqual(len(self.notified), 1)
                    self.assertIn("review stopped at the", self.notified[0])
                    self.assertIn("after opening", self.notified[0])
                    self.assertTrue(self.observer.snapshot.alerts)
                    unclear = next(rec for rec in self.records() if rec["event"] == "unclear")
                    self.assertGreater(unclear["offered"], 0)
                    self.assertFalse(self.gh.writes)

from pathlib import Path
import unittest

from lightkeeper.models import AnswerV2, Finding, is_automated, marker_line, trace_fields
from lightkeeper.posting import BODY_MAX_CHARS, DEFAULT_TEMPLATE, _model_text, render_body
from lightkeeper.settings import CommentSettings, RepoSettings, SettingsError, validate_template

LEGACY_TEMPLATE = "{summary}\n\n{findings}\n\n{disclosure}"

HEAD = "abcdef0123456789" * 2 + "abcdef01"
JUDGEMENT = "123456abcdef"
TRACE = "<!-- lightkeeper v2 head=abcdef0 judgement=123456abcdef engine=claude:opus -->"
FINDINGS = tuple(Finding(severity, origin, text, path) for severity, origin, text, path in (
    ("blocker", "diff", "Fix the condition.", "src/example.py"),
    ("important", "ci", "The test failed.", None),
    ("minor", "comment", "Simplify the name.", None),
))


class PostingTests(unittest.TestCase):
    def render(self, *, action="comment", summary="Public summary.", findings=FINDINGS,
               template=LEGACY_TEMPLATE, remarks="all", disclose=True, why="Looks solid; no blockers.", watch="", **trace):
        return render_body(AnswerV2(action, summary, findings, "PRIVATE_REASON_SENTINEL", "PRIVATE_NOTIFY_SENTINEL",
                                   why=why, watch=watch),
                           template, CommentSettings(disclose=disclose), RepoSettings("example/widget", remarks=remarks),
                           **(dict(head_sha=HEAD, judgement=JUDGEMENT, engine_label="claude:opus", login="reviewer") | trace))

    def test_default_examples(self):
        cases = (
            ("approve", "Approved: the migration is additive and the rollback path is covered by tests.",
             "One thing to keep in mind: the retry loop in orders/sync.py swallows timeouts silently."),
            ("request_changes", "Requesting changes: the new endpoint returns rows without the status filter.",
             "Everything else is fine, including the schema change."),
            ("comment", "Looks solid; no blockers.", "The only caveat is that CI hasn't run on the last push."),
            ("approve", "Approved: the migration is additive and the rollback path is covered by tests.", ""),
        )
        for action, why, watch in cases:
            for disclose in (False, True):
                with self.subTest(action=action, watch=watch, disclose=disclose):
                    result = self.render(action=action, why=why, watch=watch, template="", disclose=disclose)
                    public = "🔭 " + why + ("\n\n" + watch if watch else "")
                    if disclose:
                        public += "\n\nAutomated review for @reviewer (lightkeeper)"
                    self.assertEqual(result.body, public + "\n\n" + TRACE)
                    self.assertEqual(result.published_findings, 0)
                    self.assertNotIn("PRIVATE", result.body)
                    self.assertNotIn("Public summary", result.body)

    def test_new_fields_neutralise_mentions_fences_and_html(self):
        for field in ("why", "watch", "summary"):
            result = self.render(template="{why}\n{watch}\n{summary}", disclose=False, **{
                field: "Ask @someone or @org/team about <!-- text --> and `inline`.\n```"})
            self.assertIn("Ask someone or org/team", result.body)
            self.assertNotIn("@", result.body)
            self.assertIn("<!‑‑ text ‑‑>", result.body)
            self.assertIn("\\```", result.body)
            self.assertIn("`inline`", result.body)
            self.assertTrue(is_automated(result.body))

    def test_mentions_only_change_linkable_prose(self):
        cases = {"@@user": "user", "@@@user": "user", "@org/team": "org/team",
                 "see @user, thanks": "see user, thanks"}
        unchanged = ("a@b.com", "`@dataclass`", "lodash@4.17.21", "git@github.com:o/r",
                     "packages/@scope/x.ts", "https://www.npmjs.com/package/@scope/pkg",
                     "`@user`", "a@@user")
        cases.update((text, text) for text in unchanged)
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(_model_text(source), expected)
                self.assertEqual(_model_text(expected), expected)
        finding = Finding("important", "diff", "Ask @@user", "@scope/x.ts", "@user")
        result = self.render(template="{findings}", findings=(finding,), disclose=False)
        self.assertIn("Ask user [@scope/x.ts](@user)", result.body)

    def test_shipped_template_matches_default(self):
        template = (Path(__file__).resolve().parents[1] / "lightkeeper" / "templates" / "template.md").read_text()
        self.assertEqual(template.rstrip("\n"), DEFAULT_TEMPLATE)
        for disclose in (False, True):
            for watch in ("", "Check the retry path."):
                with self.subTest(disclose=disclose, watch=watch):
                    self.assertEqual(self.render(template=template, disclose=disclose, watch=watch),
                                     self.render(template="", disclose=disclose, watch=watch))

    def test_empty_placeholders_collapse_blank_lines_before_cap(self):
        result = self.render(template="{why}\n\n{watch}\n\n{disclosure}")
        self.assertEqual(result.body, "Looks solid; no blockers.\n\n"
                         "Automated review for @reviewer (lightkeeper)\n\n" + TRACE)
        limit = BODY_MAX_CHARS - len(TRACE) - 2
        result = self.render(template="x" * (limit - 4) + "\n\n{watch}\n\n{findings}",
                             findings=(Finding("blocker", "diff", "x"),), disclose=False)
        self.assertEqual(result.published_findings, 0)
        result = self.render(template="x" * (limit - 29) + "\n" * 100 + "{findings}",
                             findings=(Finding("blocker", "diff", "x"),), disclose=False)
        self.assertEqual(result.published_findings, 1)
        self.assertNotIn("…", result.body)
        result = self.render(template="\n\n{watch}\n\n{findings}", disclose=False)
        self.assertEqual(result.published_findings, len(FINDINGS))
        self.assertNotIn("\n\n\n", result.body)

    def test_all_placeholders_still_validate_and_disclosure_is_opt_in(self):
        for template in ("{why}\n{watch}", LEGACY_TEMPLATE):
            validate_template(template)
        self.assertFalse(CommentSettings().disclose)
        for disclose in (False, True):
            result = self.render(template="🔭 {why}\n\n{watch}\n\n{disclosure}", disclose=disclose)
            self.assertEqual("Automated review for @reviewer (lightkeeper)" in result.body, disclose)

    def test_remarks_levels_and_legacy_custom_layout(self):
        for remarks, count in (("blockers", 1), ("important", 2), ("all", 3)):
            with self.subTest(remarks=remarks):
                result = self.render(remarks=remarks)
                bullets = [f"- **{f.severity}** ({f.origin}): {f.text}" + (f" [{f.path}]" if f.path else "")
                           for f in FINDINGS[:count]]
                self.assertEqual(result.body, "Public summary.\n\n" + "\n".join(bullets)
                                 + "\n\nAutomated review for @reviewer (lightkeeper)\n\n" + TRACE)
                self.assertEqual(result.published_findings, count)
                self.assertIsNone(result.withheld_reason)
                self.assertNotIn("PRIVATE", result.body)

    def test_disclosure_off_and_custom_template(self):
        result = self.render(template="Review: {summary}\n{disclosure}\n{findings}", disclose=False, remarks="blockers")
        self.assertEqual(result.body, "Review: Public summary.\n\n- **blocker** (diff): Fix the condition."
                         " [src/example.py]\n\n" + TRACE)
        self.assertNotIn("Automated review", result.body)
        self.assertTrue(is_automated(result.body))
        self.assertEqual(self.render(template="{summary}").published_findings, 0)

    def test_unknown_placeholders_cannot_publish_private_fields(self):
        for token in ("{reason}", "{notify}", "{action}", "{summary.upper}", "{summary!r}", "{summary:600}", "{}"):
            with self.subTest(token=token), self.assertRaises(SettingsError):
                self.render(template=token)
        # A withheld answer must not hide a broken template setting.
        with self.assertRaises(SettingsError):
            self.render(summary="", findings=(), template="{reason}")

    def test_model_braces_are_literal_single_pass(self):
        finding = Finding("important", "review", '{summary} {disclosure} {reason} {"x": 1}', "{findings}.py")
        result = self.render(summary="{findings} {reason}", findings=(finding,),
                             template="{summary}\n{findings}\n{summary}\n{disclosure}", disclose=False)
        self.assertEqual(result.body, '{findings} {reason}\n- **important** (review): {summary} {disclosure} {reason}'
                         ' {"x": 1} [{findings}.py]\n{findings} {reason}\n\n' + TRACE)

    def test_blank_why_withholds_comment_and_request_changes_even_with_summary(self):
        for action in ("request_changes", "comment", "approve", "nothing"):
            for findings in ((), (FINDINGS[-1],)):
                for disclose in (False, True):
                    with self.subTest(action=action, findings=findings, disclose=disclose):
                        result = self.render(action=action, why=" \n", summary="Public summary", findings=findings,
                                             remarks="blockers", disclose=disclose)
                        if action in ("request_changes", "comment"):
                            self.assertEqual((result.body, result.published_findings, result.withheld_reason),
                                             ("", 0, "no publishable rationale: why is blank"))
                        else:
                            self.assertTrue(result.body.endswith(TRACE))
                            self.assertEqual(result.published_findings, 0)
                            self.assertIsNone(result.withheld_reason)
        for action in ("request_changes", "comment"):
            result = self.render(action=action, summary="Public rationale", findings=(FINDINGS[-1],), remarks="blockers")
            self.assertIsNone(result.withheld_reason)
            self.assertIn("Public rationale", result.body)

    def test_cap_retains_whole_lines_and_complete_trace(self):
        findings = tuple(Finding("important", "diff", str(i) + "界" * 499) for i in range(12))
        result = self.render(summary="🙂" * 600, findings=findings)
        public, trace = result.body.rsplit("\n\n", 1)
        self.assertLessEqual(len(result.body), BODY_MAX_CHARS)
        self.assertTrue(public.endswith("\n…\n\nAutomated review for @reviewer (lightkeeper)"))
        self.assertEqual(trace, TRACE)
        self.assertTrue(is_automated(result.body))
        self.assertEqual(result.published_findings, 6)
        for index in range(6):
            self.assertIn(str(index) + "界" * 499, public)
        self.assertNotIn("6界", public)

    def test_exact_cap_unicode_and_unbroken_line(self):
        limit = BODY_MAX_CHARS - len(TRACE) - 2
        for size in (limit - 1, limit):
            result = self.render(template="🙂" * size, disclose=False)
            self.assertEqual(result.body, "🙂" * size + "\n\n" + TRACE)
        self.assertEqual(self.render(action="approve", template="🙂" * (limit + 1), disclose=False).body, "…\n\n" + TRACE)
        result = self.render(template="界" * (limit - 2) + "\nmore", disclose=False)
        self.assertEqual(result.body, "界" * (limit - 2) + "\n…\n\n" + TRACE)

    def test_disclosure_survives_cap_at_any_template_position(self):
        findings = tuple(Finding("important", "diff", "x" * 500) for _ in range(12))
        for template in (LEGACY_TEMPLATE, "{summary}\n{findings}", "{disclosure}\n{summary}\n{findings}",
                         "{summary}\n{disclosure}\n{findings}", "{summary}\n{findings}\n{disclosure}"):
            with self.subTest(template=template):
                result = self.render(template=template, findings=findings)
                self.assertLessEqual(len(result.body), BODY_MAX_CHARS)
                self.assert_trace_and_disclosure(result)
        result = self.render(findings=findings, disclose=False)
        self.assertNotIn("Automated review", result.body)

    def test_finding_url_with_and_without_path(self):
        for path, url, location in (("src/example.py", "https://example.invalid/check",
                                    " [src/example.py](https://example.invalid/check)"),
                                   (None, "https://example.invalid/check", " (https://example.invalid/check)"),
                                   ("src/example.py", None, " [src/example.py]"), (None, None, "")):
            with self.subTest(path=path, url=url):
                result = self.render(summary="", findings=(Finding("blocker", "diff", "Fix this.", path, url),),
                                     template="{findings}", disclose=False)
                self.assertEqual(result.body, "- **blocker** (diff): Fix this." + location + "\n\n" + TRACE)
                self.assertEqual(result.published_findings, 1)

    def test_withholding_uses_retained_template_body(self):
        cases = (
            dict(template="{disclosure}", summary="Omitted summary", findings=FINDINGS),
            dict(template="{summary}\n{disclosure}", summary="", findings=FINDINGS),
            dict(template="{findings}\n{disclosure}", summary="Omitted summary", findings=()),
            dict(template="{disclosure}\n{findings}", summary="", findings=(
                Finding("blocker", "diff", "Clipped rationale", "p" * 4000),)),
            dict(template="{summary}", summary="x" * 4001, findings=()),
            dict(summary="", findings=(Finding("blocker", "diff", " \t\n"),)),
        )
        for action in ("comment", "request_changes", "approve", "nothing"):
            for disclose in (False, True):
                for case in cases:
                    with self.subTest(action=action, disclose=disclose, case=case):
                        result = self.render(action=action, disclose=disclose, **case)
                        if action in ("comment", "request_changes"):
                            self.assertEqual(result.body, "")
                            self.assertEqual(result.published_findings, 0)
                            self.assertIsNotNone(result.withheld_reason)
                        else:
                            self.assertIsNone(result.withheld_reason)
                            self.assertEqual(result.body.splitlines()[-1], TRACE)

    def assert_trace_and_disclosure(self, result):
        self.assertEqual(result.body.splitlines()[-1], TRACE)
        self.assertEqual(trace_fields(result.body),
                         {"head": "abcdef0", "judgement": JUDGEMENT, "engine": "claude:opus"})
        public = result.body.rsplit("\n\n", 1)[0]
        self.assertNotIn("<!--", public)
        self.assertNotIn("-->", public)
        self.assertIn("Automated review for @reviewer (lightkeeper)", public)
        self.assertNotRegex(result.body, r"(?m)^[ \t]*(?:`{3,}|~{3,})")

    def test_reserved_syntax_in_each_model_field_is_neutralised(self):
        v1 = marker_line("opus", "abcdef", HEAD)
        v2 = TRACE.replace(JUDGEMENT, "abcdef123456")
        for field in ("summary", "text", "path"):
            for injected in (v1, v2, "<!-- open comment", "closing --> text"):
                with self.subTest(field=field, injected=injected):
                    payload = "Public rationale\n" + injected + "\n"
                    finding = Finding("blocker", "diff", payload if field == "text" else "Fix this.",
                                      payload if field == "path" else None)
                    result = self.render(summary=payload if field == "summary" else "Summary", findings=(finding,))
                    self.assert_trace_and_disclosure(result)
                    public = result.body.rsplit("\n\n", 1)[0]
                    self.assertNotIn(injected, public)
                    neutral = injected.replace("<!--", "<!‑‑").replace("-->", "‑‑>")
                    if injected == v1:
                        neutral = neutral.replace("lightkeeper (", "lightkeeper\u00a0(")
                    self.assertIn(neutral, public)

    def test_unclosed_fence_and_truncated_closing_fence(self):
        cases = (
            dict(summary="```python\nPublic rationale", findings=()),
            dict(summary="Summary", findings=(Finding("blocker", "diff", "Fix this.\n```"),)),
            dict(summary="Summary", findings=(Finding("blocker", "diff", "Fix this.", "path\n```\n"),)),
            dict(template="{disclosure}\n{summary}", findings=(),
                 summary="```\n" + "rationale\n" * 500 + "```"),
        )
        for case in cases:
            with self.subTest(case=case):
                result = self.render(**case)
                self.assert_trace_and_disclosure(result)
                self.assertNotRegex(result.body, r"(?m)^[ \t]*`{3,}")
        balanced = self.render(summary="```\nPublic rationale\n```", findings=())
        self.assert_trace_and_disclosure(balanced)
        self.assertEqual(balanced.body.count("\\```"), 2)

    def test_fence_lines_are_escaped_and_inline_spans_unchanged(self):
        for fence in ("````", "~~~", "  ```", "\t~~~", "```python", "~~~~ language", "   ````python"):
            for field in ("summary", "text"):
                with self.subTest(fence=fence, field=field):
                    payload = f"Rationale with `inline` and ``code``.\n{fence}\ncontent\n{fence}"
                    result = self.render(summary=payload if field == "summary" else "Summary",
                                         findings=(Finding("blocker", "diff", payload if field == "text" else "Fix."),))
                    self.assert_trace_and_disclosure(result)
                    escaped = fence[:len(fence) - len(fence.lstrip())] + "\\" + fence.lstrip()
                    self.assertEqual(result.body.count(escaped), 2)
                    self.assertIn("`inline` and ``code``", result.body)

    def test_repeated_findings_count_once_and_clipped_findings_do_not_count(self):
        result = self.render(template="{findings}\n{findings}", remarks="blockers")
        self.assertEqual(result.published_findings, 1)
        result = self.render(findings=(Finding("blocker", "diff", "x", "p" * 4000),))
        self.assertEqual(result.published_findings, 0)
        self.assertEqual(result.body, "Public summary.\n\n…\n\n"
                         "Automated review for @reviewer (lightkeeper)\n\n" + TRACE)

    def test_trace_round_trip_and_strict_last_line_reader(self):
        body = self.render().body
        expected = {"head": "abcdef0", "judgement": JUDGEMENT, "engine": "claude:opus"}
        for suffix in ("", "\n\n", " \n"):
            self.assertEqual(trace_fields(body + suffix), expected)
        for bad in ("", marker_line("opus", "abcdef", HEAD), "> " + TRACE, "    " + TRACE,
                    body + "\ntrailing text", TRACE.replace("abcdef0", "not-hex"),
                    TRACE.replace(JUDGEMENT, "oops"), TRACE.replace("claude:opus", "claude opus"),
                    TRACE[:-1], "prefix " + TRACE):
            with self.subTest(body=bad):
                self.assertIsNone(trace_fields(bad))

    def test_invalid_trace_inputs_are_rejected(self):
        for field, value in (("head_sha", "bad"), ("judgement", "123456"), ("engine_label", ""),
                             ("engine_label", "two words"), ("engine_label", "engine\nline"),
                             ("engine_label", "engine-->")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.render(**{field: value})

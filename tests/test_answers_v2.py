"""Strict v2 contracts and retained legacy parser schemas."""
from copy import deepcopy
import unittest

from lightkeeper.models import AnswerV2, Finding, PR_ACTIONS
from lightkeeper.reviewer import DEPLOY_SCHEMA, DEPLOY_SCHEMA_V2, PR_SCHEMA, PR_SCHEMA_V2
from lightkeeper.reviewer import UnclearAnswer, parse_pr_answer_v2


FINDING = {"severity": "blocker", "origin": "diff", "text": "Fix the retry condition."}
ANSWER = {"action": "request_changes", "summary": "Please address this defect.",
          "findings": [FINDING], "reason": "private",
          "why": "Requesting changes: the retry condition is inverted.", "watch": ""}


class AnswerV2Tests(unittest.TestCase):
    def reject(self, obj):
        with self.assertRaises(UnclearAnswer) as caught:
            parse_pr_answer_v2(obj)
        self.assertIsNone(caught.exception.session)

    def test_valid_actions_severities_origins_and_optional_fields(self):
        for action in PR_ACTIONS:
            for severity in ("blocker", "important", "minor"):
                for origin in ("diff", "ci", "review", "comment"):
                    finding = dict(FINDING, severity=severity, origin=origin,
                                   path="src/example.py", url="https://example.invalid/check")
                    answer = parse_pr_answer_v2(dict(ANSWER, action=action, findings=[finding], notify=" look "))
                    self.assertEqual(answer, AnswerV2(action, ANSWER["summary"], (Finding(**finding),), "private", "look",
                                                     why=ANSWER["why"], watch=""))
        answer = parse_pr_answer_v2(ANSWER)
        self.assertIsNone(answer.notify)
        self.assertIsNone(answer.findings[0].path)
        self.assertIsNone(answer.findings[0].url)

    def test_empty_reason_and_public_rationale_are_valid_at_parse_time(self):
        for action in PR_ACTIONS:
            for reason in ("", " \n "):
                answer = parse_pr_answer_v2(dict(ANSWER, action=action, summary=" \n", findings=[],
                                                 reason=reason, notify=" "))
                self.assertEqual((answer.summary, answer.findings, answer.reason, answer.notify), ("", (), "", None))

    def test_why_is_required_nonblank_and_newlines_are_collapsed(self):
        for action in PR_ACTIONS:
            for why in ("", " ", "\t\n\r", "\u2003\u00a0"):
                self.reject(dict(ANSWER, action=action, why=why))
        answer = parse_pr_answer_v2(dict(ANSWER, why="  Approved:\nthe tests cover rollback.  ",
                                         watch="  CI\r\nhas not run.\u2028Check it.  "))
        self.assertEqual(answer.why, "Approved: the tests cover rollback.")
        self.assertEqual(answer.watch, "CI has not run. Check it.")
        self.assertEqual(PR_SCHEMA_V2["properties"]["why"]["minLength"], 1)

    def test_why_watch_whitespace_collapses_before_length_checks(self):
        for key, limit in (("why", 240), ("watch", 300)):
            expected = "x " + "y" * (limit - 2)
            for value in (expected + "\n", "  x\n\n\t" + "y" * (limit - 2) + "  "):
                with self.subTest(key=key, value=value):
                    obj = dict(ANSWER, **{key: value})
                    self.assertEqual(getattr(parse_pr_answer_v2(obj), key), expected)
                    self.assertEqual(obj[key], value)
        self.assertEqual(parse_pr_answer_v2(dict(ANSWER, watch=" \n" * 300)).watch, "")

    def test_top_level_shape_and_required_keys(self):
        for bad in (None, [], "answer", 1, True, dict(ANSWER, extra="x")):
            with self.subTest(bad=bad):
                self.reject(bad)
        for key in ("action", "why", "watch", "summary", "findings", "reason"):
            bad = dict(ANSWER)
            del bad[key]
            with self.subTest(missing=key):
                self.reject(bad)

    def test_string_types_and_unknown_action(self):
        for key in ("action", "why", "watch", "summary", "reason", "notify"):
            for value in (None, 1, True, [], {}):
                with self.subTest(key=key, value=value):
                    self.reject(dict(ANSWER, **{key: value}))
        for action in ("merge", "APPROVE", " approve ", ""):
            self.reject(dict(ANSWER, action=action))

    def test_findings_shape_required_fields_and_extra_keys(self):
        for findings in (None, {}, "", (), 1, [FINDING] * 13):
            with self.subTest(findings=findings):
                self.reject(dict(ANSWER, findings=findings))
        for finding in (None, [], "finding", 1, dict(FINDING, extra="x")):
            self.reject(dict(ANSWER, findings=[finding]))
        for key in FINDING:
            finding = dict(FINDING)
            del finding[key]
            self.reject(dict(ANSWER, findings=[finding]))
        for key in ("severity", "origin", "text", "path", "url"):
            for value in (None, 1, True, [], {}):
                with self.subTest(key=key, value=value):
                    self.reject(dict(ANSWER, findings=[dict(FINDING, **{key: value})]))
        for key, values in (("severity", ("critical", "BLOCKER", "")), ("origin", ("pipeline", "DIFF", ""))):
            for value in values:
                self.reject(dict(ANSWER, findings=[dict(FINDING, **{key: value})]))

    def test_unicode_limits_are_strict_before_trimming(self):
        for key, limit in (("why", 240), ("watch", 300), ("summary", 600), ("reason", 300), ("notify", 200)):
            with self.subTest(key=key):
                self.assertEqual(getattr(parse_pr_answer_v2(dict(ANSWER, **{key: "🙂" * limit})), key), "🙂" * limit)
                self.reject(dict(ANSWER, **{key: "🙂" * (limit + 1)}))
                if key != "watch":
                    self.reject(dict(ANSWER, **{key: " " * (limit + 1)}))
        valid = dict(ANSWER, findings=[dict(FINDING, text="界" * 500)] * 12)
        self.assertEqual(len(parse_pr_answer_v2(valid).findings), 12)
        for text in ("界" * 501, " " * 501):
            self.reject(dict(ANSWER, findings=[dict(FINDING, text=text)]))

    def test_blank_finding_text_is_rejected_for_every_action(self):
        for action in PR_ACTIONS:
            for text in ("", " ", "\t\n\r", "\u2003\u00a0"):
                with self.subTest(action=action, text=text):
                    self.reject(dict(ANSWER, action=action, summary="",
                                     findings=[dict(FINDING, text=text, path="src/example.py")]))

    def test_parse_does_not_mutate_input(self):
        obj = deepcopy(ANSWER)
        before = deepcopy(obj)
        parse_pr_answer_v2(obj)
        self.assertEqual(obj, before)

    def test_schemas_are_additive_and_keep_legacy_contract(self):
        self.assertEqual(PR_SCHEMA["required"], ["action", "body", "reason", "notify"])
        self.assertNotIn("summary", PR_SCHEMA["properties"])
        self.assertEqual(PR_SCHEMA["properties"]["body"]["maxLength"], 600)
        self.assertNotIn("notify", PR_SCHEMA_V2["required"])
        self.assertFalse(PR_SCHEMA_V2["additionalProperties"])
        self.assertFalse(PR_SCHEMA_V2["properties"]["findings"]["items"]["additionalProperties"])
        self.assertEqual(DEPLOY_SCHEMA["required"], ["decision", "reason"])
        self.assertNotIn("summary", DEPLOY_SCHEMA["properties"])
        self.assertEqual(DEPLOY_SCHEMA_V2["required"], ["decision", "reason", "summary"])
        self.assertEqual(DEPLOY_SCHEMA_V2["properties"],
                         {**DEPLOY_SCHEMA["properties"], "summary": {"type": "string", "maxLength": 300}})
        self.assertFalse(DEPLOY_SCHEMA_V2["additionalProperties"])

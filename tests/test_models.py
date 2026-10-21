import unittest
from dataclasses import replace

from lightkeeper.models import Check, ChangedFile, PullRequest, Review, is_automated, marker_line


def _pr(files):
    return PullRequest(
        repo="org/repo", number=1, head_sha="a" * 40, base_ref="main", author="alice",
        is_draft=False, state="OPEN", title="t", body="", requested_users=("me",),
        requested_teams=(), reviews=(), checks=(), files=tuple(files),
    )


class ChangedFileTests(unittest.TestCase):
    def test_binary_file_is_incomplete(self):
        f = ChangedFile("img.png", None, "added", 0, 0, patch=None)
        self.assertFalse(f.is_complete)

    def test_pure_rename_without_patch_is_complete(self):
        f = ChangedFile("b.py", "a.py", "renamed", 0, 0, patch=None)
        self.assertTrue(f.is_complete)

class PullRequestTests(unittest.TestCase):
    def test_fingerprint_ignores_required_flag(self):
        check = Check("unit", "in_progress", "")
        pr = replace(_pr([]), checks=(check,))
        self.assertEqual(pr.fingerprint, replace(pr, checks=(replace(check, required=True),)).fingerprint)

    def test_fingerprint_counts_others_reviews_and_check_links_but_not_own_reviews(self):
        base = _pr([ChangedFile("a.py", None, "modified", 1, 1, "@@")])
        teammate = PullRequest(**{**base.__dict__, "reviews": (Review("bob", "CHANGES_REQUESTED", "a" * 40, "no"),)})
        own = PullRequest(**{**base.__dict__, "reviews": (Review("me", "COMMENTED", "a" * 40,
                                                                  "x\n\n" + marker_line("opus", "abcdef", "a" * 40)),)})
        rerun = PullRequest(**{**base.__dict__, "checks": (Check("t", "completed", "failure", "https://x/runs/2/job/9"),)})
        again = PullRequest(**{**base.__dict__, "checks": (Check("t", "completed", "failure", "https://x/runs/3/job/10"),)})
        self.assertNotEqual(base.fingerprint, teammate.fingerprint)
        self.assertEqual(base.fingerprint, own.fingerprint)
        self.assertNotEqual(rerun.fingerprint, again.fingerprint)


class MarkerTests(unittest.TestCase):
    def test_only_the_exact_last_line_counts(self):
        line = marker_line("opus", "abcdef", "0123456789" * 4)
        self.assertEqual(line, "lightkeeper (opus) · policy abcdef · head 0123456")
        self.assertTrue(is_automated(line))
        self.assertTrue(is_automated("CI failed in sast.\n\n" + line + "\n"))
        self.assertFalse(is_automated("lightkeeper missed the bug, do not merge"))
        self.assertFalse(is_automated(line + "\nbut I disagree"))
        self.assertFalse(is_automated(""))
        self.assertTrue(is_automated("<!-- approval-delegate v2 head=abcdef0 judgement=123456 engine=claude -->"))
        self.assertTrue(is_automated("approval-delegate (opus) · policy 123456 · head abcdef0"))



if __name__ == "__main__":
    unittest.main()

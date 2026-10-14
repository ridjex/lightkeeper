"""Exercise real subprocess pipes and persisted sessions under an ASCII locale."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


class Utf8Tests(unittest.TestCase):
    def test_utf8_arguments_with_ascii_filesystem_codec(self):
        from lightkeeper.github import Gh
        from lightkeeper.engines.claude import ClaudeEngine
        value = 'café 日本'
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"structured_output": {}}', ''))
        with patch('os.fsdecode', side_effect=lambda raw: raw.decode('ascii', 'surrogateescape')):
            Gh(runner=runner).run(['api', '-f', value])
            self.assertEqual(runner.call_args.args[0][-1].encode('ascii', 'surrogateescape'), value.encode('utf-8'))
            ClaudeEngine('opus', runner=runner).ask(value, value, {}, 10, Path('.'))
            args = runner.call_args.args[0]
            self.assertIn('--append-system-prompt-file', args)
            self.assertNotIn(value, ' '.join(args))
            self.assertEqual(runner.call_args.kwargs['input'], value)

    def test_cli_pipes_sessions_and_plain_logging_in_ascii_locale(self):
        source = r'''
import json
from pathlib import Path
import subprocess
import sys
from lightkeeper.__main__ import configure_logging, log, main
from lightkeeper.engines.claude import ClaudeEngine
from lightkeeper.github import Gh
from lightkeeper.journal import Journal
from lightkeeper.reviewer import Reviewer
from tests.helpers import make_pr

value = "caf\u00e9 \u65e5\u672c \U0001f52ed"
root = Path(sys.argv[1])
# Keep the real subprocess implementation: mocks alone miss locale-dependent codecs.
def gh_runner(command, **kwargs):
    return subprocess.run([sys.executable, '-c',
        'import sys; sys.stdout.buffer.write(' + repr(json.dumps({'login': value}, ensure_ascii=False).encode('utf-8')) + ')'], **kwargs)
assert Gh(runner=gh_runner).login() == value

def claude_runner(command, **kwargs):
    if command[1:3] == ['auth', 'status']:
        output = {'loggedIn': True, 'email': value}
        code = ''
    else:
        assert Path(command[command.index('--append-system-prompt-file') + 1]).read_text(encoding='utf-8').find(value) >= 0
        output = {'structured_output': {'action': 'approve', 'why': value, 'watch': '',
                  'summary': value, 'findings': [], 'reason': value}}
        code = 'assert ' + repr(value.encode('utf-8')) + ' in sys.stdin.buffer.read(); '
    code = 'import sys; ' + code + 'sys.stdout.buffer.write(' + repr(json.dumps(output, ensure_ascii=False).encode('utf-8')) + ')'
    return subprocess.run([sys.executable, '-c', code], **kwargs)
engine = ClaudeEngine('opus', runner=claude_runner)
assert engine.probe().account == value
reviewer = Reviewer(engine, value, sessions_dir=root / 'sessions')
decision = reviewer.review_pr(make_pr(title=value))
assert decision.summary == value
session = Path(decision.data['session'])
assert json.loads((session / 'decision.json').read_text(encoding='utf-8'))['summary'] == value
assert value in (session / 'prompt.md').read_text(encoding='utf-8')
# Status prints before logging is configured and must also tolerate an ASCII stream.
Journal(root / 'journal.jsonl').append(kind='pr', key=('org/repo', 7, 'head'), event='notify',
                                     mode='dry-run', policy='rules', text=value)
assert main(['--init', '--config-dir', str(root / 'config')]) == 0
assert main(['--status', '--config-dir', str(root / 'config'), '--state-dir', str(root)]) == 0
configure_logging(root)
log(value)
assert value in (root / 'run.log').read_text(encoding='utf-8')
'''
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'LC_ALL': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'}
            proc = subprocess.run([sys.executable, '-c', source, directory], env=env,
                                  cwd=Path(__file__).resolve().parents[1], capture_output=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(r'caf\xe9', proc.stdout)
        self.assertNotIn('Logging error', proc.stderr)

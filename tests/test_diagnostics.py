"""Refresh and hang diagnostics without a terminal, network, threads or sleeps."""
import os
import io
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from lightkeeper import __main__ as cli


class DiagnosticsTests(unittest.TestCase):
    def test_terminal_controls_are_removed_from_plain_output_and_log(self):
        payload = ('start\x1b[31mred\x1b[0m\x1b]52;c;secret\x07'
                   '\x1b]52;c;also-secret\x1b\\\x1bMend\x00\r\b\n\tlast\x1b')
        expected = 'startredend\n\tlast'
        self.assertEqual(cli.clean_log_text(payload), expected)
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new_callable=io.StringIO) as output:
            root = Path(directory)
            cli.configure_logging(root, plain=True)
            cli.log(payload)
            # Exceptions and direct logger calls pass through the run.log formatter too.
            cli._logger.warning('direct: %s', payload)
            try:
                raise ValueError(payload)
            except ValueError:
                cli._logger.exception('failed')
            self.assertIn(expected, output.getvalue())
            recorded = (root / 'run.log').read_text()
            self.assertIn('direct: ' + expected, recorded)
            self.assertIn('ValueError: ' + expected, recorded)
            for text in (output.getvalue(), recorded):
                self.assertNotIn('\x1b', text)
                self.assertNotIn('secret', text)

    def test_private_lifetime_fd_and_installed_exception_hook(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, "_hang_fd", None), \
             patch.object(cli.faulthandler, "register") as register, \
             patch.object(threading, "excepthook"):
            path = Path(directory)
            hang = path / "hang.log"
            hang.write_text("previous dump\n")
            hang.chmod(0o644)
            try:
                cli.configure_diagnostics(path)
                fd = cli._hang_fd
                register.assert_called_once_with(signal.SIGUSR1, file=fd, all_threads=True)
                self.assertEqual(hang.stat().st_mode & 0o777, 0o600)
                cli.configure_diagnostics(path)
                self.assertEqual(cli._hang_fd, fd)
                os.write(fd, b"next dump\n")
                self.assertEqual(hang.read_text(), "previous dump\nnext dump\n")
                error = ValueError("helper failed")
                args = threading.ExceptHookArgs((ValueError, error, None, None))
                with self.assertLogs("lightkeeper", level="ERROR") as logs:
                    threading.excepthook(args)
                self.assertIn("helper failed", logs.output[0])
                self.assertEqual(logs.records[0].exc_info, (ValueError, error, None))
            finally:
                if cli._hang_fd is not None:
                    os.close(cli._hang_fd)


class UnterminatedOscTests(unittest.TestCase):
    def test_unterminated_osc_loses_only_its_escape(self):
        from lightkeeper import __main__ as cli
        text = "org/repo#12: \x1b]title -> approved; posted review 99\nnext line"
        self.assertEqual(cli.clean_log_text(text), "org/repo#12: title -> approved; posted review 99\nnext line")

import io
import logging
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.error_reporting import ConsoleErrorReporter, ErrorReporter, report_error
from src.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        # Exercise the application's real root handler without changing the test runner's logging.
        for patcher in (
            patch.object(logging.getLogger(), 'handlers', []),
            patch.object(logging.getLogger(), 'level', logging.WARNING),
            patch('sys.stderr', self.output),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.timestamp = datetime(2026, 10, 27, 8, 30, 45, tzinfo=timezone.utc).timestamp()
        self.prefix = datetime.fromtimestamp(self.timestamp).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
        for clock in (
            patch('logging.time.time', return_value=self.timestamp),
            patch('logging.time.time_ns', return_value=int(self.timestamp * 1_000_000_000)),
        ):
            clock.start()
            self.addCleanup(clock.stop)
        configure_logging()

    def test_errors_include_event_time_context_and_original_traceback(self):
        try:
            raise ValueError('bad posting')
        except ValueError as error:
            ConsoleErrorReporter().report(error, context={'source': 'example'})
        self.assertTrue(self.output.getvalue().startswith(
            f'{self.prefix} [ERROR] job_searcher.errors: Error context: {{"source": "example"}}\n'
        ))
        self.assertIn('Traceback (most recent call last):', self.output.getvalue())
        self.assertIn('ValueError: bad posting', self.output.getvalue())

    def test_fallback_and_library_logs_use_timestamps_without_duplicate_handlers(self):
        class BrokenReporter(ErrorReporter):
            def report(self, error, *, context):
                raise RuntimeError('backend failed')

        configure_logging()
        logging.getLogger('aiogram').warning('polling failed')
        logging.getLogger('job_searcher').info('Running in RELEASE mode')
        report_error(BrokenReporter(), ValueError('original failure'))
        output = self.output.getvalue()
        self.assertEqual(output.count('polling failed'), 1)
        self.assertIn(f'{self.prefix} [WARNING] aiogram: polling failed\n', output)
        self.assertIn(f'{self.prefix} [INFO] job_searcher: Running in RELEASE mode\n', output)
        self.assertIn(f'{self.prefix} [ERROR] src.error_reporting: Error reporting failed\n', output)
        self.assertIn('RuntimeError: backend failed', output)

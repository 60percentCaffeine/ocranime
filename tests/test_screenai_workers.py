"""Default OCR parallelism must respect the job's available memory."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

from ocranime import detect_text_frames_screenai


class ScreenAIWorkerTests(unittest.TestCase):
    def check_pool_size(self, memory, expected, requested=None):
        context = Mock()
        context.Pool.return_value.imap_unordered.return_value = []
        with patch('ocranime.os.cpu_count', return_value=16), \
             patch('ocranime._screenai_available_memory', return_value=memory, create=True), \
             patch('ocranime._ensure_screenai_downloaded'), \
             patch('multiprocessing.get_context', return_value=context), \
             redirect_stdout(io.StringIO()):
            detect_text_frames_screenai([], ['zh-Hant'], num_workers=requested)
        context.Pool.assert_called_once_with(processes=expected)

    def test_memory_limited_job_still_runs_multiple_workers(self):
        self.check_pool_size(10 * 2**30, 4)

    def test_sufficient_memory_uses_all_cpus(self):
        self.check_pool_size(40 * 2**30, 16)

    def test_explicit_worker_count_is_preserved(self):
        self.check_pool_size(10 * 2**30, 16, requested=16)

    def test_unknown_memory_falls_back_to_cpu_count(self):
        self.check_pool_size(None, 16)


if __name__ == '__main__':
    unittest.main()

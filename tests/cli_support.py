"""Bounded, isolated runs of the real OCR CLI, including historical replay."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT / 'tests' / 'fixtures'


class CliTestCase(unittest.TestCase):
    def run_clip(self, name, *options):
        clip = FIXTURES / name
        if not clip.is_file():
            self.skipTest('local video fixture missing; see tests/fixtures/README.md')
        if not sys.platform.startswith('linux'):
            self.skipTest('requires Linux Screen AI')
        if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
            self.skipTest('requires ffmpeg/ffprobe')

        override = os.environ.get('OCRANIME_TEST_SCRIPT')
        if override and sys.version_info < (3, 13):
            self.fail('Historical replay requires Python 3.13+ to bound its pre-scan worker pool')
        script = Path(override or PROJECT / 'ocranime.py').resolve()
        env = os.environ.copy()
        env.update({
            'PYTHON_CPU_COUNT': '1',
            'OMP_NUM_THREADS': '1',
            'OPENBLAS_NUM_THREADS': '1',
            'MKL_NUM_THREADS': '1',
        })
        with tempfile.TemporaryDirectory(prefix='ocranime_clip_test_') as tmp:
            output = Path(tmp) / 'subtitles.srt'
            process = subprocess.Popen(
                [sys.executable, str(script), str(clip),
                 '--scan-backend', 'screenai', '--scan-threads', '1',
                 '--scan-batch-size', '1', *options, '-o', str(output)],
                cwd=tmp, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            try:
                log, _ = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                log, _ = process.communicate()
                self.fail(f'OCR exceeded the 120-second clip-test limit:\n{log}')
            self.assertEqual(process.returncode, 0, log)
            # A single PGS track produces a stream-suffixed filename; older
            # revisions wrote the requested filename and ignored the track.
            outputs = sorted(Path(tmp).glob('subtitles*.srt'))
            self.assertEqual(len(outputs), 1, log)
            output = outputs[0]
            srt = output.read_text(encoding='utf-8')
            entries = [block.splitlines() for block in srt.strip().split('\n\n') if block]
            return entries, f'{srt}\n{log}'

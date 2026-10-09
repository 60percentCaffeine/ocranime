"""Real CLI regression: preserve large opening captions in a 1440p video."""

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
CLIP = PROJECT / 'tests' / 'fixtures' / 'CfGdnxdbQ7o-opening.webm'


class OpeningClipTests(unittest.TestCase):
    @unittest.skipUnless(CLIP.is_file(), 'local video fixture missing; see tests/fixtures/README.md')
    @unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux Screen AI')
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg/ffprobe')
    def test_cli_recovers_first_three_opening_captions(self):
        # Override the script only to replay this same test against an old
        # revision, without replacing the working tree or mocking OCR.
        script = Path(os.environ.get('OCRANIME_TEST_SCRIPT', PROJECT / 'ocranime.py'))
        env = os.environ.copy()
        env.update({
            'PYTHON_CPU_COUNT': '1',
            'OMP_NUM_THREADS': '1',
            'OPENBLAS_NUM_THREADS': '1',
            'MKL_NUM_THREADS': '1',
        })
        with tempfile.TemporaryDirectory(prefix='ocranime_clip_test_') as tmp:
            output = Path(tmp) / 'opening.srt'
            # The isolated cwd also contains scanned_frames; never touch the
            # project's or user's previously extracted frames.
            process = subprocess.Popen(
                [sys.executable, str(script), str(CLIP),
                 '--scan-backend', 'screenai', '--scan-threads', '1',
                 '--scan-batch-size', '1', '-o', str(output)],
                cwd=tmp, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            try:
                log, _ = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                # Kill worker processes too, not just the CLI parent.
                os.killpg(process.pid, signal.SIGKILL)
                log, _ = process.communicate()
                self.fail(f'OCR exceeded the 120-second clip-test limit:\n{log}')
            self.assertEqual(process.returncode, 0, log)
            self.assertTrue(output.exists(), log)
            srt = output.read_text(encoding='utf-8')
            entries = [block.splitlines() for block in srt.strip().split('\n\n')]
            expected = [
                '我覺得這個是一個底部上面',
                '想世界的一個方式',
                'like這種想世界的方式對我太陌生了',
            ]
            self.assertGreaterEqual(len(entries), 3, f'{srt}\n{log}')
            # Ignore whitespace only; wrong/missing text and chat contamination
            # must fail, rather than merely checking that each phrase exists.
            actual = [re.sub(r'\s+', '', ''.join(entry[2:])) for entry in entries[:3]]
            self.assertEqual(actual, expected, f'{srt}\n{log}')
            self.assertEqual(entries[0][1].split(' --> ')[0], '00:00:00,000', srt)


if __name__ == '__main__':
    unittest.main()

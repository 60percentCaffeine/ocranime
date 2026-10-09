import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ocranime import auto_detect_crop


class AutoCropTests(unittest.TestCase):
    def detect(self, lines, dimensions=(2560, 1440)):
        def extract(cmd, **kwargs):
            pattern = cmd[-1]
            # Simulate ffmpeg's aspect-preserving dimension clamp.
            scale = min(1, 2048 / max(dimensions)) if 'scale=' in cmd[cmd.index('-vf') + 1] else 1
            size = tuple(round(d * scale) for d in dimensions)
            Image.new('RGB', size).save(pattern.replace('%06d', '000001'))

        with patch('ocranime._get_video_dimensions', return_value=dimensions), \
             patch('ocranime._get_video_duration', return_value=120), \
             patch('ocranime._append_hdr_to_sdr_filters'), \
             patch('ocranime.subprocess.run', side_effect=extract), \
             patch('ocranime.detect_text_frames_screenai', side_effect=lambda frames, *a, **kw: ([], [lines] * len(frames))):
            return auto_detect_crop('source.webm', ['zh-Hant'], 'pad')

    def test_maps_prescan_bboxes_back_to_source_pixels(self):
        crop, heights = self.detect([('我覺得這個是一個底部上面', 400, 880, 1200, 96)])
        w, h, x, y = map(int, crop.split(':'))
        self.assertLessEqual(x, 500)
        self.assertGreaterEqual(x + w, 2000)
        self.assertLessEqual(y, 1100)
        self.assertGreaterEqual(y + h, 1220)
        self.assertEqual(heights, (120, 120))

    def test_ui_density_does_not_hide_subtitle_height_mode(self):
        lines = [('我覺得這個是一個底部上面', 300, 850, 1400, 96)]
        lines += [('聊天室訊息', 1600, 800 + i * 10, 300, 18) for i in range(30)]
        crop, heights = self.detect(lines)
        self.assertEqual(heights, (120, 120))
        w, h, x, y = map(int, crop.split(':'))
        self.assertLessEqual(y, 1062)
        self.assertGreaterEqual(y + h, 1183)


if __name__ == '__main__':
    unittest.main()

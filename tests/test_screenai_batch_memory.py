"""Exercise decoded-image lifetimes through the actual OCR worker."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ocranime import _screenai_worker_ocr


class ScreenAIBatchMemoryTests(unittest.TestCase):
    def test_releases_completed_sub_batches_without_changing_frame_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(17):
                path = Path(tmp) / f'{i}.png'
                with Image.new('RGB', (128, 32), (i, 0, 0)) as image:
                    image.save(path)
                paths.append(str(path))

            opened = []
            live_counts = []
            trim_live_counts = []
            original_open = Image.open
            with Image.new('RGB', (1, 1)) as image:
                core_type = type(image._im)

            def track_open(path):
                image = original_open(path)
                opened.append(image)
                return image

            def recognize(engine, image):
                image.load()
                self.assertEqual(image.width, 128)
                live_counts.append(sum(isinstance(im._im, core_type) for im in opened))
                return {'lines': [
                    {'utf8_string': str(image.getpixel((0, y))[0]),
                     'bounding_box': {'x': 1, 'y': y + 2, 'width': 10, 'height': 4}}
                    for y in range(0, image.height, 32)
                ]}

            def trim(pad):
                self.assertEqual(pad, 0)
                trim_live_counts.append(sum(isinstance(im._im, core_type) for im in opened))

            with patch('PIL.Image.open', side_effect=track_open), \
                 patch('ocranime._load_screenai', return_value={'max_pixel_size': 128, 'malloc_trim': trim}), \
                 patch('ocranime._screenai_perform_ocr', side_effect=recognize), \
                 patch.object(_screenai_worker_ocr, '_stderr_suppressed', True, create=True):
                indices, lines = _screenai_worker_ocr((list(range(17)), paths))

            self.assertEqual(indices, list(range(17)))
            self.assertEqual(lines, [[(str(i), 1, 2, 10, 4)] for i in range(17)])
            self.assertLessEqual(max(live_counts), 4,
                                 'decoded frames accumulated across native OCR sub-batches')
            self.assertEqual(trim_live_counts, [0] * 6,
                             'heap trimming must follow each group and task image cleanup')

    def test_keeps_large_individual_frames_at_full_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frame.png'
            with Image.new('RGB', (128, 96)) as image:
                image.save(path)
            sizes = []

            def recognize(engine, image):
                sizes.append(image.size)
                return {'lines': []}

            with patch('ocranime._load_screenai', return_value={'max_pixel_size': 128}), \
                 patch('ocranime._screenai_perform_ocr', side_effect=recognize), \
                 patch.object(_screenai_worker_ocr, '_stderr_suppressed', True, create=True):
                _screenai_worker_ocr(([0, 1, 2], [str(path)] * 3))
            self.assertEqual(sizes, [(128, 96)] * 3)


if __name__ == '__main__':
    unittest.main()

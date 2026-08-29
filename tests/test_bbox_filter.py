import unittest

from ocranime import _compute_line_height_range


class BboxHeightFilterTests(unittest.TestCase):
    def test_keeps_bimodal_legitimate_subtitle_heights(self):
        # Real ScreenAI output can have both small and large subtitle bbox modes.
        heights = [34] * 25 + [51] * 50 + [80] * 25

        min_h, max_h = _compute_line_height_range(heights)

        self.assertLessEqual(min_h, 34)
        self.assertGreaterEqual(max_h, 80)


if __name__ == "__main__":
    unittest.main()

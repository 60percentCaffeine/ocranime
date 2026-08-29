import unittest

from ocranime import format_timestamp


class SrtTimestampFormattingTests(unittest.TestCase):
    def test_carries_rounded_milliseconds_into_seconds(self):
        self.assertEqual(format_timestamp(54.999999999), "00:00:55,000")

    def test_carries_rounded_seconds_into_minutes(self):
        self.assertEqual(format_timestamp(59.999999999), "00:01:00,000")


if __name__ == "__main__":
    unittest.main()

"""Real CLI regression: preserve large opening captions in a 1440p video."""

import re
import unittest

from cli_support import CliTestCase


class OpeningClipTests(CliTestCase):
    def test_cli_recovers_first_three_opening_captions(self):
        entries, evidence = self.run_clip('CfGdnxdbQ7o-opening.webm')
        expected = [
            '我覺得這個是一個底部上面',
            '想世界的一個方式',
            'like這種想世界的方式對我太陌生了',
        ]
        self.assertGreaterEqual(len(entries), 3, evidence)
        # Ignore whitespace only; wrong/missing text and chat contamination
        # must fail, rather than merely checking that each phrase exists.
        actual = [re.sub(r'\s+', '', ''.join(entry[2:])) for entry in entries[:3]]
        self.assertEqual(actual, expected, evidence)
        self.assertEqual(entries[0][1].split(' --> ')[0], '00:00:00,000', evidence)


if __name__ == '__main__':
    unittest.main()

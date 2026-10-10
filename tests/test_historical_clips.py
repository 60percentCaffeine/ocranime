"""Real-media regressions for previously fixed subtitle failures."""

import re
import unittest

from cli_support import CliTestCase


class HistoricalClipTests(CliTestCase):
    def test_bocchi_keeps_dialogue_in_crop_with_room_for_two_rows(self):
        entries, evidence = self.run_clip(
            'bocchi-dialogue.mp4', '--crop', 'iw:ih*0.3:0:ih*0.7',
            '--cleanup-backend', 'none',
        )
        self.assertEqual(len(entries), 1, evidence)
        self.assertEqual(re.sub(r'\s+', '', ''.join(entries[0][2:])),
                         '為了可以在大家面前演奏', evidence)

    def test_fishing_auto_crop_keeps_both_parts_of_short_dialogue(self):
        entries, evidence = self.run_clip(
            'BV1yqJJ67E5w-opening.mp4', '--cleanup-backend', 'none',
        )
        matching = [entry for entry in entries
                    if re.sub(r'\s+', '', ''.join(entry[2:])) == '跑跑跑跑跑别跑']
        self.assertEqual(len(matching), 1, evidence)
        self.assertEqual(matching[0][1], '00:00:02,500 --> 00:00:03,500', evidence)

    def test_takeaway_review_keeps_larger_dialogue_height_mode(self):
        entries, evidence = self.run_clip(
            'BV14HEX6CEkV-opening.mp4', '--cleanup-backend', 'none',
        )
        matching = [entry for entry in entries
                    if re.sub(r'\s+', '', ''.join(entry[2:])) == '还是这么着急呀']
        self.assertEqual(len(matching), 1, evidence)
        self.assertEqual(matching[0][1], '00:00:01,500 --> 00:00:02,750', evidence)

    def test_guess_price_auto_crop_preserves_long_intro_to_item_cards(self):
        entries, evidence = self.run_clip(
            'guess-price-width-floor.mkv', '--cleanup-backend', 'none',
        )
        self.assertTrue(entries, evidence)
        self.assertEqual(re.sub(r'\s+', '', ''.join(entries[0][2:])),
                         '对了我们今天还会有三张道具卡', evidence)
        self.assertEqual(entries[0][1].split(' --> ')[0], '00:00:00,000', evidence)

    def test_recovers_small_dialogue_in_a_taller_crop(self):
        # Legitimate subtitles are shorter than 27% of this crop's height.
        # The old absolute-height filter dropped them as if they were UI text.
        entries, evidence = self.run_clip(
            'testlines-dialogue.mkv', '--crop', 'iw:ih*0.2:0:ih*0.8',
            '--cleanup-backend', 'none',
        )
        expected = '为什么为什么为什么啊!'
        matching = [entry for entry in entries
                    if re.sub(r'\s+', '', ''.join(entry[2:])) == expected]
        self.assertTrue(matching, evidence)

    def test_detects_embedded_pgs_caption_on_blank_video(self):
        # The source video is black: the visible caption lives only in its
        # bitmap subtitle stream. Use flags supported before the PGS pipeline.
        entries, evidence = self.run_clip(
            'sample-pgs-caption.mkv', '--crop', 'iw:ih:0:0',
        )
        self.assertEqual(len(entries), 1, evidence)
        self.assertEqual(re.sub(r'\s+', '', ''.join(entries[0][2:])),
                         '（女贞路）', evidence)

    def test_auto_crop_preserves_long_dialogue_before_prescan_window(self):
        # The opening long line ends before the pre-scan starts; subsequent
        # short lines cannot justify shrinking the crop enough to cut it off.
        entries, evidence = self.run_clip(
            'ocrtest-width-floor.mkv', '--cleanup-backend', 'none',
        )
        self.assertTrue(entries, evidence)
        self.assertEqual(re.sub(r'\s+', '', ''.join(entries[0][2:])),
                         '而進到深處的葛洛姆他們受了重傷', evidence)
        self.assertEqual(entries[0][1].split(' --> ')[0], '00:00:00,000', evidence)


if __name__ == '__main__':
    unittest.main()

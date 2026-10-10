"""Document still-broken media behavior without weakening its assertions."""

import re
import unittest

from cli_support import CliTestCase


class KnownMediaFailureTests(CliTestCase):
    @unittest.expectedFailure
    def test_bocchi_taller_crop_does_not_add_noise_to_second_caption(self):
        entries, evidence = self.run_clip(
            'bocchi-opening.mp4', '--crop', 'iw:ih*0.3:0:ih*0.7',
            '--cleanup-backend', 'none',
        )
        self.assertGreaterEqual(len(entries), 2, evidence)
        actual = [re.sub(r'\s+', '', ''.join(entry[2:])) for entry in entries[:2]]
        self.assertEqual(actual, ['為了可以在大家面前演奏', '每天勤練6小時吉他的結果是'], evidence)

    @unittest.expectedFailure
    def test_helltaker_auto_crop_keeps_commentary_instead_of_game_dialogue(self):
        entries, evidence = self.run_clip(
            'BV1tsjJ61ExU-opening.mp4', '--cleanup-backend', 'none',
        )
        self.assertTrue(any(re.sub(r'\s+', '', ''.join(entry[2:])) == '你们三个一起攻略'
                            for entry in entries), evidence)

    @unittest.expectedFailure
    def test_wordplay_auto_crop_keeps_dialogue_instead_of_game_controls(self):
        entries, evidence = self.run_clip(
            'wordplay-opening.mp4', '--cleanup-backend', 'none',
        )
        self.assertGreaterEqual(len(entries), 2, evidence)
        self.assertEqual(re.sub(r'\s+', '', ''.join(entries[0][2:])), '这是石榴', evidence)
        self.assertTrue(any(re.sub(r'\s+', '', ''.join(entry[2:])) == '土化石'
                            for entry in entries), evidence)

    @unittest.expectedFailure
    def test_cream_auto_crop_keeps_dialogue_instead_of_dense_chat(self):
        # Current auto-crop selects the top watermark rather than the dialogue.
        # Remove expectedFailure only when these original assertions pass.
        entries, evidence = self.run_clip(
            'cream-chat-opening.mp4', '--cleanup-backend', 'none',
        )
        self.assertGreaterEqual(len(entries), 2, evidence)
        actual = [re.sub(r'\s+', '', ''.join(entry[2:])) for entry in entries[:2]]
        self.assertEqual(actual, ['定海神针，你想啊', '定海神针定的是什么啊'], evidence)


if __name__ == '__main__':
    unittest.main()

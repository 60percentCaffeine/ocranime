import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from ocranime import _video_is_hdr, extract_frames


class FrameExtractionTests(unittest.TestCase):
    def test_clamps_extracted_frame_to_max_dimension_preserving_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.mkv"
            output = tmp_path / "frames"
            output.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-f", "lavfi", "-i",
                    "color=c=black:s=320x100:r=1:d=1",
                    "-c:v", "ffv1", "-loglevel", "error", str(source),
                ],
                check=True,
            )

            frames = extract_frames(
                str(source), output, "iw:ih:0:0", fps=1,
                max_dimension=128,
            )

            self.assertEqual(len(frames), 1)
            with Image.open(frames[0]) as frame:
                self.assertLessEqual(max(frame.size), 128)
                self.assertAlmostEqual(frame.width / frame.height, 3.2, delta=0.1)

    def test_tone_maps_pq_hdr_frames_to_sdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "hdr.mkv"
            output = tmp_path / "frames"
            output.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-f", "lavfi", "-i",
                    "color=c=gray:s=64x64:r=1:d=1",
                    "-vf", (
                        "format=yuv420p10le,"
                        "setparams=color_primaries=bt2020:"
                        "color_trc=smpte2084:colorspace=bt2020nc"
                    ),
                    "-c:v", "ffv1", "-loglevel", "error", str(source),
                ],
                check=True,
            )

            self.assertTrue(_video_is_hdr(source))
            frames = extract_frames(
                str(source), output, "iw:ih:0:0", fps=1,
            )

            self.assertEqual(len(frames), 1)
            with Image.open(frames[0]).convert("RGB") as frame:
                # PQ code value 0.5 maps well above its unconverted value of 128.
                self.assertGreater(frame.getpixel((0, 0))[0], 180)


if __name__ == "__main__":
    unittest.main()

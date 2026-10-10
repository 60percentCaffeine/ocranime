# OCR regression fixtures

## `CfGdnxdbQ7o-opening.webm`

The first six seconds of the user-provided video
`這真的不是一個好主意... [CfGdnxdbQ7o].webm`
(YouTube ID: `CfGdnxdbQ7o`). Video only, 2560×1440, about 3.3 MiB.
The original video resolution and compressed video packets are preserved;
there is no re-encoding, resizing, or manually supplied subtitle track.

The video fixture is local-only and gitignored. On a fresh checkout the clip
test skips until this file is supplied. Recreate it from the original video
using the command below; no video is downloaded automatically by the test.

Created with:

```sh
ffmpeg -nostdin -i '這真的不是一個好主意... [CfGdnxdbQ7o].webm' \
  -t 6 -map 0:v:0 -an -c:v copy tests/fixtures/CfGdnxdbQ7o-opening.webm
```

`tests/test_opening_clip.py` runs the actual Screen AI CLI with automatic crop
selection, then checks the first three SRT entries (ignoring whitespace only):

1. 我覺得這個是一個底部上面
2. 想世界的一個方式
3. like 這種想世界的方式對我太陌生了

The first entry must start at `00:00:00,000`.
No OCR calls or crop decisions are mocked. This test fails against the old
`ocranime.py` at commit `5a92f8c`, which recognizes chat messages instead.

Run from the project directory:

```sh
poetry run python -m unittest discover -s tests -p test_opening_clip.py -v
```

Requires Linux, ffmpeg/ffprobe, project Python dependencies, and Chrome Screen AI
resources (the CLI downloads these on first use). The test uses one OCR worker,
limits numerical-library threads, and imposes a 120-second timeout with worker
cleanup. Its temporary directory contains all extracted frames and SRT output;
it does not touch existing `scanned_frames/` directories or SRT files.

To replay the same test against an older revision, set `OCRANIME_TEST_SCRIPT`
to a copy of that revision's `ocranime.py` in the project directory. Use
Python 3.13+ for old-code replay: `PYTHON_CPU_COUNT=1`, set by the test, also
limits its pre-scan pool, which did not honor `--scan-threads`.

## Historical media regressions

`tests/test_historical_clips.py` adds seven actual CLI cases from existing local
media. All new fixtures are gitignored and tests skip individually when their
fixture is absent. No test processes the original full videos or downloads media.
Expected text was checked against visible captions, rather than copied blindly
from earlier OCR output. Whitespace is ignored; missing or wrong characters fail.

| Fixture | Source and preserved properties | Assertion | Historical RED |
| --- | --- | --- | --- |
| `testlines-dialogue.mkv` | `testlines.mkv`, 1280×720 H.264 packets, video only, 5.254 seconds including keyframe pre-roll | Recovers `为什么 为什么 为什么啊!` with a bottom-20% crop | `ca1a828` writes an empty SRT: its absolute 27%-of-crop-height threshold rejects legitimate subtitles. Fixed by `e89e3e5`. |
| `ocrtest-width-floor.mkv` | `ocrtest.mkv`, seconds 31.25–36.25, original 1920×1080 decoded pixels in lossless FFV1, video only, 5.005 seconds | First subtitle is the complete `而進到深處的葛洛姆他們受了重傷`, starting at zero | `c0b1cb7` selects a 664-pixel-wide crop and emits only `深處的葛洛姆他們受`. Fixed by the 70% width floor in `041a474`. |
| `sample-pgs-caption.mkv` | `outputs/sample_pgs_30s.mkv`, first six seconds, original 1920×1080 black H.264 video and Chinese PGS bitmap packets | Exactly one subtitle, `（女贞路）`, from the embedded track | `e18002c` (parent of `ec57588`) ignores the bitmap track and writes an empty SRT. `ec57588` adds the PGS pipeline. |
| `bocchi-dialogue.mp4` | `test.mp4`, first 1.25 seconds (container duration 1.3013s), original 1920×1080 H.264 packets, video only | Exactly one caption, `為了可以在大家面前演奏`, with a bottom-30% crop | `ca1a828` writes an empty SRT because the legitimate text is shorter than 27% of the crop height. |
| `guess-price-width-floor.mkv` | `BV1dN5t6CEUS.mp4`, seconds 83.75–88.75, original 1920×1080 pixels in lossless FFV1, video only | First caption is the complete `对了我们今天还会有三张道具卡`, starting at zero | `c0b1cb7` selects a 452-pixel-wide crop and emits `今天还会有三`. Current code preserves the full line. |
| `BV14HEX6CEkV-opening.mp4` | `aige/` takeaway-review video, first six seconds, original 1920×1080 AV1 packets, video only | Recovers `还是这么着急呀` at 1.50–2.75 seconds | `5a92f8c` drops the legitimate 99-pixel subtitle height mode by selecting only the post-crop 77–83px histogram cluster. Current code retains the auto-selected range with bbox jitter allowance. |
| `BV1yqJJ67E5w-opening.mp4` | `aige/` fishing-game video, first six seconds, original 1920×1080 AV1 packets, video only | Recovers both adjacent parts of `跑跑跑跑跑 别跑` at 2.50–3.50 seconds | `5a92f8c` drops the entire caption after selecting only one post-crop height mode. Current code preserves it. |

Each new test was replayed against the indicated historical revision and failed
for the stated subtitle behavior, with exit status zero from the CLI and all
dependencies available. All seven pass against current code. No unsupported CLI
flags, mocked OCR, or missing dependencies are used as historical failure evidence.

The small-dialogue sample also contains another caption; the test requires the
complete target phrase without constraining unrelated OCR output. The width-floor
sample deliberately places the long line in its first half-second, before the
pre-scan's 10% start point. Exact trimming is necessary here: stream-copy pre-roll
would change that relationship, so this one fixture uses lossless encoding.
The PGS source is an existing generated black-video sample with a real bitmap
caption. Its subtitle packets start at zero and clear at 4.922 seconds; this case
tests embedded-track handling, not a delayed subtitle start or HDR conversion.

Recreate from the project root (no resizing or manually supplied text):

```sh
ffmpeg -nostdin -threads 1 -ss 14 -i testlines.mkv -t 3 \
  -map 0:v:0 -c:v copy -an -avoid_negative_ts make_zero \
  tests/fixtures/testlines-dialogue.mkv

ffmpeg -nostdin -threads 1 -ss 31.25 -i ocrtest.mkv -t 5 \
  -map 0:v:0 -an -c:v ffv1 -threads 1 \
  tests/fixtures/ocrtest-width-floor.mkv

ffmpeg -nostdin -threads 1 -i outputs/sample_pgs_30s.mkv -t 6 \
  -map 0:v:0 -map 0:s:0 -c copy -an \
  tests/fixtures/sample-pgs-caption.mkv
```

The fixtures occupy about 0.5 MiB, 38 MiB, and 20 KiB respectively. The FFV1
fixture is larger on disk but still only five seconds long. Embedded ASS subtitles
from `ocrtest.mkv` are excluded; the test recognizes its burned-in caption pixels.

Run serially using the project interpreter:

```sh
poetry run python -m unittest discover -s tests -p test_historical_clips.py -v
```

The shared `tests/cli_support.py` runner also serves the opening-caption test.
It isolates output and extracted frames, uses one OCR worker with batch size one,
limits numerical-library threads, and kills the entire subprocess group on its
120-second deadline. For old auto-crop code, Python 3.13+ is required so
`PYTHON_CPU_COUNT=1` also bounds the historical pre-scan pool. Run these tests
serially; do not substitute full media or the `ocrtest10x.mkv` stress sample.

For a historical replay, copy the script beside the project's `screenai_protos/`
directory and select only the corresponding test. For example:

```sh
# Replace python3.14 with your project virtualenv's Python 3.13+ interpreter.
git show ca1a828:ocranime.py > .historical-ocranime.py
OCRANIME_TEST_SCRIPT="$PWD/.historical-ocranime.py" python3.14 \
  -m unittest discover -s tests -p test_historical_clips.py -k small_dialogue -v
rm .historical-ocranime.py
```

Use `c0b1cb7` with `-k long_dialogue`, or `e18002c` with `-k embedded_pgs`,
for the other two RED replays. Remove the override to run against current code.
An empty SRT is a behavioral failure; a CLI crash or dependency error must be
investigated separately and does not demonstrate a regression.

For the additional RED replays, use `ca1a828` with `-k bocchi_keeps`,
`c0b1cb7` with `-k guess_price`, and `5a92f8c` with either
`-k takeaway_review` or `-k fishing_auto`.

Recreate the additional root-media fixtures from the project root:

```sh
ffmpeg -nostdin -threads 1 -i test.mp4 -t 6 -map 0:v:0 -an -c:v copy \
  tests/fixtures/bocchi-opening.mp4

ffmpeg -nostdin -threads 1 -i test.mp4 -t 1.25 -map 0:v:0 -an -c:v copy \
  tests/fixtures/bocchi-dialogue.mp4

ffmpeg -nostdin -threads 1 -ss 83.75 -i BV1dN5t6CEUS.mp4 -t 5 \
  -map 0:v:0 -an -c:v ffv1 -threads 1 \
  tests/fixtures/guess-price-width-floor.mkv

ffmpeg -nostdin -threads 1 \
  -i '【奶油-cream-】秦始皇：狂野，震撼亚洲！ [BV1AmFVzZEzw].mp4' \
  -t 6 -map 0:v:0 -an -c:v copy tests/fixtures/cream-chat-opening.mp4

ffmpeg -nostdin -threads 1 \
  -i '这游戏没偷偷骂我两句吧？！【这是谐音梗】 [BV1dydGBKEzE].mp4' \
  -t 6 -map 0:v:0 -an -c:v copy tests/fixtures/wordplay-opening.mp4
```

The guessing-price fixture uses the same exact-trim technique as the anime
width-floor fixture and occupies about 69 MiB. Root opening fixtures are below
1.2 MiB. Recreate the three retained `aige/` fixtures without re-encoding:

```sh
python3 - <<'PY'
from pathlib import Path
import subprocess

for video_id in ('BV14HEX6CEkV', 'BV1yqJJ67E5w', 'BV1tsjJ61ExU'):
    source = next(Path('aige').glob(f'*{video_id}*.mp4'))
    output = Path('tests/fixtures') / f'{video_id}-opening.mp4'
    subprocess.run([
        'ffmpeg', '-nostdin', '-threads', '1', '-i', str(source), '-t', '6',
        '-map', '0:v:0', '-an', '-c:v', 'copy', str(output),
    ], check=True, timeout=20)
PY
```

## Remaining media audit and known failures

These findings concern bounded samples, not complete-video recognition. The
seven `aige/` opening clips were checked serially with the real CLI against
`5a92f8c` and current code; each clip was only six seconds long.

| Original media | Outcome of inspection/replay |
| --- | --- |
| `test.mp4` | Short opening-caption bottom-30% crop regression passes today and fails on `ca1a828`. The six-second sample with that crop still inserts `00` into the second caption; this stronger assertion is retained as a known failure. Its default-auto opening also passes on `c0b1cb7`; that default-auto case does not prove a regression. |
| `BV1dN5t6CEUS.mp4` and the Chinese-title copy with the same ID | SHA-256 identical (`50a62b19679f549d9c0a49b50a3abbc5fbec48cebada3ef15beed910c8375ff6`); one representative regression is sufficient. |
| Cream (`BV1AmFVzZEzw`) | **Still fails today:** auto-crop selects the top watermark instead of the bottom spoken captions. |
| Wordplay (`BV1dydGBKEzE`) | **Still fails today:** auto-crop selects game-control text and drops `这是石榴` / `土化石`. Explicit biggest-cluster filtering also drops legitimate subtitle modes in this sample. |
| `aige/` Helltaker (`BV1tsjJ61ExU`) | **Still fails today:** keeps the in-game panel dialogue, missing the visible spoken caption `你们三个一起攻略` at 5.5 seconds. |
| `aige/` takeaway reviews (`BV14HEX6CEkV`) | Proven historical missing-caption regression added. |
| `aige/` fishing (`BV1yqJJ67E5w`) | Proven historical missing-caption regression added. |
| `aige/` Gretel/Hansel (`BV1uS7k6DEQF`) | Checked `这个老鼠我应该怎么弄它`; both revisions recover it. No historical failure established for this sample. |
| `aige/` fly (`BV1b47E6EEdy`) | Checked `脚底板`; both revisions recover it. No historical failure established for this sample. |
| `aige/` owners' chat (`BV178Vm6NEvj`) | Checked `13栋你的内裤没挂好`; both revisions recover it. No historical failure established for this sample. |
| `aige/` fish names (`BV16ujZ6gEFQ`) | Checked `感觉像在哪里见过`; both revisions recover it. No historical failure established for this sample. |
| `ocrtest10x.mkv` | 9011.52 seconds, exactly ten times `ocrtest.mkv`'s duration. Stress fixture left unprocessed under the resource constraint. |
| `outputs/auto_select_check_30s.mp4`, `script_check_30s.mp4`, `harry_potter_integrated_pgssub_30s.pgssub-black.mp4` | Byte-identical generated outputs, SHA-256 `c15fe588f9909c8d869d01f78f1eed75489ed9e176402cdd300b9a91664c3429`. Not separate original failure inputs. |
| `outputs/pgs_black_test_30s.mp4` | Incomplete file: ffprobe reports `moov atom not found`. Does not provide valid behavioral RED evidence. |
| Other `outputs/` black videos (`sample_pgs_burn_test.mp4`, `test_from_sup.mp4`, `test_offset.mp4`, the 20-minute PGS burn) | Generated pipeline artifacts; embedded PGS handling is tested using the actual track in `sample_pgs_30s.mkv`. No separate historical failure established; the long burn was not OCRed. |

`tests/test_known_media_failures.py` retains the intended assertions for the
four still-broken samples as explicit `unittest.expectedFailure` tests. These
are **not** counted as historical-RED/current-GREEN regressions. Remove the
marker only when the intended caption assertions pass; an unexpected pass then
fails the suite and prompts that update. Skip behavior and resource limits match
the other clip tests. No assertion was weakened to accept watermark/UI text.

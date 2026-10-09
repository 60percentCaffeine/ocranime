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

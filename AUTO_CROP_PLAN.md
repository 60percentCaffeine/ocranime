# Auto-crop heuristic

Automatically derive the ffmpeg crop region for the main OCR pass by pre-scanning a small slice of the video and reading the bbox positions.

## CLI surface

- `--crop auto` becomes the **new default** value for the existing `--crop` flag.
  - Existing manual values (`iw*0.7:ih*0.15:iw*0.15:ih*0.8`, etc.) keep working unchanged.
  - `--crop2` is unaffected and remains mutually exclusive with `--crop`.
- New flag: `--auto-crop-width {pad,full}` (default: `pad`).
  - `pad` — fit the cluster's widest subtitle and add 20% left + 20% right.
  - `full` — use the entire frame width (`x = 0`, `w = iw`).

## Pre-scan

1. Pick `K` short windows (default `K = 6`, each `8s`) evenly spaced across the **middle 80%** of the runtime so OP/ED title cards do not bias the clusters.
2. Extract frames **without any crop** at **4 fps** (matching the main pipeline default):
   ```
   ffmpeg -ss <t> -t 8 -i <video> -vf fps=4 ...
   ```
   ~192 frames total per video — seconds of OCR with the `screenai` backend.
3. Run the existing screenai worker path. Collect `(text, x, y, w, h)` per frame in **original video coordinates** (no crop, no offset translation needed).

## Cluster selection

Use the existing `cluster_heights()` to bucket bbox heights. For each cluster compute:

| Metric | Definition |
|---|---|
| `frame_coverage` | fraction of sampled frames with ≥1 bbox in the cluster |
| `y_dispersion`   | stdev of bbox `y` (lower = more line-locked) |
| `x_spread`       | stdev of bbox centers / median bbox width (higher = real dialogue) |
| `cjk_ratio`      | fraction of bboxes containing target-language characters |
| `size_sanity`    | `1` if median height ∈ `[ih·0.025, ih·0.10]`, else penalize |

Score:

```
score = frame_coverage
      * cjk_ratio
      * size_sanity
      * (x_spread > 0.5 ? 1 : 0.3)
      * 1 / (1 + y_dispersion / median_h)
```

Pick the max. If the top two scores are within 10% **and** their Y medians sit in opposite halves of the frame, print a suggestion that the user re-run with `--crop2` (don't try to auto-emit two rects).

## Crop derivation

Let the winning cluster contain bboxes `C = [(x, y, w, h), ...]` in original-video pixels. Let `H = median(h for _, _, _, _, h in C)` (text height).

### Horizontal extents — controlled by `--auto-crop-width`

The heuristic can miss outlier very-long subtitles that fell outside the sample windows, so always anchor the width on the **widest bbox actually seen in the chosen cluster**, not a percentile.

- **`pad` (default)** — find the cluster's widest bbox `(x*, _, w*, _)`. Extend by 20% of its own width on each side:
  ```
  x1 = max(0,  x* - 0.20 * w*)
  x2 = min(iw, x* + w* + 0.20 * w*)
  ```
- **`full`** — `x1 = 0`, `x2 = iw`. Use when sample windows are likely to miss the longest lines (short videos, sparse dialogue) and the wasted pixels don't matter.

### Vertical extents

```
y1 = min(y for _, y, _, _ in C)
y2 = max(y + h for _, y, _, h in C)
```

**Two-line guard.** The pre-scan can land entirely on frames that only show single-line subtitles. If the cluster's vertical extent only fits one line of text, pad both directions by one text height to leave room for a second line:

```
if (y2 - y1) < 1.8 * H:        # roughly "one line of characters"
    y1 -= H
    y2 += H
y1 = max(0,  y1)
y2 = min(ih, y2)
```

Picking `1.8 * H` (not `2 * H`) tolerates slight OCR jitter on the single-line case without being so generous that a real two-line cluster wrongly expands further.

### Output

Snap `x1, y1, x2, y2` to even pixel boundaries (some ffmpeg codecs require even crop dimensions), then emit:

```
w = x2 - x1
h = y2 - y1
crop = f"{w}:{h}:{x1}:{y1}"
```

## Sanity / fallbacks

- **No cluster passes thresholds** (e.g. silent intro, no subtitles in sample) → fall back to the previous default `iw*0.7:ih*0.15:iw*0.15:ih*0.8` and print why.
- **Resulting crop is >90% of frame area** → likely garbage; fall back to default with a warning.
- **Y is bimodal** (two tight sub-modes) → print suggestion to re-run with `--crop2`, still apply the winning cluster's rect for this run.
- Always print the chosen crop and the cluster summary so the user can override on the next run.

## Cost

~200 OCR frames before the main pass. With `screenai` this is on the order of seconds, and it's strictly cheaper than running the full pipeline against a too-wide default crop (no wasted OCR on dead pixels for the rest of the video).

## Implementation notes

- New function `auto_detect_crop(video_path, languages, width_mode) -> str` in `ocranime.py`.
- Called in `main()` immediately before `extract_frames(...)` (around `ocranime.py:1729`) whenever `args.crop == "auto"`.
- Reuses `cluster_heights()` (`ocranime.py:505`), `_parse_response_lines()`, the screenai worker path, and `_get_video_dimensions()` (`ocranime.py:1363`).
- `--crop`'s `default=` changes from the hard-coded expression to `"auto"`; the existing manual expressions still work because the flag stays a free-form string.
- Help text for `--crop` updates to mention `auto` and document `--auto-crop-width`.

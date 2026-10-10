#!/usr/bin/env python3
"""Extract burned-in subtitles from anime videos into SRT using OCR."""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import atexit
import urllib.request
import urllib.error
from pathlib import Path


MIN_CLIP_DURATION = 0.5  # seconds — clips shorter than this are dropped
MAX_SUBTITLE_DURATION = 7.0  # seconds — subtitles longer than this are capped
SCREENAI_MAX_FRAME_DIMENSION = 2048  # GetMaxImageDimension() for the bundled model


_YT_DLP_PATH_TAG = "__ocranime_path__:"


def _download_via_yt_dlp(url):
    """Download `url` via yt-dlp into cwd and return the local file path.

    Cookies and other site-specific settings are expected to come from the
    user's ~/.yt-dlp.conf — we don't pass any auth flags ourselves.

    yt-dlp's normal status + progress output is streamed straight to the
    terminal; the final filepath is identified by a tagged --print line.
    """
    if not shutil.which("yt-dlp"):
        print("Error: yt-dlp is required for URL inputs but was not found in PATH",
              file=sys.stderr)
        sys.exit(1)

    template = "%(title)s [%(id)s].%(ext)s"
    cmd = [
        "yt-dlp",
        "--newline",  # progress lines use \n so we can stream them
        "--print", f"after_move:{_YT_DLP_PATH_TAG}%(filepath)s",
        "-o", template,
        url,
    ]
    print(f"Downloading {url} ...", flush=True)
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, bufsize=1)
    final_path = None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line.startswith(_YT_DLP_PATH_TAG):
            final_path = line[len(_YT_DLP_PATH_TAG):]
        else:
            print(line, flush=True)
    proc.wait()
    if proc.returncode != 0:
        print(f"Error: yt-dlp failed for {url} (exit {proc.returncode})",
              file=sys.stderr)
        sys.exit(1)
    if not final_path:
        print(f"Error: yt-dlp produced no filepath for {url}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(final_path):
        print(f"Error: yt-dlp reported {final_path!r} but the file is missing",
              file=sys.stderr)
        sys.exit(1)
    return final_path


def _scale_filter(quality):
    """Return an ffmpeg scale filter for a frame-size multiplier."""
    if quality == 1.0:
        return None
    q = f"{quality:.12f}".rstrip("0").rstrip(".")
    return f"scale=iw*{q}:ih*{q}"


def _dimension_clamp_filter(max_dimension):
    """Return an aspect-preserving ffmpeg scale filter that never upscales."""
    if max_dimension is None:
        return None
    limit = int(max_dimension)
    return (
        f"scale=w='min(iw,{limit})':h='min(ih,{limit})':"
        "force_original_aspect_ratio=decrease"
    )


HDR_COLOR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
HDR_TO_SDR_FILTERS = (
    "zscale=transfer=linear:npl=100",
    "format=gbrpf32le",
    "zscale=primaries=bt709",
    "tonemap=tonemap=mobius:desat=0",
    "zscale=transfer=bt709:matrix=bt709:range=tv",
    "format=rgb24",
)


def _video_is_hdr(video_path):
    """Return whether the first video stream uses a PQ or HLG transfer."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=color_transfer",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip().lower() in HDR_COLOR_TRANSFERS


def _append_hdr_to_sdr_filters(filters, video_path):
    """Add a linear-light HDR-to-BT.709 tone map when the input is HDR."""
    if _video_is_hdr(video_path):
        filters.extend(HDR_TO_SDR_FILTERS)


def extract_frames(video_path, tmpdir, crop, fps, quality=1.0, duration=None,
                   max_dimension=None):
    """Extract cropped, size-limited frames from video using ffmpeg."""
    output_pattern = os.path.join(tmpdir, "%06d.png")
    filters = [f"crop={crop}", f"fps={fps}"]
    _append_hdr_to_sdr_filters(filters, video_path)
    if scale := _scale_filter(quality):
        filters.append(scale)
    if clamp := _dimension_clamp_filter(max_dimension):
        filters.append(clamp)
    cmd = [
        "ffmpeg",
        "-i",
        video_path,
    ]
    if duration:
        cmd += ["-t", duration]
    cmd += [
        "-vf",
        ",".join(filters),
        "-loglevel",
        "error",
        output_pattern,
    ]
    subprocess.run(cmd, check=True)
    frames = sorted(Path(tmpdir).glob("*.png"))
    return frames


def extract_frames_crop2(video_path, tmpdir, crop2, fps, quality=1.0,
                         duration=None, max_dimension=None):
    """Extract, stack, and size-limit frames from two crop regions."""
    output_pattern = os.path.join(tmpdir, "%06d.png")
    top_crop, bottom_crop = crop2
    output_filters = [f"[top][bottom]vstack", f"fps={fps}"]
    _append_hdr_to_sdr_filters(output_filters, video_path)
    if scale := _scale_filter(quality):
        output_filters.append(scale)
    if clamp := _dimension_clamp_filter(max_dimension):
        output_filters.append(clamp)
    filter_complex = (
        f"[0:v]crop={top_crop}[top];"
        f"[0:v]crop={bottom_crop}[bottom];"
        f"{','.join(output_filters)}"
    )
    cmd = [
        "ffmpeg",
        "-i",
        video_path,
    ]
    if duration:
        cmd += ["-t", duration]
    cmd += [
        "-filter_complex",
        filter_complex,
        "-loglevel",
        "error",
        output_pattern,
    ]
    subprocess.run(cmd, check=True)
    frames = sorted(Path(tmpdir).glob("*.png"))
    return frames


PREFILTER_EDGE_THRESHOLD = 2.0  # frames below this edge variance are skipped
FRAMEDIFF_THRESHOLD = 5.0  # frames with mean pixel diff below this reuse prev OCR


def _load_frame_gray(frame_path):
    """Load a frame as a grayscale float32 numpy array."""
    from PIL import Image
    import numpy as np

    return np.array(Image.open(frame_path).convert("L"), dtype=np.float32)


def _frame_has_edges(frame_path):
    """Fast heuristic: check if a frame has enough edges to plausibly contain text.

    Computes mean absolute gradient in both axes. Frames with subtitles have
    sharp edges (text outlines) that produce higher values. Frames below
    PREFILTER_EDGE_THRESHOLD are almost certainly empty (solid/gradient background).
    """
    import numpy as np

    img = _load_frame_gray(frame_path)
    dx = np.abs(np.diff(img, axis=1))
    dy = np.abs(np.diff(img, axis=0))
    return float(np.mean(dx) + np.mean(dy)) >= PREFILTER_EDGE_THRESHOLD


def _frames_are_similar(prev_arr, curr_arr):
    """Check if two frames are similar enough to reuse the previous OCR result."""
    import numpy as np

    return float(np.mean(np.abs(curr_arr - prev_arr))) < FRAMEDIFF_THRESHOLD


def build_clips(texts, sample_interval):
    """Build clips (start_idx, end_idx) from consecutive non-empty texts, drop short ones."""
    clips = []
    start = None
    for i, text in enumerate(texts):
        if text and start is None:
            start = i
        elif not text and start is not None:
            clips.append((start, i - 1))
            start = None
    if start is not None:
        clips.append((start, len(texts) - 1))

    # Filter out clips shorter than MIN_CLIP_DURATION
    filtered = []
    for s, e in clips:
        duration = (e - s + 1) * sample_interval
        if duration >= MIN_CLIP_DURATION:
            filtered.append((s, e))
    return filtered


def ocr_frame_apple(frame_path, languages, fast):
    """Run Apple Vision OCR on a single frame, return recognized text."""
    import Vision
    from Foundation import NSData

    data = NSData.dataWithContentsOfFile_(str(frame_path))
    if data is None:
        return ""

    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1 if fast else 0)
    request.setRecognitionLanguages_(languages)

    handler.performRequests_error_([request], None)

    results = request.results()
    if not results:
        return ""

    lines = []
    for obs in results:
        candidate = obs.topCandidates_(1)
        if candidate:
            lines.append(candidate[0].string())

    return "\n".join(lines)


def _apple_ocr_concat(frame_paths, languages):
    """OCR multiple frames in one Vision call by concatenating vertically.

    Returns a list of text strings, one per input frame.  Uses bounding-box
    Y coordinates to assign lines back to their source frame.  Vision uses
    normalised coordinates with Y=0 at the bottom, so we invert when mapping.
    """
    import Vision
    from Foundation import NSData
    from PIL import Image
    import io

    if not frame_paths:
        return []
    if len(frame_paths) == 1:
        return [ocr_frame_apple(frame_paths[0], languages, fast=False).strip()]

    # Load and normalize widths
    images = [Image.open(p) for p in frame_paths]
    target_w = images[0].width
    resized = []
    frame_heights = []
    for img in images:
        if img.width != target_w:
            ratio = target_w / img.width
            img = img.resize((target_w, int(img.height * ratio)), Image.LANCZOS)
        resized.append(img)
        frame_heights.append(img.height)

    separator_h = 20  # black bar between frames to prevent text bleeding
    total_h = sum(frame_heights) + separator_h * (len(resized) - 1)
    concat = Image.new("RGB", (target_w, total_h))  # black by default
    y_off = 0
    boundaries = []
    for img, fh in zip(resized, frame_heights):
        concat.paste(img, (0, y_off))
        boundaries.append((y_off, y_off + fh))
        y_off += fh + separator_h
    for img in images:
        img.close()

    # Convert concat to PNG bytes in memory for Vision
    buf = io.BytesIO()
    concat.save(buf, format="PNG")
    concat.close()
    png_bytes = buf.getvalue()

    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    if data is None:
        return [""] * len(frame_paths)

    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(0)
    request.setRecognitionLanguages_(languages)
    handler.performRequests_error_([request], None)

    results = request.results()
    if not results:
        return [""] * len(frame_paths)

    # Assign each observation to a frame based on bounding box Y
    frame_lines = [[] for _ in frame_paths]
    for obs in results:
        candidate = obs.topCandidates_(1)
        if not candidate:
            continue
        text = candidate[0].string()
        if not text.strip():
            continue
        bbox = obs.boundingBox()
        # Vision Y=0 is bottom; convert to pixel Y from top
        center_y_norm = 1.0 - (bbox.origin.y + bbox.size.height / 2)
        center_y_px = center_y_norm * total_h
        for fi, (ys, ye) in enumerate(boundaries):
            if center_y_px < ye:
                frame_lines[fi].append(text)
                break
        else:
            frame_lines[-1].append(text)

    return ["\n".join(lines) for lines in frame_lines]


def _apple_worker_ocr(task):
    """Worker function: OCR one or more frames with Apple Vision.

    Accepts (chunk_indices, paths, languages).  When multiple paths are given
    the frames are concatenated into a single image for one OCR call.
    """
    chunk_indices, frame_paths, languages = task
    texts = _apple_ocr_concat(frame_paths, languages)
    return chunk_indices, [t.strip() for t in texts]


def _apple_warmup(languages):
    """Warm up a worker by importing Vision framework."""
    import Vision  # noqa: F401
    return True


def detect_text_frames_apple(frames, languages, num_workers=None, batch_size=16):
    """OCR frames using Apple Vision with parallel processes for speed.

    Uses multiprocessing (spawn) so each worker gets its own Vision
    framework instance with full CPU parallelism.  Workers warm up
    while the main process pre-filters frames.

    When batch_size > 1, frames are concatenated into single images so
    each Vision call processes multiple frames at once.
    """
    import numpy as np
    from multiprocessing import get_context

    if num_workers is None:
        num_workers = max(os.cpu_count() or 1, 1)

    # Start worker pool and warm up in background while we pre-filter
    ctx = get_context("spawn")
    pool = ctx.Pool(processes=num_workers)
    warmup_results = [pool.apply_async(_apple_warmup, (languages,)) for _ in range(num_workers)]

    # Pre-filter: skip frames that clearly have no text, and build the OCR
    # plan without retaining one grayscale array per extracted frame.
    print("Pre-filtering frames...", end="", flush=True)
    candidate_count = 0
    reuse_sources = [None] * len(frames)
    ocr_indices = []
    diff_reused = 0
    last_ocr_arr = None
    last_ocr_idx = None
    for idx, frame in enumerate(frames):
        arr = _load_frame_gray(frame)
        dx = np.abs(np.diff(arr, axis=1))
        dy = np.abs(np.diff(arr, axis=0))
        if float(np.mean(dx) + np.mean(dy)) >= PREFILTER_EDGE_THRESHOLD:
            candidate_count += 1
            if last_ocr_arr is not None and _frames_are_similar(last_ocr_arr, arr):
                diff_reused += 1
                reuse_sources[idx] = last_ocr_idx
            else:
                ocr_indices.append(idx)
                last_ocr_arr = arr
                last_ocr_idx = idx
        else:
            last_ocr_arr = None
            last_ocr_idx = None
    edge_skipped = len(frames) - candidate_count
    print(f" {edge_skipped}/{len(frames)} frames skipped (no text edges)")

    # Build per-frame OCR result placeholders.
    texts = [""] * len(frames)

    if diff_reused:
        print(f"Frame differencing will reuse OCR for {diff_reused} frames")

    # Wait for all workers to finish warming up
    for r in warmup_results:
        r.get()

    total_ocr = len(ocr_indices)
    ocr_paths = [str(frames[i]) for i in ocr_indices]

    # Build tasks: chunk frames into batches for concatenated OCR
    bs = max(1, batch_size)
    ocr_tasks = []
    for i in range(0, total_ocr, bs):
        chunk_indices = list(range(i, min(i + bs, total_ocr)))
        chunk_paths = [ocr_paths[j] for j in chunk_indices]
        ocr_tasks.append((chunk_indices, chunk_paths, languages))

    batch_label = f", concat {bs}" if bs > 1 else ""
    print(f"OCR: 0% (0/{total_ocr} frames, {num_workers} workers{batch_label})", end="", flush=True)
    done = 0
    for chunk_indices, chunk_texts in pool.imap_unordered(_apple_worker_ocr, ocr_tasks):
        for ci, text in zip(chunk_indices, chunk_texts):
            texts[ocr_indices[ci]] = text
        done += len(chunk_indices)
        progress = done / total_ocr * 100 if total_ocr else 100
        print(f"\rOCR: {progress:.0f}% ({done}/{total_ocr} frames, {num_workers} workers{batch_label})", end="", flush=True)
    pool.close()
    pool.join()

    # Propagate OCR results to diff-reused frames.
    for idx, source_idx in enumerate(reuse_sources):
        if source_idx is not None:
            texts[idx] = texts[source_idx]

    print()
    if diff_reused:
        print(f"Frame differencing reused OCR for {diff_reused} frames")
    return texts


# ── Chrome Screen AI backend ─────────────────────────────────────────────

_screenai_engine = None


def _load_screenai():
    """Load the Chrome Screen AI native library (cached after first call).

    Model files must already be present at ~/.config/screen_ai/resources/
    (call _ensure_screenai_downloaded() first when running in the main process).
    """
    global _screenai_engine
    if _screenai_engine is not None:
        return _screenai_engine

    import ctypes

    model_dir = Path.home() / ".config" / "screen_ai" / "resources"
    if not model_dir.exists():
        raise RuntimeError(
            "Screen AI model files not found. Run _ensure_screenai_downloaded() first."
        )

    dll_name = "chrome_screen_ai.dll" if sys.platform == "win32" else "libchromescreenai.so"
    dll_mode = os.RTLD_LAZY if hasattr(os, "RTLD_LAZY") else ctypes.DEFAULT_MODE
    lib = ctypes.CDLL(str(model_dir / dll_name), mode=dll_mode)

    # ── ctypes struct definitions (mirrors Skia's SkBitmap) ─────────────
    class SkColorInfo(ctypes.Structure):
        _fields_ = [("fColorSpace", ctypes.c_void_p), ("fColorType", ctypes.c_int32), ("fAlphaType", ctypes.c_int32)]

    class SkISize(ctypes.Structure):
        _fields_ = [("fWidth", ctypes.c_int32), ("fHeight", ctypes.c_int32)]

    class SkImageInfo(ctypes.Structure):
        _fields_ = [("fColorInfo", SkColorInfo), ("fDimensions", SkISize)]

    class SkPixmap(ctypes.Structure):
        _fields_ = [("fPixels", ctypes.c_void_p), ("fRowBytes", ctypes.c_size_t), ("fInfo", SkImageInfo)]

    class SkBitmap(ctypes.Structure):
        _fields_ = [("fPixelRef", ctypes.c_void_p), ("fPixmap", SkPixmap), ("fFlags", ctypes.c_uint32)]

    # ── file-content callbacks (library reads its own weight files) ──────
    @ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_char_p)
    def get_file_content_size(p):
        path = model_dir / p.decode("utf-8")
        return os.path.getsize(path) if path.exists() else 0

    @ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p)
    def get_file_content(p, s, ptr):
        path = model_dir / p.decode("utf-8")
        if path.exists():
            # Screen AI invokes this on native fiber stacks. CPython 3.14 can
            # defer destruction of callback temporaries indefinitely there
            # (python/cpython#157519), retaining whole f.read() model copies.
            # Read directly into the destination owned by the library instead.
            buffer = (ctypes.c_char * s).from_address(ptr)
            with open(path, "rb", buffering=0) as f:
                view = memoryview(buffer).cast("B")
                offset = 0
                while offset < s:
                    count = f.readinto(view[offset:])
                    if not count:
                        raise OSError(f"Unexpected EOF reading Screen AI model: {path}")
                    offset += count

    # ── configure function signatures ───────────────────────────────────
    lib.SetFileContentFunctions.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.InitOCRUsingCallback.restype = ctypes.c_bool
    lib.SetOCRLightMode.argtypes = [ctypes.c_bool]
    lib.PerformOCR.argtypes = [ctypes.POINTER(SkBitmap), ctypes.POINTER(ctypes.c_uint32)]
    lib.PerformOCR.restype = ctypes.c_void_p
    lib.FreeLibraryAllocatedCharArray.argtypes = [ctypes.c_void_p]
    lib.GetMaxImageDimension.restype = ctypes.c_uint32

    lib.SetFileContentFunctions(get_file_content_size, get_file_content)
    lib.InitOCRUsingCallback()
    lib.SetOCRLightMode(False)
    max_pixel_size = lib.GetMaxImageDimension()

    malloc_trim = None
    if sys.platform == "linux":
        # Native inference frees large model/workspace allocations, but glibc
        # can retain over a GiB of unused resident heap in each worker.
        malloc_trim = getattr(ctypes.CDLL(None), "malloc_trim", None)
        if malloc_trim is not None:
            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int

    _screenai_engine = {
        "lib": lib,
        "SkBitmap": SkBitmap,
        "max_pixel_size": max_pixel_size,
        "malloc_trim": malloc_trim,
        # prevent garbage-collection of the callback pointers
        "_cb_size": get_file_content_size,
        "_cb_content": get_file_content,
    }
    print("Chrome Screen AI loaded.", flush=True)
    return _screenai_engine


def _screenai_perform_ocr(engine, img):
    """Run Screen AI OCR on a PIL Image, return parsed protobuf response dict."""
    import ctypes
    from screenai_protos.chrome_screen_ai_pb2 import VisualAnnotation
    from google.protobuf.json_format import MessageToDict

    lib = engine["lib"]
    SkBitmap = engine["SkBitmap"]
    max_px = engine["max_pixel_size"]

    # down-scale if needed
    if any(x > max_px for x in img.size):
        factor = min(max_px / img.width, max_px / img.height)
        img = img.resize((int(img.width * factor), int(img.height * factor)), 3)  # LANCZOS=3

    img_bytes = img.convert("RGBA").tobytes()
    w, h = img.size

    bitmap = SkBitmap()
    bitmap.fPixmap.fPixels = ctypes.cast(ctypes.c_char_p(img_bytes), ctypes.c_void_p)
    bitmap.fPixmap.fRowBytes = w * 4
    bitmap.fPixmap.fInfo.fColorInfo.fColorType = 4
    bitmap.fPixmap.fInfo.fColorInfo.fAlphaType = 1
    bitmap.fPixmap.fInfo.fDimensions.fWidth = w
    bitmap.fPixmap.fInfo.fDimensions.fHeight = h

    output_length = ctypes.c_uint32(0)
    result_ptr = lib.PerformOCR(ctypes.byref(bitmap), ctypes.byref(output_length))
    if not result_ptr:
        return {}

    proto_bytes = ctypes.string_at(result_ptr, output_length.value)
    lib.FreeLibraryAllocatedCharArray(result_ptr)

    annotation = VisualAnnotation()
    annotation.ParseFromString(proto_bytes)
    return MessageToDict(annotation, preserving_proto_field_name=True)


# Lines outside this height range (relative to median) are treated as noise.
# Below: small UI text (watermarks, usernames, timestamps).
# Above: large background text (title cards, signs, logos).
# Keep this broad: ScreenAI's bbox heights can be bimodal even for legitimate
# subtitles in one video (for example, modes near 40px and 80px).
_BBOX_HEIGHT_RATIO_MIN = 0.5
_BBOX_HEIGHT_RATIO_MAX = 1.75


def _parse_response_lines(response_lines):
    """Parse OCR response lines into (text, bbox) tuples."""
    parsed = []
    for line in response_lines:
        text = line.get("utf8_string", "").strip()
        if not text:
            continue
        bbox = line.get("bounding_box", {})
        parsed.append((text, bbox))
    return parsed


def _compute_line_height_range(all_heights):
    """Compute allowed line height range from observed bbox heights.

    Since most OCR lines are subtitles (consistent height), the median height
    represents the subtitle size.  Returns (min_h, max_h) thresholds.
    Returns (0, 0) if there aren't enough lines to decide.
    """
    if len(all_heights) < 2:
        return 0, 0
    sorted_h = sorted(all_heights)
    median_h = sorted_h[len(sorted_h) // 2]
    if median_h <= 0:
        return 0, 0
    return median_h * _BBOX_HEIGHT_RATIO_MIN, median_h * _BBOX_HEIGHT_RATIO_MAX


def cluster_heights(all_heights):
    """Split bbox heights into clusters at valleys in a smoothed histogram.

    Builds an integer histogram of heights, smooths it with a Gaussian kernel
    whose width scales with the mean height, then walks the smoothed curve to
    locate peaks (local maxima) and splits at the local minimum between each
    pair of adjacent peaks.  This handles dense, continuous distributions that
    a pure gap-based rule cannot separate.  Clusters with fewer than
    max(5, 5% of samples) members are dropped as noise.  Returns a list of
    (min_h, max_h) tuples sorted by ascending height.
    """
    if not all_heights:
        return []
    import numpy as np

    sorted_h = sorted(all_heights)
    max_h = sorted_h[-1]
    if max_h <= 0:
        return []

    hist = np.zeros(max_h + 2, dtype=np.float64)
    for h in all_heights:
        if h > 0:
            hist[h] += 1

    mean_h = sum(sorted_h) / len(sorted_h)
    sigma = max(2.0, mean_h * 0.06)
    radius = int(sigma * 3) + 1
    kx = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-(kx ** 2) / (2 * sigma * sigma))
    kernel /= kernel.sum()
    smoothed = np.convolve(hist, kernel, mode="same")

    peaks = [
        i for i in range(1, len(smoothed) - 1)
        if smoothed[i] > smoothed[i - 1] and smoothed[i] > smoothed[i + 1]
    ]
    if peaks:
        peak_threshold = 0.05 * max(smoothed[p] for p in peaks)
        peaks = [p for p in peaks if smoothed[p] >= peak_threshold]

    boundaries = []
    for left, right in zip(peaks, peaks[1:]):
        boundaries.append(left + int(np.argmin(smoothed[left:right + 1])))

    cluster_edges = [0] + boundaries + [max_h + 1]
    clusters = []
    j = 0
    for lo, hi in zip(cluster_edges, cluster_edges[1:]):
        members = []
        while j < len(sorted_h) and sorted_h[j] < hi:
            if sorted_h[j] >= lo:
                members.append(sorted_h[j])
            j += 1
        if members:
            clusters.append(members)

    min_size = max(5, len(all_heights) * 0.05)
    return [(c[0], c[-1]) for c in clusters if len(c) >= min_size]


def summarize_clusters(frame_lines, cluster_ranges, sample_interval):
    """For each cluster, return dict with first_text and on-screen duration.

    `first_text` is the merged text built from all cluster bboxes in the
    earliest frame in which the cluster appears.  `duration` counts each frame
    that contains at least one cluster bbox (sample_interval per frame).
    """
    out = []
    for cmin, cmax in cluster_ranges:
        first_text = ""
        frames_with = 0
        for lines in frame_lines:
            in_cluster = [b for b in lines if cmin <= b[4] <= cmax]
            if not in_cluster:
                continue
            frames_with += 1
            if not first_text:
                texts = _build_texts_from_frame_lines([in_cluster], 1, 0, 0)
                first_text = texts[0].replace("\n", " / ")
        out.append({
            "min_h": cmin,
            "max_h": cmax,
            "first_text": first_text,
            "duration": frames_with * sample_interval,
        })
    return out


def filter_height_cluster(frame_lines, cmin, cmax):
    """Keep only bboxes with height in [cmin, cmax].  Returns a new list."""
    return [[b for b in lines if cmin <= b[4] <= cmax] for lines in frame_lines]


def _screenai_ocr_image(engine, img):
    """Run Screen AI OCR on a PIL Image, return list of (text, x, y, w, h) tuples."""
    response = _screenai_perform_ocr(engine, img)
    return [(text, bbox.get("x", 0), bbox.get("y", 0),
             bbox.get("width", 0), bbox.get("height", 0))
            for text, bbox in _parse_response_lines(response.get("lines", []))]


def _screenai_ocr_concat(engine, images, close_images=False):
    """OCR multiple images in one call by concatenating them vertically.

    Returns a list of per-frame line data.  Each element is a list of
    ``(text, height)`` tuples — one per detected OCR line assigned to that
    frame.  No height filtering is applied here; the caller is responsible
    for computing thresholds globally and filtering.

    Uses bounding-box Y coordinates from the protobuf response to assign
    lines to their source frame within the concatenated image.

    If the concatenated height would exceed max_pixel_size, the batch is split
    into sub-batches that each fit within the limit to avoid quality-destroying
    downscale.

    When close_images is true, consume owned images and release each completed
    sub-batch immediately instead of retaining decoded pixels for the whole task.
    """
    from PIL import Image

    if not images:
        return []
    if len(images) == 1:
        return [_screenai_ocr_image(engine, images[0])]

    max_px = engine["max_pixel_size"]
    target_w = images[0].width

    # Normalize widths and collect heights
    resized = []
    frame_heights = []
    for img in images:
        if img.width != target_w:
            ratio = target_w / img.width
            img = img.resize((target_w, int(img.height * ratio)), 3)
        resized.append(img)
        frame_heights.append(img.height)

    # Split into sub-batches that fit within max_pixel_size
    sub_batches = []  # list of (start_idx, end_idx) into resized/frame_heights
    batch_start = 0
    batch_h = 0
    for i, fh in enumerate(frame_heights):
        if batch_h + fh > max_px and i > batch_start:
            sub_batches.append((batch_start, i))
            batch_start = i
            batch_h = 0
        batch_h += fh
    sub_batches.append((batch_start, len(resized)))

    all_results = [[] for _ in images]  # list of list of (text, height)

    for sb_start, sb_end in sub_batches:
        sb_images = resized[sb_start:sb_end]
        sb_heights = frame_heights[sb_start:sb_end]

        if len(sb_images) == 1:
            try:
                all_results[sb_start] = _screenai_ocr_image(engine, sb_images[0])
            finally:
                if close_images:
                    sb_images[0].close()
                    images[sb_start].close()
                    if trim := engine.get("malloc_trim"):
                        trim(0)
            continue

        total_h = sum(sb_heights)
        concat = Image.new("RGB", (target_w, total_h))
        y_off = 0
        boundaries = []
        try:
            for img, fh in zip(sb_images, sb_heights):
                concat.paste(img, (0, y_off))
                boundaries.append((y_off, y_off + fh))
                y_off += fh

            response = _screenai_perform_ocr(engine, concat)
        finally:
            concat.close()
            if close_images:
                for img in sb_images + images[sb_start:sb_end]:
                    img.close()
                if trim := engine.get("malloc_trim"):
                    trim(0)

        # Assign each line to a frame based on bounding box Y center
        frame_lines = [[] for _ in sb_images]
        for text, bbox in _parse_response_lines(response.get("lines", [])):
            bx = bbox.get("x", 0)
            by = bbox.get("y", 0)
            bw = bbox.get("width", 0)
            bh = bbox.get("height", 0)
            line_y = by + bh / 2
            for fi, (ys, ye) in enumerate(boundaries):
                if line_y < ye:
                    # Adjust Y to be relative to the source frame
                    frame_lines[fi].append((text, bx, by - ys, bw, bh))
                    break
            else:
                frame_lines[-1].append((text, bx, by - boundaries[-1][0], bw, bh))

        for i, lines in enumerate(frame_lines):
            all_results[sb_start + i] = lines

    return all_results


def ocr_frame_screenai(frame_path, languages, fast):
    """Run Chrome Screen AI OCR on a single frame, return recognized text."""
    from PIL import Image

    _ensure_screenai_downloaded()
    engine = _load_screenai()
    img = Image.open(frame_path)
    lines = _screenai_ocr_image(engine, img)
    img.close()
    return "\n".join(text for text, *_ in lines)


def _screenai_worker_ocr(task):
    """Worker function: OCR one or more frames, return (indices, frame_lines).

    Accepts (indices, paths) tuple.  When multiple paths are given the frames
    are concatenated into a single image for one PerformOCR call, and results
    are split back by bounding-box Y coordinate.

    Returns (indices, frame_lines) where frame_lines is a list of
    list-of-(text, height) per frame.
    """
    indices, frame_paths = task
    from PIL import Image
    # Suppress noisy native-library logging in workers
    if not getattr(_screenai_worker_ocr, "_stderr_suppressed", False):
        _devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_devnull_fd, 2)
        _screenai_worker_ocr._stderr_suppressed = True
    engine = _load_screenai()
    images = [Image.open(p) for p in frame_paths]
    try:
        frame_lines = _screenai_ocr_concat(engine, images, close_images=True)
        return indices, frame_lines
    finally:
        for img in images:
            img.close()
        if trim := engine.get("malloc_trim"):
            trim(0)



def _ensure_screenai_protos():
    """Generate screenai_protos Python bindings if missing."""
    proto_pkg = Path(__file__).parent / "screenai_protos"
    pb2_file = proto_pkg / "chrome_screen_ai_pb2.py"
    proto_file = proto_pkg / "chrome_screen_ai.proto"
    if pb2_file.exists():
        return
    if not proto_file.exists():
        sys.exit("Error: screenai_protos/chrome_screen_ai.proto not found")
    project_root = str(Path(__file__).parent)
    # Try grpc_tools first (pure Python, no system dependency)
    try:
        from grpc_tools import protoc as grpc_protoc
        rc = grpc_protoc.main(["grpc_tools.protoc", f"--proto_path={project_root}",
                               f"--python_out={project_root}",
                               str(proto_file.relative_to(project_root))])
        if rc == 0 and pb2_file.exists():
            return
    except ImportError:
        pass
    # Fall back to system protoc
    protoc_bin = shutil.which("protoc")
    if protoc_bin:
        try:
            subprocess.run([protoc_bin, f"--proto_path={project_root}",
                            f"--python_out={project_root}",
                            str(proto_file.relative_to(project_root))],
                           check=True, capture_output=True)
            if pb2_file.exists():
                return
        except subprocess.CalledProcessError:
            pass
    sys.exit("Error: could not generate screenai protobuf bindings. "
             "Install protoc (e.g. brew install protobuf) or "
             "pip install grpcio-tools, then retry.")


def _ensure_screenai_downloaded():
    """Download Screen AI model files if needed (call before spawning workers)."""
    import platform

    _ensure_screenai_protos()

    model_dir = Path.home() / ".config" / "screen_ai" / "resources"
    if model_dir.exists():
        return

    print("Downloading Chrome Screen AI model files...", flush=True)

    os_name = platform.system().lower()
    arch = platform.machine().lower()
    if os_name == "darwin":
        os_name = "mac"
    if arch in ("x86_64", "amd64"):
        arch = "amd64"
    elif arch in ("aarch64", "arm64"):
        arch = "arm64"
    elif arch in ("x86", "i386", "i686"):
        arch = "386"

    cipd_platform = "linux" if os_name == "linux" else f"{os_name}-{arch}"
    package_name = f"chromium/third_party/screen-ai/{cipd_platform}"
    ensure_content = f"{package_name} latest\n"

    with tempfile.TemporaryDirectory() as td:
        cipd_bin = "cipd.exe" if sys.platform == "win32" else "cipd"
        cipd_path = os.path.join(td, cipd_bin)
        cipd_client_platform = f"{os_name}-{arch}"
        cipd_url = f"https://chrome-infra-packages.appspot.com/client?platform={cipd_client_platform}&version=latest"
        try:
            urllib.request.urlretrieve(cipd_url, cipd_path)
            if sys.platform != "win32":
                os.chmod(cipd_path, 0o755)
        except Exception as exc:
            sys.exit(f"Error: failed to download CIPD client: {exc}")
        target_path = model_dir.parent
        cmd = [cipd_path, "export", "-root", str(target_path), "-ensure-file", "-"]
        try:
            subprocess.run(cmd, input=ensure_content, text=True, check=True)
        except Exception as exc:
            sys.exit(f"Error: failed to download Screen AI files: {exc}")

    print("Screen AI model files downloaded.", flush=True)


def _screenai_warmup(_=None):
    """Warm up a worker by loading the Screen AI engine and suppressing stderr."""
    if not getattr(_screenai_warmup, "_stderr_suppressed", False):
        _devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_devnull_fd, 2)
        _screenai_warmup._stderr_suppressed = True
    _load_screenai()
    return True


def _cjk_space_to_ideo(s):
    """Replace ASCII spaces with ideographic space unless both neighbors are ASCII."""
    return re.sub(
        r'(?<![A-Za-z0-9]) | (?![A-Za-z0-9])',
        '\u3000', s)


_SAMELINE_Y_THRESH = 10  # max Y difference to consider bboxes on same line
_SAMELINE_X_GAP = 50     # max horizontal gap between adjacent bboxes


def _build_texts_from_frame_lines(frame_lines, num_frames, min_h, max_h):
    """Build per-frame text strings from raw bbox data, merging same-line bboxes."""
    texts = [""] * num_frames
    for idx, lines in enumerate(frame_lines):
        kept = []
        for text, x, y, w, h in lines:
            if min_h > 0 and h > 0 and (h < min_h or h > max_h):
                continue
            kept.append((text, x, y, w, h))
        if not kept:
            texts[idx] = ""
            continue
        # Sort by x so left-to-right order is correct
        kept.sort(key=lambda b: b[1])
        # Group bboxes on the same visual line (similar Y coordinate)
        row_groups = []  # list of lists of (text, x, y, w, h)
        for box in kept:
            merged = False
            for group in row_groups:
                ref_y = group[0][2]  # Y of first box in group
                if abs(box[2] - ref_y) <= _SAMELINE_Y_THRESH:
                    group.append(box)
                    merged = True
                    break
            if not merged:
                row_groups.append([box])
        # Sort groups top-to-bottom by average Y
        row_groups.sort(key=lambda g: sum(b[2] for b in g) / len(g))
        # Within each group, merge horizontally adjacent bboxes
        merged_lines = []
        for group in row_groups:
            group.sort(key=lambda b: b[1])  # sort by x within group
            parts = [_cjk_space_to_ideo(group[0][0])]
            for i in range(1, len(group)):
                prev = group[i - 1]
                cur = group[i]
                gap = cur[1] - (prev[1] + prev[3])  # cur_x - (prev_x + prev_w)
                if gap <= _SAMELINE_X_GAP:
                    parts.append(_cjk_space_to_ideo(cur[0]))
                else:
                    # Too far apart, treat as separate segment
                    merged_lines.append("\u3000".join(parts))
                    parts = [_cjk_space_to_ideo(cur[0])]
            merged_lines.append("\u3000".join(parts))
        texts[idx] = "\n".join(merged_lines)
    return texts


# Dense 1440p video measured up to ~1.5 GiB per native OCR worker. Leave room
# for inference peaks, decoded pixels, and activity elsewhere in the system.
SCREENAI_WORKER_MEMORY_BYTES = 2 * 1024**3
SCREENAI_MEMORY_HEADROOM_BYTES = 1024**3


def _screenai_available_memory():
    """Return Linux available RAM, bounded by remaining cgroup v2 budgets."""
    try:
        available = next(
            int(line.split()[1]) * 1024
            for line in Path('/proc/meminfo').read_text().splitlines()
            if line.startswith('MemAvailable:')
        )
    except (OSError, ValueError, StopIteration):
        return None

    try:
        relative = next(
            line.split('::', 1)[1]
            for line in Path('/proc/self/cgroup').read_text().splitlines()
            if line.startswith('0::')
        )
        root = Path('/sys/fs/cgroup')
        group = (root / relative.lstrip('/')).resolve()
        for parent in (group, *group.parents):
            if not parent.is_relative_to(root):
                break
            limit = (parent / 'memory.max').read_text().strip()
            if limit != 'max':
                used = int((parent / 'memory.current').read_text())
                available = min(available, max(0, int(limit) - used))
    except (OSError, ValueError, StopIteration):
        pass
    return available


def detect_text_frames_screenai(frames, languages, num_workers=None, batch_size=1):
    """OCR frames using Chrome Screen AI with parallel processes for speed.

    Each worker process loads its own copy of the native library to avoid
    thread-safety issues.  Pre-filtering and frame-differencing reduce the
    total number of OCR calls.

    When batch_size > 1, frames are concatenated into single images so each
    PerformOCR call processes multiple frames at once, reducing native library
    call overhead.  Workers are started and warmed up in parallel with
    pre-filtering so the two costly startup phases overlap.
    """
    import numpy as np
    from multiprocessing import get_context

    if num_workers is None:
        num_workers = max(os.cpu_count() or 1, 1)
        if (available := _screenai_available_memory()) is not None:
            budget = max(0, available - SCREENAI_MEMORY_HEADROOM_BYTES)
            memory_workers = max(1, budget // SCREENAI_WORKER_MEMORY_BYTES)
            if memory_workers < num_workers:
                num_workers = memory_workers
                print(f"Screen AI: {num_workers} workers within the available "
                      f"{available / 1024**3:.1f} GiB RAM budget", flush=True)

    # Ensure model files are downloaded before spawning workers
    _ensure_screenai_downloaded()

    # Start worker pool and warm up engines in background while we pre-filter
    ctx = get_context("spawn")
    pool = ctx.Pool(processes=num_workers)
    warmup_results = [pool.apply_async(_screenai_warmup) for _ in range(num_workers)]

    # Pre-filter: skip frames that clearly have no text and build the OCR plan
    # without retaining one grayscale array per extracted frame. This runs in
    # the main process concurrently with worker engine loading.
    print("Pre-filtering frames...", end="", flush=True)
    candidate_count = 0
    reuse_sources = [None] * len(frames)
    ocr_indices = []
    diff_reused = 0
    last_ocr_arr = None
    last_ocr_idx = None
    for idx, frame in enumerate(frames):
        arr = _load_frame_gray(frame)
        dx = np.abs(np.diff(arr, axis=1))
        dy = np.abs(np.diff(arr, axis=0))
        if float(np.mean(dx) + np.mean(dy)) >= PREFILTER_EDGE_THRESHOLD:
            candidate_count += 1
            if last_ocr_arr is not None and _frames_are_similar(last_ocr_arr, arr):
                diff_reused += 1
                reuse_sources[idx] = last_ocr_idx
            else:
                ocr_indices.append(idx)
                last_ocr_arr = arr
                last_ocr_idx = idx
        else:
            last_ocr_arr = None
            last_ocr_idx = None
    edge_skipped = len(frames) - candidate_count
    print(f" {edge_skipped}/{len(frames)} frames skipped (no text edges)")

    # Build per-frame OCR result placeholders.
    frame_lines = [[] for _ in frames]  # list of (text, height) per frame

    if diff_reused:
        print(f"Frame differencing will reuse OCR for {diff_reused} frames")

    # Wait for all workers to finish warming up before submitting OCR tasks
    for r in warmup_results:
        r.get()

    total_ocr = len(ocr_indices)
    ocr_paths = [str(frames[i]) for i in ocr_indices]

    # Build tasks: chunk frames into batches for concatenated OCR
    bs = max(1, batch_size)
    ocr_tasks = []
    for i in range(0, total_ocr, bs):
        chunk_indices = list(range(i, min(i + bs, total_ocr)))
        chunk_paths = [ocr_paths[j] for j in chunk_indices]
        ocr_tasks.append((chunk_indices, chunk_paths))

    batch_label = f", concat {bs}" if bs > 1 else ""
    print(f"OCR: 0% (0/{total_ocr} frames, {num_workers} workers{batch_label})", end="", flush=True)
    done = 0
    for task_indices, task_frame_lines in pool.imap_unordered(_screenai_worker_ocr, ocr_tasks):
        for ti, lines in zip(task_indices, task_frame_lines):
            frame_lines[ocr_indices[ti]] = lines
        done += len(task_indices)
        progress = done / total_ocr * 100 if total_ocr else 100
        print(f"\rOCR: {progress:.0f}% ({done}/{total_ocr} frames, {num_workers} workers{batch_label})", end="", flush=True)
    pool.close()
    pool.join()

    # Propagate OCR results to diff-reused frames.
    for idx, source_idx in enumerate(reuse_sources):
        if source_idx is not None:
            frame_lines[idx] = frame_lines[source_idx]

    print()
    if diff_reused:
        print(f"Frame differencing reused OCR for {diff_reused} frames")

    # Compute bbox height threshold from ALL frames, weighted by frame count.
    # Each height is counted once per frame it appears in (including
    # diff-propagated frames), so persistent subtitle text dominates over
    # transient noise.
    all_heights = []
    for lines in frame_lines:
        for _text, _x, _y, _w, h in lines:
            if h > 0:
                all_heights.append(h)
    min_h, max_h = _compute_line_height_range(all_heights)
    if all_heights:
        sorted_h = sorted(all_heights)
        median_h = sorted_h[len(sorted_h) // 2]
        print(f"Bbox filter: median_h={median_h:.0f} keep=[{min_h:.0f}, {max_h:.0f}] ({len(all_heights)} samples)")

    texts = _build_texts_from_frame_lines(frame_lines, len(frames), min_h, max_h)
    return texts, frame_lines


def format_timestamp(seconds):
    """Format seconds as SRT timestamp HH:MM:SS,mmm."""
    # Round once before splitting the fields so 999.999... ms carries into
    # the next second instead of producing the invalid four-digit value 1000.
    total_ms = int(round(seconds * 1000))
    h, remainder = divmod(total_ms, 3_600_000)
    m, remainder = divmod(remainder, 60_000)
    s, ms = divmod(remainder, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _tokenize_cjk(text):
    """Split text into tokens: each CJK character is its own token,
    consecutive non-CJK characters form a single token."""
    tokens = []
    buf = []
    for c in text:
        if "\u4E00" <= c <= "\u9FFF" or "\uF900" <= c <= "\uFAFF" or "\U00020000" <= c <= "\U0002A6DF":
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(c)
        else:
            buf.append(c)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _edit_distance(a, b):
    """Compute Levenshtein edit distance on CJK-aware tokens.

    Each CJK character (Simplified/Traditional) counts as one token.
    Consecutive non-CJK characters are grouped into a single token.
    This way 'is正在殺時間' and '正在殺時間' differ by 1 (the 'is' run),
    but 'is正在殺is時間' and '正在殺時間' differ by 2.
    """
    ta = _tokenize_cjk(a)
    tb = _tokenize_cjk(b)
    if len(ta) < len(tb):
        ta, tb = tb, ta
    if not tb:
        return len(ta)
    prev = list(range(len(tb) + 1))
    for i, ca in enumerate(ta):
        curr = [i + 1]
        for j, cb in enumerate(tb):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


_OUTER_BRACKET_PAIRS = [
    ("（", "）"), ("(", ")"),
    ("【", "】"), ("《", "》"),
    ("「", "」"), ("『", "』"),
    ("〈", "〉"), ("＜", "＞"),
    ("〔", "〕"), ("｛", "｝"),
    ("﹁", "﹂"), ("﹃", "﹄"),
]


def _strip_outer_parens(text):
    """Strip outer brackets/parentheses for comparison."""
    for opener, closer in _OUTER_BRACKET_PAIRS:
        if text.startswith(opener) and text.endswith(closer):
            return text[1:-1]
    return text


def deduplicate(entries, max_dist=0):
    """Merge consecutive entries with identical (or near-identical) text."""
    if not entries:
        return []

    merged = []
    current_text, current_start, current_end = entries[0]

    for text, start, end in entries[1:]:
        if text == current_text:
            current_end = end
        elif max_dist > 0:
            # Compare with outer parentheses stripped so e.g.
            # "ひ在殺時間" vs "（正在殺時間）" can merge (distance on inner text)
            a = _strip_outer_parens(current_text)
            b = _strip_outer_parens(text)
            if _edit_distance(a, b) <= max_dist:
                # Keep the longer (likely more accurate) text
                if len(text) > len(current_text):
                    current_text = text
                current_end = end
            else:
                merged.append((current_text, current_start, current_end))
                current_text, current_start, current_end = text, start, end
        else:
            merged.append((current_text, current_start, current_end))
            current_text, current_start, current_end = text, start, end

    merged.append((current_text, current_start, current_end))
    return merged


def _texts_similar(a, b):
    """Check if two texts are similar enough to merge a single-frame glitch."""
    # Containment: one text is a substring of the other (handles extra OCR noise)
    if a in b or b in a:
        return True
    # Edit distance with generous threshold
    max_len = max(len(a), len(b))
    threshold = max(2, int(max_len * 0.4))
    if _edit_distance(a, b) <= threshold:
        return True
    return False


def smart_deduplicate(entries, sample_interval):
    """Merge single-frame entries into a neighbor when texts are similar.

    If a subtitle spans only one frame and is similar to the previous or next
    subtitle, it is likely an OCR glitch — merge it into the neighbor.
    """
    if not entries:
        return []

    n = len(entries)
    skip = set()

    # Forward pass: merge single-frame entries into previous
    merged = []
    current_text, current_start, current_end = entries[0]

    for i, (text, start, end) in enumerate(entries[1:], 1):
        duration = round(end - start, 6)
        is_single_frame = abs(duration - sample_interval) < 1e-4
        if is_single_frame and _texts_similar(text, current_text):
            current_end = end
            # Prefer the longer text — OCR often catches fewer characters in
            # some frames (e.g. an edge character drops out), and the longer
            # variant is more likely the correct transcription.
            if len(text) > len(current_text):
                current_text = text
            skip.add(i)
            continue
        merged.append((current_text, current_start, current_end))
        current_text, current_start, current_end = text, start, end

    merged.append((current_text, current_start, current_end))

    # Backward pass: merge remaining single-frame entries into next
    result = []
    i = 0
    while i < len(merged):
        text, start, end = merged[i]
        duration = round(end - start, 6)
        is_single_frame = abs(duration - sample_interval) < 1e-4
        if is_single_frame and i + 1 < len(merged):
            next_entry_text = merged[i + 1][0]
            if _texts_similar(text, next_entry_text):
                # Extend next entry's start to absorb this one and prefer
                # whichever variant has more characters (see forward pass).
                chosen_text = text if len(text) > len(next_entry_text) else next_entry_text
                merged[i + 1] = (chosen_text, start, merged[i + 1][2])
                i += 1
                continue
        result.append((text, start, end))
        i += 1

    return result




# ---------------------------------------------------------------------------
# Rules-based cleanup (no LLM)
# ---------------------------------------------------------------------------

# Characters that legitimately appear in CJK subtitles.  Anything outside
# this set is considered OCR noise and stripped from each line.
_SUBTITLE_WHITELIST_RE = re.compile(
    r"["
    r"\u0020-\u007E"       # ASCII (letters, digits, basic punctuation)
    r"\u00C0-\u024F"       # Latin Extended (accented letters)
    r"\u2000-\u206F"       # General Punctuation (—, –, ', ', ", ", …, etc.)
    r"\u2150-\u218F"       # Number Forms (fractions)
    r"\u2190-\u21FF"       # Arrows
    r"\u22EF"              # ⋯ midline ellipsis
    r"\u2500-\u257F"       # Box Drawing
    r"\u3000-\u303F"       # CJK Symbols & Punctuation (　、。〈〉《》「」『』【】〜)
    r"\u3040-\u309F"       # Hiragana
    r"\u30A0-\u30FF"       # Katakana
    r"\u4E00-\u9FFF"       # CJK Unified Ideographs
    r"\uF900-\uFAFF"       # CJK Compatibility Ideographs
    r"\uFE30-\uFE4F"       # CJK Compatibility Forms
    r"\uFF00-\uFFEF"       # Halfwidth & Fullwidth Forms (！＂＃, ０-９, Ａ-Ｚ, etc.)
    r"\U00020000-\U0002A6DF"  # CJK Unified Ideographs Extension B (rare chars)
    r"]"
)

# Hiragana U+3040-309F, Katakana U+30A0-30FF
_KANA_RE = re.compile(r"[\u3040-\u30FF]")


def _is_mostly_kana(text):
    """Return True if text is predominantly Japanese kana/punctuation."""
    stripped = re.sub(r"[\s.,!?。、！？…⋯\-()（）「」『』【】]", "", text)
    if not stripped:
        return True
    kana_count = sum(1 for c in stripped if "\u3040" <= c <= "\u30FF")
    return kana_count / len(stripped) >= 0.5


def _strip_non_whitelist(text):
    """Remove characters not in the subtitle whitelist."""
    return "".join(c for c in text if _SUBTITLE_WHITELIST_RE.match(c))


def _fix_punctuation(text):
    """Fix common OCR punctuation errors."""
    # Normalize various ellipsis forms to ⋯
    text = re.sub(r"\.{2,}", "⋯", text)
    text = re.sub(r"。{2,}", "⋯", text)
    text = re.sub(r"…+", "⋯", text)
    # .0, .00, …00 etc. at end → ⋯  (Screen AI reads ⋯ as .00 sometimes)
    text = re.sub(r"[.…⋯]0+$", "⋯", text)
    # 。0 or trailing 0 at end of sentence → ⋯
    text = re.sub(r"。0", "⋯", text)
    # •。 or ·。 or stray dot combinations → ⋯
    text = re.sub(r"[•·]。", "⋯", text)
    text = re.sub(r"[•·]{2,}", "⋯", text)
    # Strip trailing punctuation after ⋯ (e.g. ⋯。 ⋯. ⋯, → ⋯)
    text = re.sub(r"⋯[.。,，、;；!！?？]+", "⋯", text)
    # Normalize curly quotes: treat any pair of U+201C/U+201D (in any combo)
    # as matched quotes — OCR often uses the same codepoint for both.
    curly_count = text.count("\u201C") + text.count("\u201D")
    if curly_count == 2:
        # Matched pair — normalize first to U+201C, second to U+201D
        first = True
        chars = []
        for c in text:
            if c in "\u201C\u201D":
                chars.append("\u201C" if first else "\u201D")
                first = False
            else:
                chars.append(c)
        text = "".join(chars)
    elif curly_count == 1:
        # Stray single curly quote as hesitation marker → ⋯
        text = text.replace("\u201C", "⋯").replace("\u201D", "⋯")
    # Stray single straight quote as hesitation marker (e.g. 我"我 → 我⋯我)
    if text.count('"') == 1:
        text = text.replace('"', "⋯")

    # Normalize half-width punctuation to full-width in CJK context
    # Comma: CJK,CJK or CJK, CJK → CJK，CJK
    text = re.sub(
        r"(?<=[\u3000-\u9FFF\uF900-\uFAFF\U00020000-\U0002A6DF]),\s?(?=[\u3000-\u9FFF\uF900-\uFAFF\U00020000-\U0002A6DF])",
        "，",
        text,
    )
    # Parentheses containing CJK → full-width
    text = re.sub(
        r"\(([^)]*[\u3000-\u9FFF\uF900-\uFAFF\U00020000-\U0002A6DF][^)]*)\)",
        r"（\1）",
        text,
    )
    # Straight quotes containing CJK → fullwidth quotes
    text = re.sub(
        r"\"([^\"]*[\u3000-\u9FFF\uF900-\uFAFF\U00020000-\U0002A6DF][^\"]*?)\"",
        r"「\1」",
        text,
    )
    # Curly quotes containing CJK → fullwidth quotes
    text = re.sub(
        r"\u201C([^\u201D]*[\u3000-\u9FFF\uF900-\uFAFF\U00020000-\U0002A6DF][^\u201D]*?)\u201D",
        r"「\1」",
        text,
    )
    # Strip orphan brackets/parens without matching counterpart
    for opener, closer in _OUTER_BRACKET_PAIRS:
        if closer in text and opener not in text:
            text = text.replace(closer, "")
        if opener in text and closer not in text:
            text = text.replace(opener, "")
    return text


def _clean_line(line):
    """Clean a single subtitle line. Returns cleaned line or None to remove."""
    line = line.strip()
    if not line:
        return None

    # Strip characters outside the subtitle whitelist (Arabic, Cyrillic, etc.)
    line = _strip_non_whitelist(line).strip()
    if not line:
        return None

    # Pure kana line → remove
    if _is_mostly_kana(line):
        return None

    # Fix punctuation
    line = _fix_punctuation(line)

    return line if line else None


def _eval_ffmpeg_expr(expr, iw, ih):
    """Evaluate an ffmpeg-style coordinate expression with iw/ih bound.

    Supports +, -, *, /, parentheses, integer/float literals, and the
    variables iw and ih (image width/height).  Anything else raises.
    """
    import ast
    tree = ast.parse(expr.strip(), mode="eval")
    allowed_nodes = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError(f"Disallowed expression element {type(node).__name__!r} in {expr!r}")
        if isinstance(node, ast.Name) and node.id not in ("iw", "ih"):
            raise ValueError(f"Unknown variable {node.id!r} in {expr!r} (only iw, ih allowed)")
    return eval(compile(tree, "<ffmpeg-expr>", "eval"), {"iw": iw, "ih": ih})


def parse_blind_spot(spec, frame_width, frame_height):
    """Parse an ffmpeg-style 'w:h:x:y' spec into a (x1, y1, x2, y2) pixel rect.

    iw/ih in the spec refer to the supplied frame_width/frame_height.  Returns
    pixel coordinates in that same reference frame; callers must translate to
    bbox coordinates if they differ.
    """
    parts = spec.split(":")
    if len(parts) != 4:
        raise ValueError(f"--blind-spot must be 'w:h:x:y' (got {spec!r})")
    w_e, h_e, x_e, y_e = parts
    w = _eval_ffmpeg_expr(w_e, frame_width, frame_height)
    h = _eval_ffmpeg_expr(h_e, frame_width, frame_height)
    x = _eval_ffmpeg_expr(x_e, frame_width, frame_height)
    y = _eval_ffmpeg_expr(y_e, frame_width, frame_height)
    return (x, y, x + w, y + h)


def _parse_crop_offsets(crop_spec, video_width, video_height):
    """Parse a 'w:h:x:y' crop spec and return (crop_x, crop_y) in pixels."""
    parts = crop_spec.split(":")
    if len(parts) != 4:
        raise ValueError(f"Cannot parse --crop spec for blind-spot translation: {crop_spec!r}")
    _w_e, _h_e, x_e, y_e = parts
    return (
        _eval_ffmpeg_expr(x_e, video_width, video_height),
        _eval_ffmpeg_expr(y_e, video_width, video_height),
    )


def _get_video_dimensions(video_path):
    """Return (width, height) of the first video stream via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=,",
         str(video_path)],
        check=True, capture_output=True, text=True,
    )
    w, h = result.stdout.strip().split(",")
    return int(w), int(h)


def _get_video_duration(video_path):
    """Return duration in seconds of the first video stream via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


DEFAULT_CROP = "iw*0.7:ih*0.15:iw*0.15:ih*0.8"


def _is_target_char(ch, languages):
    """Return True if ch belongs to the target language's writing system."""
    # CJK Unified Ideographs — shared by Chinese & Japanese
    if "一" <= ch <= "鿿":
        return True
    if "㐀" <= ch <= "䶿":   # CJK Extension A
        return True
    if "豈" <= ch <= "﫿":   # CJK Compatibility Ideographs
        return True
    if "\U00020000" <= ch <= "\U0002A6DF":  # CJK Extension B
        return True
    for lang in languages:
        low = lang.lower()
        if low.startswith("ja") and "぀" <= ch <= "ヿ":
            return True
        if low.startswith("ko") and "가" <= ch <= "힯":
            return True
        if low.startswith("en") and (("A" <= ch <= "Z") or ("a" <= ch <= "z")):
            return True
    return False


def auto_detect_crop(video_path, languages, width_mode, num_workers=None):
    """Auto-detect ffmpeg crop region by pre-scanning the video.

    Returns an ffmpeg crop string ``w:h:x:y``.  Falls back to ``DEFAULT_CROP``
    when no plausible subtitle cluster is found.
    """
    import numpy as np

    K = 6
    WINDOW_DUR = 8
    fps = 4

    try:
        iw, ih = _get_video_dimensions(video_path)
        duration = _get_video_duration(video_path)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"auto-crop: could not read video metadata ({exc}); using default crop")
        return DEFAULT_CROP, None

    # Pick K windows from the middle 80% so OP/ED don't bias the clusters
    span_start = duration * 0.10
    span_end = duration * 0.90
    span = max(0.0, span_end - span_start)
    if span <= WINDOW_DUR:
        # Very short video — sample once from the start of the middle 80% region
        starts = [max(0.0, span_start)]
    else:
        slot = span / K
        offset = max(0.0, (slot - WINDOW_DUR) / 2)
        starts = [span_start + i * slot + offset for i in range(K)]

    # Commentary videos can use a different subtitle size/layout in the intro.
    if starts[0] > WINDOW_DUR:
        starts.insert(0, 0.0)

    print(f"auto-crop: pre-scanning {len(starts)} windows × {WINDOW_DUR}s @ {fps}fps "
          f"(video {iw}x{ih}, duration {duration:.0f}s)...")
    pre_scan_filters = [f"fps={fps}"]
    _append_hdr_to_sdr_filters(pre_scan_filters, video_path)
    pre_scan_filters.append(_dimension_clamp_filter(SCREENAI_MAX_FRAME_DIMENSION))

    with tempfile.TemporaryDirectory(prefix="ocranime_autocrop_") as tmpdir:
        for window_idx, start_t in enumerate(starts):
            cmd = [
                "ffmpeg",
                "-ss", f"{start_t:.3f}",
                "-i", str(video_path),
                "-t", str(WINDOW_DUR),
                "-vf", ",".join(pre_scan_filters),
                "-loglevel", "error",
                os.path.join(tmpdir, f"w{window_idx:02d}_%06d.png"),
            ]
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as exc:
                print(f"auto-crop: ffmpeg failed on window {window_idx} (t={start_t:.1f}s): {exc}")

        frames = sorted(Path(tmpdir).glob("*.png"))
        if not frames:
            print("auto-crop: no frames extracted; using default crop")
            return DEFAULT_CROP, None

        from PIL import Image
        with Image.open(frames[0]) as frame:
            sx, sy = iw / frame.width, ih / frame.height
        _, frame_lines = detect_text_frames_screenai(
            frames, languages, num_workers=num_workers, batch_size=64,
        )

    # Screen AI returns coordinates in the resized image, not the source video.
    frame_lines = [
        [(text, x * sx, y * sy, w * sx, int(round(h * sy)))
         for text, x, y, w, h in lines]
        for lines in frame_lines
    ]
    # Dense browser UI/chat can erase subtitle peaks in the global histogram.
    # Cluster plausible subtitle sizes first; retain the old fallback for
    # videos whose subtitles are unusually small or large.
    plausible_lines = [
        [b for b in lines if ih * 0.025 <= b[4] <= ih * 0.10]
        for lines in frame_lines
    ]
    if sum(map(len, plausible_lines)) >= 5:
        frame_lines = plausible_lines

    all_h = [h for lines in frame_lines for _, _, _, _, h in lines if h > 0]
    if not all_h:
        print("auto-crop: pre-scan found no text; using default crop")
        return DEFAULT_CROP, None
    cluster_ranges = cluster_heights(all_h)
    if not cluster_ranges:
        print("auto-crop: no height clusters formed; using default crop")
        return DEFAULT_CROP, None

    num_frames = len(frame_lines)

    def _densest_y_band(bboxes, band_h):
        """Pick the densest Y window of height ``band_h`` and return its bboxes.

        Within a single height cluster the bboxes may span multiple Y regions
        (e.g. a top-of-frame banner with the same character height as the
        bottom subtitle band).  The genuine subtitle band is the densest
        single Y window; filter to that so the rest of the heuristic measures
        the subtitles, not a mash-up.
        """
        if not bboxes:
            return bboxes
        ys = sorted(b[2] for b in bboxes)
        best_count = 0
        best_lo = ys[0]
        i = 0
        for j in range(len(ys)):
            while ys[j] - ys[i] > band_h:
                i += 1
            count = j - i + 1
            if count > best_count:
                best_count = count
                best_lo = ys[i]
        best_hi = best_lo + band_h
        return [b for b in bboxes if best_lo <= b[2] <= best_hi]

    cluster_data = []
    for cmin, cmax in cluster_ranges:
        bboxes = []
        for fi, lines in enumerate(frame_lines):
            for text, x, y, w, h in lines:
                if cmin <= h <= cmax:
                    bboxes.append((text, x, y, w, h, fi))
        if not bboxes:
            continue

        heights_all = np.array([b[4] for b in bboxes], dtype=np.float64)
        median_h_all = float(np.median(heights_all))
        # Allow two subtitle baselines with normal line spacing, without
        # mixing the band with nearby article paragraphs or live-stream chat.
        band_h = max(8.0, 1.5 * median_h_all)
        band_bboxes = _densest_y_band(bboxes, band_h)
        if not band_bboxes:
            band_bboxes = bboxes

        frames_with = {b[5] for b in band_bboxes}
        frame_coverage = len(frames_with) / num_frames
        ys = np.array([b[2] for b in band_bboxes], dtype=np.float64)
        widths = np.array([b[3] for b in band_bboxes], dtype=np.float64)
        heights = np.array([b[4] for b in band_bboxes], dtype=np.float64)
        x_centers = np.array([b[1] + b[3] / 2 for b in band_bboxes], dtype=np.float64)

        median_h = float(np.median(heights))
        median_w = float(np.median(widths))
        y_dispersion = float(np.std(ys))
        # Kept for diagnostics only — the spec's "stdev of centers / median width"
        # wrongly punishes centred subtitles whose centres all sit at iw/2.
        x_spread = float(np.std(x_centers)) / median_w if median_w > 0 else 0.0
        y_median = float(np.median(ys))

        target_count = 0
        total_count = 0
        for b in band_bboxes:
            for c in b[0]:
                if c.isspace():
                    continue
                total_count += 1
                if _is_target_char(c, languages):
                    target_count += 1
        cjk_ratio = target_count / total_count if total_count > 0 else 0.0

        size_sanity = 1.0 if (ih * 0.025 <= median_h <= ih * 0.10) else 0.3
        y_disp_factor = 1.0 / (1.0 + y_dispersion / median_h) if median_h > 0 else 0.0
        # Subtitles overwhelmingly live in the lower half of the frame.  Treat
        # y_median / ih as a soft prior: full credit at the bottom, gentle
        # penalty at the top.  Clamped to keep the score from collapsing to 0.
        bottom_bias = max(0.3, min(1.0, y_median / ih))
        score = frame_coverage * cjk_ratio * size_sanity * y_disp_factor * bottom_bias

        cluster_data.append({
            "cmin": cmin, "cmax": cmax, "score": score,
            "bboxes": [(b[0], b[1], b[2], b[3], b[4]) for b in band_bboxes],
            "frame_coverage": frame_coverage, "cjk_ratio": cjk_ratio,
            "x_spread": x_spread, "y_dispersion": y_dispersion,
            "median_h": median_h, "y_median": y_median,
        })

    if not cluster_data:
        print("auto-crop: no scorable clusters; using default crop")
        return DEFAULT_CROP, None

    cluster_data.sort(key=lambda d: d["score"], reverse=True)
    print(f"auto-crop: {len(cluster_data)} cluster(s):", flush=True)
    for i, d in enumerate(cluster_data):
        print(f"  [{i}] h=[{d['cmin']},{d['cmax']}] score={d['score']:.3f} "
              f"coverage={d['frame_coverage']:.2f} cjk={d['cjk_ratio']:.2f} "
              f"x_spread={d['x_spread']:.2f} y_disp={d['y_dispersion']:.1f} "
              f"median_h={d['median_h']:.0f} y_median={d['y_median']:.0f} "
              f"({len(d['bboxes'])} bboxes)", flush=True)

    winner = cluster_data[0]
    if winner["score"] <= 0:
        print("auto-crop: top cluster scored zero; using default crop")
        return DEFAULT_CROP, None

    if len(cluster_data) >= 2:
        top1, top2 = cluster_data[0], cluster_data[1]
        if top2["score"] > 0 and (top1["score"] - top2["score"]) / top1["score"] < 0.10:
            if (top1["y_median"] < ih / 2) != (top2["y_median"] < ih / 2):
                print("auto-crop: top two clusters are close-scoring and sit on opposite "
                      "halves of the frame — consider re-running with --crop2.")

    C = winner["bboxes"]
    H = winner["median_h"]

    if width_mode == "full":
        x1, x2 = 0.0, float(iw)
    else:  # "pad"
        # Pad the widest *seen* bbox by 20% per side, but enforce a floor of
        # 70% of frame width centred on the cluster's horizontal centre.
        # Pre-scan windows only sample ~5% of the video, so the widest seen
        # bbox routinely undershoots the true widest subtitle; without the
        # floor, long lines get truncated at the edges.
        widest = max(C, key=lambda b: b[3])
        wx, ww = widest[1], widest[3]
        pad_x1 = wx - 0.20 * ww
        pad_x2 = wx + ww + 0.20 * ww
        centre_x = float(np.median([b[1] + b[3] / 2 for b in C]))
        floor_w = 0.70 * iw
        floor_x1 = centre_x - floor_w / 2
        floor_x2 = centre_x + floor_w / 2
        x1 = max(0.0, min(pad_x1, floor_x1))
        x2 = min(float(iw), max(pad_x2, floor_x2))

    y1 = float(min(b[2] for b in C))
    y2 = float(max(b[2] + b[4] for b in C))
    if (y2 - y1) < 1.8 * H:
        y1 -= H
        y2 += H
    y1 = max(0.0, y1)
    y2 = min(float(ih), y2)

    # Snap to even pixels (some codecs require even crop dimensions)
    x1_i = int(x1) - (int(x1) % 2)
    y1_i = int(y1) - (int(y1) % 2)
    x2_i = int(x2) + (int(x2) % 2)
    y2_i = int(y2) + (int(y2) % 2)
    x1_i = max(0, x1_i)
    y1_i = max(0, y1_i)
    x2_i = min(iw, x2_i)
    y2_i = min(ih, y2_i)

    w = x2_i - x1_i
    h = y2_i - y1_i

    if w <= 0 or h <= 0:
        print("auto-crop: derived crop is empty; using default crop")
        return DEFAULT_CROP, None
    if w * h > 0.9 * iw * ih:
        print(f"auto-crop: derived crop {w}x{h} covers >90% of frame; using default crop")
        return DEFAULT_CROP, None

    crop = f"{w}:{h}:{x1_i}:{y1_i}"
    print(f"auto-crop: selected cluster h=[{winner['cmin']},{winner['cmax']}] -> crop={crop}")
    return crop, (int(winner["cmin"]), int(winner["cmax"]))


def print_crop_recommendations(recommendations):
    """Print reusable --crop arguments selected by auto-crop."""
    if not recommendations:
        return
    print()
    if len(recommendations) == 1:
        _, crop = recommendations[0]
        print(f'Recommended crop filter: --crop "{crop}"')
        return
    print("Recommended crop filters:")
    for video_path, crop in recommendations:
        print(f'  {video_path}: --crop "{crop}"')


def filter_blind_spot_bboxes(frame_lines, rect):
    """Drop bboxes wholly contained inside rect=(x1, y1, x2, y2).

    A bbox that extends outside the rect (even partially) is kept.  Returns a
    new frame_lines list; originals are unchanged.
    """
    x1, y1, x2, y2 = rect
    removed = 0
    filtered = []
    for lines in frame_lines:
        kept = []
        for entry in lines:
            _text, bx, by, bw, bh = entry
            if bx >= x1 and by >= y1 and bx + bw <= x2 and by + bh <= y2:
                removed += 1
                continue
            kept.append(entry)
        filtered.append(kept)
    print(f"Blind-spot filter: removed {removed} bboxes inside rect=({x1:.0f},{y1:.0f})-({x2:.0f},{y2:.0f})")
    return filtered


def _build_watermark_charset(watermark_text):
    """Build a set of normalised characters from watermark text for fuzzy matching."""
    chars = set()
    for ch in watermark_text:
        if ch.isspace():
            continue
        chars.add(ch.lower() if ch.isascii() else ch)
    return chars


_WATERMARK_MIN_CHARS = 3      # line must have at least this many matching chars
_WATERMARK_OVERLAP_RATIO = 0.5  # at least this fraction of line chars must match


def _bbox_matches_watermark(text, watermark_charset):
    """Return True if a bbox text has high character overlap with the watermark."""
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return False
    matches = sum(
        1 for ch in chars
        if (ch.lower() if ch.isascii() else ch) in watermark_charset
    )
    return matches >= _WATERMARK_MIN_CHARS and matches / len(chars) >= _WATERMARK_OVERLAP_RATIO


_WATERMARK_Y_TOLERANCE = 20  # pixels around median Y to consider watermark position


def filter_watermark_bboxes(frame_lines, watermark_charset):
    """Remove watermark bboxes from frame_lines using character overlap + Y position.

    1. Scan all bboxes across all frames to find those matching the watermark charset.
    2. Compute the median Y of matching bboxes — this is the watermark's stable position.
    3. Remove bboxes that match BOTH character overlap AND are near that Y position.

    Returns filtered frame_lines (new list, originals unchanged).
    """
    # Pass 1: find median Y of watermark-matching bboxes
    wm_ys = []
    for lines in frame_lines:
        for text, _x, y, _w, _h in lines:
            if _bbox_matches_watermark(text, watermark_charset):
                wm_ys.append(y)
    if not wm_ys:
        return frame_lines
    wm_ys.sort()
    median_y = wm_ys[len(wm_ys) // 2]
    removed = 0

    # Pass 2: filter bboxes matching both charset and Y position
    filtered = []
    for lines in frame_lines:
        kept = []
        for entry in lines:
            text, _x, y, _w, _h = entry
            if (_bbox_matches_watermark(text, watermark_charset)
                    and abs(y - median_y) <= _WATERMARK_Y_TOLERANCE):
                removed += 1
                continue
            kept.append(entry)
        filtered.append(kept)
    print(f"Watermark filter: removed {removed} bboxes near y={median_y:.0f}")
    return filtered


def cleanup_rules(entries, languages=None):
    """Clean OCR artifacts using deterministic rules. Returns filtered entries."""
    if not entries:
        return entries

    if languages is None:
        languages = ["zh-Hant"]

    result = []
    for text, start, end in entries:
        lines = text.split("\n")
        cleaned_lines = []
        for line in lines:
            cleaned = _clean_line(line)
            if cleaned is not None:
                cleaned_lines.append(cleaned)

        if not cleaned_lines:
            continue

        new_text = "\n".join(cleaned_lines)
        result.append((new_text, start, end))

    return result




def fill_clip_gaps(entries, clips, sample_interval):
    """Extend subtitles so every frame inside detected clips is covered.

    After LLM cleanup some entries may be removed, leaving gaps within clips
    that were determined to contain subtitles.  For each uncovered frame,
    extend the previous subtitle's end time to cover it.
    """
    if not entries:
        return entries

    # Build the set of timestamps (as frame indices) that must be covered
    covered_times = set()
    for s, e in clips:
        for idx in range(s, e + 1):
            covered_times.add(round(idx * sample_interval, 6))

    # For each required timestamp, check if any entry covers it
    result = list(entries)
    for t in sorted(covered_times):
        t_rounded = round(t, 6)
        # Find if any entry covers this timestamp
        covered = False
        for text, start, end in result:
            if round(start, 6) <= t_rounded < round(end, 6):
                covered = True
                break
        if not covered:
            # Find the last entry that ends at or before this timestamp
            best = None
            for i, (text, start, end) in enumerate(result):
                if round(end, 6) <= t_rounded:
                    best = i
            if best is not None:
                text, start, end = result[best]
                new_end = round(t + sample_interval, 6)
                if new_end - start <= MAX_SUBTITLE_DURATION:
                    result[best] = (text, start, new_end)

    return result


def cap_durations(entries, max_duration=MAX_SUBTITLE_DURATION):
    """Clamp subtitle durations to a maximum length."""
    result = []
    for text, start, end in entries:
        if end - start > max_duration:
            end = start + max_duration
        result.append((text, start, end))
    return result


def _write_bbox_csv(csv_path, frame_lines, sample_interval):
    """Write per-line bounding box data to CSV, sorted by height.

    Aggregates identical (text, bbox) tuples across consecutive frames into
    a single row with a duration column.  ``frame_lines`` is the raw per-frame
    list of (text, x, y, w, h) tuples *before* height filtering.
    """
    import csv

    # Aggregate: for each unique (text, x, y, w, h), track first/last frame
    # We group consecutive runs so the same text reappearing later gets its
    # own row.
    rows = []  # (text, x, y, w, h, start_idx, end_idx)
    prev_keys = {}  # (text, x, y, w, h) -> row index in rows (for current run)
    for idx, lines in enumerate(frame_lines):
        current_keys = set()
        for text, x, y, w, h in lines:
            key = (text, round(x), round(y), round(w), round(h))
            current_keys.add(key)
            if key in prev_keys:
                # Extend existing run
                rows[prev_keys[key]] = (*key, rows[prev_keys[key]][5], idx)
            else:
                prev_keys[key] = len(rows)
                rows.append((*key, idx, idx))
        # Remove keys that didn't appear this frame (run ended)
        for key in list(prev_keys):
            if key not in current_keys:
                del prev_keys[key]

    # Convert to final format with duration, sort by height
    csv_rows = []
    for text, x, y, w, h, start_idx, end_idx in rows:
        duration = (end_idx - start_idx + 1) * sample_interval
        csv_rows.append((text, x, y, w, h, round(duration, 3)))
    csv_rows.sort(key=lambda r: r[4])  # sort by bbh

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["line", "bbx", "bby", "bbw", "bbh", "duration"])
        for text, x, y, w, h, duration in csv_rows:
            writer.writerow([text, x, y, w, h, duration])
    print(f"Written {len(csv_rows)} bbox entries to {csv_path}")


def write_srt(entries, output_path):
    """Write entries as SRT file."""
    with open(output_path, "w", encoding="utf-8") as f:
        for i, (text, start, end) in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(start)} --> {format_timestamp(end)}\n")
            f.write(f"{text}\n\n")


def _ffprobe_streams(video_path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", video_path],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(proc.stdout).get("streams", [])


def _format_duration_seconds(video_path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", video_path],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return float(proc.stdout.strip())


def _duration_to_seconds_str(duration):
    if duration is None:
        return None
    if ":" not in duration:
        return duration
    total = 0.0
    for part in duration.split(":"):
        total = total * 60 + float(part)
    return str(total)


def _pgssub_streams(video_path):
    return [
        s for s in _ffprobe_streams(video_path)
        if s.get("codec_type") == "subtitle"
        and s.get("codec_name") in {"hdmv_pgs_subtitle", "pgssub"}
    ]


def _select_pgssub_stream(video_path, requested_index=None):
    pgssubs = _pgssub_streams(video_path)
    if requested_index is not None:
        for s in pgssubs:
            if s.get("index") == requested_index:
                return s
        raise SystemExit(f"Error: stream #{requested_index} is not a PGSSUB subtitle track")
    return pgssubs[0] if pgssubs else None


def _stream_output_path(output_path, stream):
    root, ext = os.path.splitext(output_path)
    tags = stream.get("tags") or {}
    lang = (tags.get("language") or "und").lower()
    return f"{root}.pgssub-{stream['index']}-{lang}{ext or '.srt'}"


def _first_video_fps(video_path):
    streams = _ffprobe_streams(video_path)
    for s in streams:
        if s.get("codec_type") == "video":
            fps = s.get("avg_frame_rate") or s.get("r_frame_rate")
            if fps and fps not in {"0/0", "N/A"}:
                return fps
    raise SystemExit(f"Error: could not determine video FPS for {video_path}")


def _probe_sup_start_and_size(sup_path):
    streams = _ffprobe_streams(sup_path)
    if not streams:
        raise SystemExit(f"Error: could not probe extracted SUP: {sup_path}")
    s = streams[0]
    width = s.get("width")
    height = s.get("height")
    start = s.get("start_time") or "0"
    if not width or not height:
        raise SystemExit(f"Error: could not determine PGSSUB canvas size from {sup_path}")
    return float(start), int(width), int(height)


def _burn_pgssub_to_black_video(video_path, stream_index, output_path, duration_limit=None):
    """Extract a PGS subtitle stream and burn it onto a black video."""
    output_path = str(output_path)
    sup_path = os.path.splitext(output_path)[0] + ".sup"
    duration = _duration_to_seconds_str(duration_limit) or str(_format_duration_seconds(video_path))
    fps = _first_video_fps(video_path)

    print(f"Extracting PGSSUB stream #0:{stream_index} to {sup_path}")
    extract_cmd = ["ffmpeg", "-hide_banner", "-y"]
    if duration_limit:
        extract_cmd += ["-t", duration_limit]
    extract_cmd += ["-i", video_path, "-map", f"0:{stream_index}", "-c:s", "copy", sup_path]
    subprocess.run(extract_cmd, check=True)
    if not os.path.exists(sup_path) or os.path.getsize(sup_path) == 0:
        print(f"No PGSSUB packets extracted for stream #0:{stream_index}; writing empty result")
        return None, sup_path

    sub_start, width, height = _probe_sup_start_and_size(sup_path)
    size = f"{width}x{height}"
    print(f"Burning PGSSUB onto black {size} @ {fps}: {output_path}")
    print(f"Applying subtitle start offset: {sub_start:.3f}s")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={size}:r={fps}:d={duration}",
            "-itsoffset", str(sub_start), "-i", sup_path,
            "-filter_complex", "[0:v][1:s:0]overlay=format=auto",
            "-t", str(duration), "-an",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            output_path,
        ],
        check=True,
    )
    return output_path, sup_path


def main():
    global PREFILTER_EDGE_THRESHOLD
    parser = argparse.ArgumentParser(
        description="Extract hardsubs from anime video to SRT"
    )
    parser.add_argument("video", nargs="+", help="Input video file path(s) or URL(s) (URLs are fetched via yt-dlp)")
    parser.add_argument(
        "--scan-backend",
        default="screenai",
        choices=["apple", "screenai"],
        help="OCR backend: apple (macOS Vision) or screenai (Chrome Screen AI) (default: screenai)",
    )
    parser.add_argument("-o", "--output", help="Output SRT path (default: <video>.srt), or output folder when processing multiple files")
    crop_group = parser.add_mutually_exclusive_group()
    crop_group.add_argument(
        "--crop",
        default="auto",
        help=(f"ffmpeg crop filter, or 'auto' to derive it from a pre-scan "
              f"(default: auto; manual fallback: {DEFAULT_CROP})"),
    )
    crop_group.add_argument(
        "--crop2",
        nargs=2,
        metavar=("TOP_CROP", "BOTTOM_CROP"),
        help="Two ffmpeg crop filters (top and bottom regions) vstacked together",
    )
    parser.add_argument(
        "--auto-crop-width",
        default="pad",
        choices=["pad", "full"],
        help=("How --crop=auto sets horizontal extents: 'pad' fits the widest "
              "bbox and adds 20%% on each side, with a floor of 70%% of the frame "
              "width centred on the cluster; 'full' uses the whole frame width "
              "(default: pad)"),
    )
    parser.add_argument(
        "--fps", type=int, default=4, help="Frames per second to sample (default: 4)"
    )
    parser.add_argument(
        "--duration",
        help="Limit processing duration, e.g. 00:20:00 (useful for tests/samples)",
    )
    parser.add_argument(
        "--quality",
        type=float,
        default=1.0,
        help="Scale extracted frame width and height by this factor (default: 1.0; 0.5 halves both dimensions)",
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        default=["zh-Hant"],
        help="OCR languages (default: zh-Hant)",
    )
    parser.add_argument(
        "--cleanup-backend",
        default="rules",
        choices=["none", "rules"],
        help="Cleanup backend (default: rules): rules (local, no LLM), none (skip)",
    )
    parser.add_argument(
        "--scan-batch-size",
        type=int,
        default=None,
        help="Batch size for apple/screenai backends (default: 16 for apple, 64 for screenai)",
    )
    parser.add_argument(
        "--scan-threads",
        type=int,
        default=None,
        help="Worker processes (default: CPU count, capped by available RAM for Screen AI)",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only generate subtitle frame PNGs in scanned_frames/, skip SRT generation",
    )
    parser.add_argument(
        "--prefilter-edge-threshold",
        type=float,
        default=None,
        help=f"Edge threshold for skipping frames before OCR (default: {PREFILTER_EDGE_THRESHOLD}; PGSSUB pipeline default: 0.1)",
    )
    parser.add_argument(
        "--bbox-csv",
        default=None,
        help="Write per-line bounding box data to a CSV file (line,bbx,bby,bbw,bbh,duration)",
    )
    parser.add_argument(
        "--watermark",
        metavar="TEXT",
        help="Watermark text to remove (fuzzy character-overlap matching catches OCR-garbled variants)",
    )
    parser.add_argument(
        "--blind-spot",
        metavar="W:H:X:Y",
        help="Ignore bboxes wholly contained inside this region (ffmpeg-style 'w:h:x:y' with iw/ih relative to the original video frame; not supported with --crop2)",
    )
    parser.add_argument(
        "--filter-cluster",
        metavar="biggest|N",
        help="Keep only bboxes from a single height cluster: 'biggest' picks the cluster with the largest height, or pass an integer index (0 = smallest height cluster, sorted ascending)",
    )
    parser.add_argument(
        "--raw-srt",
        action="store_true",
        help="Also write a <output>.raw.srt with the pre-cleanup entries",
    )
    parser.add_argument(
        "--no-pgssub-pipeline",
        action="store_true",
        help="Disable automatic PGSSUB extraction/burn-to-black OCR pipeline",
    )
    parser.add_argument(
        "--pgssub-stream",
        type=int,
        default=None,
        help="Only OCR this global PGSSUB stream index. By default, all PGSSUB tracks are OCRed.",
    )
    parser.add_argument(
        "--keep-pgssub-video",
        action="store_true",
        help="Keep the temporary black video created for PGSSUB OCR",
    )
    parser.add_argument(
        "--pgssub-video-output",
        default=None,
        help="Write the black PGSSUB video here and keep it (single-input only)",
    )
    args = parser.parse_args()

    if args.blind_spot and args.crop2:
        parser.error("--blind-spot is not supported with --crop2")

    if not math.isfinite(args.quality) or args.quality <= 0:
        parser.error("--quality must be a finite number greater than 0")

    if args.filter_cluster and args.filter_cluster != "biggest":
        try:
            n = int(args.filter_cluster)
            if n < 0:
                raise ValueError
        except ValueError:
            parser.error(f"--filter-cluster must be 'biggest' or a non-negative integer (got {args.filter_cluster!r})")

    scan_only_incompatible = []
    if args.scan_only:
        if args.output is not None:
            scan_only_incompatible.append("--output")
        if args.languages != ["zh-Hant"]:
            scan_only_incompatible.append("--languages")
        if scan_only_incompatible:
            parser.error(
                f"--scan-only is incompatible with: {', '.join(scan_only_incompatible)}"
            )

    multi = len(args.video) > 1
    if multi and args.pgssub_video_output is not None:
        parser.error("--pgssub-video-output is only supported with a single input")
    if args.pgssub_video_output is not None and args.pgssub_stream is None:
        parser.error("--pgssub-video-output requires --pgssub-stream when OCRing all PGSSUB tracks")
    if multi and args.output is not None and not os.path.isdir(args.output):
        os.makedirs(args.output, exist_ok=True)

    # Resolve inputs: existing local files pass through; anything else is
    # handed to yt-dlp, which lets users pass URLs or bare IDs (e.g. a
    # YouTube/Bilibili video ID) without a scheme prefix.
    args.video = [
        entry if os.path.isfile(entry) else _download_via_yt_dlp(entry)
        for entry in args.video
    ]

    crop_recommendations = []

    for file_idx, video_path in enumerate(args.video):
        if multi:
            print(f"\n{'='*60}")
            print(f"[{file_idx + 1}/{len(args.video)}] {video_path}")
            print(f"{'='*60}")

        if multi and args.output is not None:
            basename = os.path.splitext(os.path.basename(video_path))[0] + ".srt"
            output_path = os.path.join(args.output, basename)
        else:
            output_path = args.output or (os.path.splitext(video_path)[0] + ".srt")

        if not args.no_pgssub_pipeline and args.pgssub_stream is None:
            pgssub_streams = _pgssub_streams(video_path)
            if pgssub_streams:
                print(f"Found {len(pgssub_streams)} PGSSUB subtitle track(s); OCRing all of them")
                for stream in pgssub_streams:
                    stream_output = _stream_output_path(output_path, stream)
                    child_cmd = [
                        sys.executable, os.path.abspath(__file__), video_path,
                        "-o", stream_output,
                        "--pgssub-stream", str(stream["index"]),
                        "--scan-backend", args.scan_backend,
                        "--fps", str(args.fps),
                        "--quality", str(args.quality),
                        "--cleanup-backend", args.cleanup_backend,
                        "--auto-crop-width", args.auto_crop_width,
                    ]
                    if args.duration:
                        child_cmd += ["--duration", args.duration]
                    if args.languages:
                        child_cmd += ["--languages", *args.languages]
                    if args.scan_batch_size is not None:
                        child_cmd += ["--scan-batch-size", str(args.scan_batch_size)]
                    if args.scan_threads is not None:
                        child_cmd += ["--scan-threads", str(args.scan_threads)]
                    if args.watermark:
                        child_cmd += ["--watermark", args.watermark]
                    if args.filter_cluster:
                        child_cmd += ["--filter-cluster", args.filter_cluster]
                    if args.raw_srt:
                        child_cmd.append("--raw-srt")
                    if args.keep_pgssub_video:
                        child_cmd.append("--keep-pgssub-video")
                    if args.prefilter_edge_threshold is not None:
                        child_cmd += ["--prefilter-edge-threshold", str(args.prefilter_edge_threshold)]
                    tags = stream.get("tags") or {}
                    print(f"\n--- PGSSUB stream #0:{stream['index']} ({tags.get('language', '')}) -> {stream_output} ---")
                    subprocess.run(child_cmd, check=True)
                continue

        crop2 = args.crop2
        pgssub_cleanup_dir = None
        pgssub_pipeline_used = False
        if not args.no_pgssub_pipeline:
            pgssub_stream = _select_pgssub_stream(video_path, args.pgssub_stream)
            if pgssub_stream is not None:
                stream_index = pgssub_stream["index"]
                tags = pgssub_stream.get("tags") or {}
                title = tags.get("title", "")
                lang = tags.get("language", "")
                print(f"Using PGSSUB OCR pipeline with stream #0:{stream_index} ({lang}) {title}".rstrip())
                if args.pgssub_video_output:
                    pgssub_video = args.pgssub_video_output
                elif args.keep_pgssub_video:
                    pgssub_video = os.path.splitext(output_path)[0] + ".pgssub-black.mp4"
                else:
                    pgssub_cleanup_dir = tempfile.mkdtemp(prefix="ocranime_pgssub_")
                    atexit.register(lambda p=pgssub_cleanup_dir: shutil.rmtree(p, ignore_errors=True))
                    pgssub_video = os.path.join(pgssub_cleanup_dir, "pgssub-black.mp4")
                os.makedirs(os.path.dirname(os.path.abspath(pgssub_video)), exist_ok=True)
                video_path, sup_path = _burn_pgssub_to_black_video(video_path, stream_index, pgssub_video, args.duration)
                if video_path is None:
                    if args.scan_only:
                        print("Scan-only mode: no subtitle packets in selected PGSSUB stream.")
                    else:
                        write_srt([], output_path)
                        print(f"Written 0 subtitle entries to {output_path}")
                    if pgssub_cleanup_dir:
                        shutil.rmtree(pgssub_cleanup_dir, ignore_errors=True)
                    continue
                pgssub_pipeline_used = True
                # The generated video is just black + subtitles, so OCR the full frame.
                crop2 = None
                if args.crop2:
                    print("Ignoring --crop2 because PGSSUB pipeline uses the full generated subtitle canvas")
            elif args.pgssub_stream is not None:
                # _select_pgssub_stream exits for invalid requested streams; this is defensive.
                raise SystemExit(f"Error: stream #{args.pgssub_stream} is not a PGSSUB subtitle track")

        if args.prefilter_edge_threshold is not None:
            PREFILTER_EDGE_THRESHOLD = args.prefilter_edge_threshold
        elif pgssub_pipeline_used:
            PREFILTER_EDGE_THRESHOLD = 0.1

        # Prepare scanned_frames directory (empty it on each run)
        scanned_dir = Path("scanned_frames")
        if scanned_dir.exists():
            shutil.rmtree(scanned_dir)
        scanned_dir.mkdir()

        fps = args.fps
        sample_interval = 1.0 / fps
        quality = args.quality

        crop = "iw:ih:0:0" if pgssub_pipeline_used else args.crop
        auto_picked_cluster = None  # (cmin, cmax) of auto-crop cluster in extracted-frame coords
        if not crop2 and crop == "auto":
            crop, auto_picked_cluster = auto_detect_crop(
                video_path, args.languages, args.auto_crop_width,
                num_workers=args.scan_threads,
            )
            crop_recommendations.append((video_path, crop))

        with tempfile.TemporaryDirectory(prefix="ocranime_") as tmpdir:
            quality_label = f", quality {quality:g}x" if quality != 1.0 else ""
            max_dimension = None
            dimension_label = ""
            video_width, video_height = _get_video_dimensions(video_path)
            if args.scan_backend == "screenai":
                max_dimension = SCREENAI_MAX_FRAME_DIMENSION
                if max(video_width, video_height) * quality > max_dimension:
                    dimension_label = f", max {max_dimension}px"
            print(f"Extracting frames ({fps} fps{quality_label}{dimension_label})...")
            if crop2:
                frames = extract_frames_crop2(
                    video_path, tmpdir, crop2, fps, quality, args.duration,
                    max_dimension=max_dimension,
                )
            else:
                frames = extract_frames(
                    video_path, tmpdir, crop, fps, quality, args.duration,
                    max_dimension=max_dimension,
                )
            coordinate_scale = quality
            if frames:
                from PIL import Image
                with Image.open(frames[0]) as first_frame:
                    frame_width, frame_height = first_frame.size
                print(f"Extracted {len(frames)} frames ({frame_width}x{frame_height})")

                # Bboxes use post-clamp frame coordinates. Derive their actual
                # scale relative to the crop rather than assuming --quality was
                # the only resize applied.
                if not crop2:
                    try:
                        crop_width_expr = crop.split(":", 1)[0]
                        crop_width = _eval_ffmpeg_expr(
                            crop_width_expr, video_width, video_height,
                        )
                        if crop_width > 0:
                            coordinate_scale = frame_width / crop_width
                    except (ValueError, SyntaxError):
                        pass
            else:
                print("Extracted 0 frames")

            if auto_picked_cluster is not None and coordinate_scale != 1.0:
                auto_picked_cluster = tuple(
                    h * coordinate_scale for h in auto_picked_cluster
                )

            # OCR all frames
            batch_size = args.scan_batch_size
            raw_frame_lines = None  # per-frame [(text, x, y, w, h)] before filtering
            if args.scan_backend == "screenai":
                ocr_texts, raw_frame_lines = detect_text_frames_screenai(frames, args.languages, num_workers=args.scan_threads, batch_size=batch_size or 64)
            elif args.scan_backend == "apple":
                ocr_texts = detect_text_frames_apple(frames, args.languages, num_workers=args.scan_threads, **({"batch_size": batch_size} if batch_size is not None else {}))

            # Filter blind-spot bboxes before watermark/clip-building.
            # iw/ih in the spec refer to the original video frame; the rect is
            # then translated by the --crop offset and scaled by --quality so it
            # lines up with bbox coordinates (which are in extracted-frame space).
            if args.blind_spot and raw_frame_lines is not None:
                vw, vh = _get_video_dimensions(video_path)
                rect = parse_blind_spot(args.blind_spot, vw, vh)
                cx, cy = _parse_crop_offsets(crop, vw, vh)
                rect = (
                    (rect[0] - cx) * coordinate_scale,
                    (rect[1] - cy) * coordinate_scale,
                    (rect[2] - cx) * coordinate_scale,
                    (rect[3] - cy) * coordinate_scale,
                )
                raw_frame_lines = filter_blind_spot_bboxes(raw_frame_lines, rect)
                all_h = [h for lines in raw_frame_lines for _, _, _, _, h in lines if h > 0]
                fl_min_h, fl_max_h = _compute_line_height_range(all_h)
                ocr_texts = _build_texts_from_frame_lines(raw_frame_lines, len(frames), fl_min_h, fl_max_h)

            # Compute height clusters (informational + powers --filter-cluster).
            # Done after blind-spot but before watermark so positional noise is
            # already excluded from the cluster analysis.
            cluster_ranges = []
            cluster_summary = []
            if raw_frame_lines is not None:
                all_h = [h for lines in raw_frame_lines for _, _, _, _, h in lines if h > 0]
                cluster_ranges = cluster_heights(all_h)
                cluster_summary = summarize_clusters(raw_frame_lines, cluster_ranges, sample_interval)

            chosen_idx = None
            if args.filter_cluster and cluster_ranges:
                if args.filter_cluster == "biggest":
                    chosen_idx = len(cluster_ranges) - 1
                else:
                    chosen_idx = int(args.filter_cluster)
                if not (0 <= chosen_idx < len(cluster_ranges)):
                    print(f"Cluster filter: index {chosen_idx} out of range (have {len(cluster_ranges)} clusters); skipping")
                    chosen_idx = None
            elif auto_picked_cluster and raw_frame_lines is not None:
                # Keep the selected range, not whichever post-crop histogram
                # cluster happens to contain its midpoint. OCR can split one
                # subtitle font into several modes or merge it with outliers.
                # Allow modest bbox jitter after cropping/resizing.
                cmin = auto_picked_cluster[0] * 0.8
                cmax = auto_picked_cluster[1] * 1.2
                raw_frame_lines = filter_height_cluster(raw_frame_lines, cmin, cmax)
                ocr_texts = _build_texts_from_frame_lines(raw_frame_lines, len(frames), 0, 0)
                print(f"Auto-cluster filter: keeping h=[{cmin:.1f}, {cmax:.1f}]")

            if chosen_idx is not None:
                cmin, cmax = cluster_ranges[chosen_idx]
                raw_frame_lines = filter_height_cluster(raw_frame_lines, cmin, cmax)
                print(f"Cluster filter: kept cluster {chosen_idx} (h=[{cmin}, {cmax}])")
                all_h = [h for lines in raw_frame_lines for _, _, _, _, h in lines if h > 0]
                fl_min_h, fl_max_h = _compute_line_height_range(all_h)
                ocr_texts = _build_texts_from_frame_lines(raw_frame_lines, len(frames), fl_min_h, fl_max_h)

            # Filter watermark bboxes before building clips
            if args.watermark and raw_frame_lines is not None:
                wm_charset = _build_watermark_charset(args.watermark)
                filtered_fl = filter_watermark_bboxes(raw_frame_lines, wm_charset)
                all_h = [h for lines in filtered_fl for _, _, _, _, h in lines if h > 0]
                fl_min_h, fl_max_h = _compute_line_height_range(all_h)
                ocr_texts = _build_texts_from_frame_lines(filtered_fl, len(frames), fl_min_h, fl_max_h)

            clips = build_clips(ocr_texts, sample_interval)
            text_frame_count = sum(e - s + 1 for s, e in clips)
            print(
                f"Found {len(clips)} subtitle clips ({text_frame_count} frames with text)"
            )

            # Copy clip frames to scanned_frames/ with timestamp names
            for s, e in clips:
                for idx in range(s, e + 1):
                    timestamp = idx * sample_interval
                    h = int(timestamp // 3600)
                    m = int((timestamp % 3600) // 60)
                    s_sec = int(timestamp % 60)
                    ms = int(round((timestamp - int(timestamp)) * 1000))
                    dest = scanned_dir / f"{h:02d}-{m:02d}-{s_sec:02d}-{ms:03d}.png"
                    shutil.copy2(frames[idx], dest)
            print(f"Saved {text_frame_count} frame PNGs to {scanned_dir}/")

            # Write per-line bounding box CSV if requested
            if args.bbox_csv and raw_frame_lines is not None:
                _write_bbox_csv(args.bbox_csv, raw_frame_lines, sample_interval)

            if cluster_summary:
                print("Height clusters:")
                for idx, info in enumerate(cluster_summary):
                    text = info["first_text"]
                    if len(text) > 60:
                        text = text[:57] + "…"
                    print(
                        f"  cluster {idx}: height [{info['min_h']}, {info['max_h']}], "
                        f"first subtitle \"{text}\", "
                        f"weighted duration {info['duration']:.1f}s"
                    )

            if args.scan_only:
                print("Scan-only mode: skipping SRT generation.")
                if pgssub_cleanup_dir:
                    shutil.rmtree(pgssub_cleanup_dir, ignore_errors=True)
                    print("Removed temporary PGSSUB black video")
                elif pgssub_pipeline_used and (args.keep_pgssub_video or args.pgssub_video_output):
                    print(f"Kept PGSSUB black video: {video_path}")
                continue

            # Build entries from OCR results within detected clips
            raw_entries = []
            for s, e in clips:
                for idx in range(s, e + 1):
                    timestamp = idx * sample_interval
                    text = ocr_texts[idx]
                    if text:
                        raw_entries.append((text, timestamp, timestamp + sample_interval))

            entries = deduplicate(raw_entries)
            before_smart = len(entries)
            entries = smart_deduplicate(entries, sample_interval)
            smart_merged = before_smart - len(entries)
            if smart_merged:
                print(f"Smart dedup merged {smart_merged} single-frame entries")
            if args.cleanup_backend == "rules":
                if args.raw_srt:
                    raw_path = os.path.splitext(output_path)[0] + ".raw.srt"
                    write_srt(entries, raw_path)
                    print(f"Written {len(entries)} raw subtitle entries to {raw_path}")

                print("Cleaning up OCR artifacts with rules-based cleanup...")
                entries = cleanup_rules(entries, languages=args.languages)
                # Fuzzy dedup: merge consecutive near-duplicate entries the cleanup missed
                entries = deduplicate(entries, max_dist=2)
                entries = cap_durations(entries)
                # Ensure every frame in detected clips is covered by a subtitle
                entries = fill_clip_gaps(entries, clips, sample_interval)
            write_srt(entries, output_path)
            print(f"Written {len(entries)} subtitle entries to {output_path}")
            if pgssub_cleanup_dir:
                shutil.rmtree(pgssub_cleanup_dir, ignore_errors=True)
                print("Removed temporary PGSSUB black video")
            elif pgssub_pipeline_used and (args.keep_pgssub_video or args.pgssub_video_output):
                print(f"Kept PGSSUB black video: {video_path}")

    print_crop_recommendations(crop_recommendations)


if __name__ == "__main__":
    main()

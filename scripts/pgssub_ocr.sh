#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  pgssub_ocr.sh -i INPUT.mkv -o OUTPUT.srt [options] [-- OCRANIME_ARGS...]

Auto-select the first Chinese PGSSUB subtitle track (unless -s is given),
burn it onto a temporary black video, OCR that video with ocranime, then delete
the temporary video unless --keep-video is passed.

Required:
  -i, --input PATH          Input video
  -o, --output PATH         Output SRT path

Options:
  -s, --subtitle INDEX      Global subtitle stream index from ffprobe, e.g. 7 for Stream #0:7
                            Default: first Chinese PGSSUB subtitle track
  -d, --duration DURATION   Limit extraction/OCR duration, e.g. 00:20:00
      --keep-video          Keep the temporary burned subtitle video
      --video-output PATH   Burned subtitle video path (implies --keep-video)
      --crf N               Temporary video x264 CRF (default: 18)
      --preset PRESET       Temporary video x264 preset (default: veryfast)
  -h, --help                Show this help

Anything after -- is passed through to ocranime.

Examples:
  ./scripts/pgssub_ocr.sh -i movie.mkv -o movie.srt
  ./scripts/pgssub_ocr.sh -i movie.mkv -o movie.srt -d 00:20:00 -- --fps 6 --raw-srt
EOF
}

input=""
output=""
stream_index=""
duration=""
keep_video=0
video_output=""
crf="18"
preset="veryfast"
ocr_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--input) input="$2"; shift 2 ;;
    -o|--output) output="$2"; shift 2 ;;
    -s|--subtitle) stream_index="$2"; shift 2 ;;
    -d|--duration) duration="$2"; shift 2 ;;
    --keep-video) keep_video=1; shift ;;
    --video-output) video_output="$2"; keep_video=1; shift 2 ;;
    --crf) crf="$2"; shift 2 ;;
    --preset) preset="$2"; shift 2 ;;
    --) shift; ocr_args=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$input" || -z "$output" ]]; then
  echo "Missing -i/--input or -o/--output" >&2
  usage >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"

mkdir -p "$(dirname "$output")"
output="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$output")"

if [[ -z "$video_output" ]]; then
  tmpdir="$(mktemp -d -t pgssub_ocr_XXXXXX)"
  video_output="$tmpdir/burned_subtitles.mp4"
else
  tmpdir=""
  mkdir -p "$(dirname "$video_output")"
  video_output="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$video_output")"
fi

cleanup() {
  if [[ "$keep_video" -eq 0 ]]; then
    rm -f "$video_output"
    [[ -n "${tmpdir:-}" ]] && rm -rf "$tmpdir"
  fi
}
trap cleanup EXIT

burn_cmd=("$script_dir/pgssub_black_burn.sh" -i "$input" -o "$video_output" --crf "$crf" --preset "$preset")
if [[ -n "$stream_index" ]]; then
  burn_cmd+=(-s "$stream_index")
fi
if [[ -n "$duration" ]]; then
  burn_cmd+=(-d "$duration")
fi

"${burn_cmd[@]}"

ocr_cmd=(python3 "$repo_dir/ocranime.py" "$video_output" -o "$output" --crop "iw:ih:0:0")
if [[ ${#ocr_args[@]} -gt 0 ]]; then
  ocr_cmd+=("${ocr_args[@]}")
fi

echo "OCRing burned subtitle video to: $output"
(
  cd "$repo_dir"
  "${ocr_cmd[@]}"
)

echo "Done: $output"
if [[ "$keep_video" -eq 1 ]]; then
  echo "Kept burned subtitle video: $video_output"
else
  echo "Removed temporary burned subtitle video"
fi

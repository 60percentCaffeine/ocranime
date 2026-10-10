#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  pgssub_black_burn.sh -i INPUT.mkv -o OUTPUT.mp4 [options]

Extract a PGSSUB/HDMV PGS subtitle stream, then burn it onto a black video.

Required:
  -i, --input PATH          Input video
  -o, --output PATH         Output video path

Options:
  -s, --subtitle INDEX      Global subtitle stream index from ffprobe, e.g. 7 for Stream #0:7
                            Default: first Chinese PGSSUB subtitle track
  -d, --duration DURATION   Limit output/extraction duration, e.g. 00:20:00
      --crf N               x264 CRF (default: 18)
      --preset PRESET       x264 preset (default: veryfast)
      --sup PATH            Extracted .sup path (default: OUTPUT without extension + .sup)
      --list                List streams and exit
  -h, --help                Show this help

Example:
  ./scripts/pgssub_black_burn.sh -i movie.mkv -o subs_black.mp4 -d 00:20:00
EOF
}

input=""
stream_index=""
output=""
duration=""
crf="18"
preset="veryfast"
sup=""
list_only=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--input) input="$2"; shift 2 ;;
    -s|--subtitle) stream_index="$2"; shift 2 ;;
    -o|--output) output="$2"; shift 2 ;;
    -d|--duration) duration="$2"; shift 2 ;;
    --crf) crf="$2"; shift 2 ;;
    --preset) preset="$2"; shift 2 ;;
    --sup) sup="$2"; shift 2 ;;
    --list) list_only=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$input" ]]; then
  echo "Missing -i/--input" >&2
  usage >&2
  exit 2
fi

if [[ "$list_only" -eq 1 ]]; then
  ffprobe -hide_banner "$input" 2>&1 | grep -E 'Stream #|Duration'
  exit 0
fi

if [[ -z "$output" ]]; then
  echo "Missing -o/--output" >&2
  usage >&2
  exit 2
fi

if [[ ! -f "$input" ]]; then
  echo "Input not found: $input" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")"
if [[ -z "$sup" ]]; then
  base="${output%.*}"
  sup="${base}.sup"
fi
mkdir -p "$(dirname "$sup")"

if [[ -z "$stream_index" ]]; then
  streams_json="$(ffprobe -v error -show_streams -of json "$input")"
  stream_index="$(python3 - "$streams_json" <<'PY'
import json, sys

data = json.loads(sys.argv[1])
langs = {"chi", "zho", "zh", "chs", "cht", "cmn", "yue"}
for s in data.get("streams", []):
    tags = s.get("tags") or {}
    lang = (tags.get("language") or "").lower()
    codec = s.get("codec_name")
    if s.get("codec_type") == "subtitle" and codec in {"hdmv_pgs_subtitle", "pgssub"} and lang in langs:
        print(s["index"])
        sys.exit(0)
sys.exit(1)
PY
)" || {
    echo "Could not auto-select a Chinese PGSSUB subtitle track." >&2
    echo "Use --list to inspect streams, then pass -s STREAM_INDEX." >&2
    exit 1
  }
  echo "Auto-selected first Chinese PGSSUB subtitle stream: #0:${stream_index}"
fi

codec="$(ffprobe -v error -select_streams "$stream_index" -show_entries stream=codec_name -of default=nw=1:nk=1 "$input" | head -n1 || true)"
if [[ "$codec" != "hdmv_pgs_subtitle" && "$codec" != "pgssub" ]]; then
  echo "Stream $stream_index codec is '$codec', not PGSSUB/HDMV PGS." >&2
  echo "Use --list to inspect streams." >&2
  exit 1
fi

if [[ -z "$duration" ]]; then
  duration="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$input")"
fi

extract_args=(-hide_banner -y)
if [[ -n "$duration" ]]; then
  extract_args+=(-t "$duration")
fi
extract_args+=(-i "$input" -map "0:$stream_index" -c:s copy "$sup")

echo "Extracting Stream #0:$stream_index to: $sup"
ffmpeg "${extract_args[@]}"

color_duration="$(python3 - "$duration" <<'PY'
import sys
s = sys.argv[1]
if ':' not in s:
    print(s)
else:
    parts = [float(p) for p in s.split(':')]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    print(total)
PY
)"

sub_start="$(ffprobe -v error -select_streams s:0 -show_entries stream=start_time -of default=nw=1:nk=1 "$sup" | head -n1 || true)"
if [[ -z "$sub_start" || "$sub_start" == "N/A" ]]; then
  sub_start="0"
fi

sub_width="$(ffprobe -v error -select_streams s:0 -show_entries stream=width -of default=nw=1:nk=1 "$sup" | head -n1 || true)"
sub_height="$(ffprobe -v error -select_streams s:0 -show_entries stream=height -of default=nw=1:nk=1 "$sup" | head -n1 || true)"
if [[ -z "${sub_width:-}" || -z "${sub_height:-}" || "$sub_width" == "N/A" || "$sub_height" == "N/A" ]]; then
  echo "Could not determine subtitle canvas size from: $sup" >&2
  exit 1
fi
size="${sub_width}x${sub_height}"

fps="$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 "$input" | head -n1 || true)"
if [[ -z "$fps" || "$fps" == "0/0" || "$fps" == "N/A" ]]; then
  fps="$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of default=nw=1:nk=1 "$input" | head -n1 || true)"
fi
if [[ -z "$fps" || "$fps" == "0/0" || "$fps" == "N/A" ]]; then
  echo "Could not determine original video FPS from: $input" >&2
  exit 1
fi

# ffmpeg's PGS decoder normalizes the extracted .sup events to t=0 when
# feeding overlay, so re-apply the original first PGS timestamp explicitly.
echo "Burning subtitles onto black ${size} @ ${fps} for ${duration}: $output"
echo "Applying subtitle start offset: ${sub_start}s"
ffmpeg -hide_banner -y \
  -f lavfi -i "color=c=black:s=${size}:r=${fps}:d=${color_duration}" \
  -itsoffset "$sub_start" -i "$sup" \
  -filter_complex "[0:v][1:s:0]overlay=format=auto" \
  -t "$duration" -an \
  -c:v libx264 -crf "$crf" -preset "$preset" -pix_fmt yuv420p -movflags +faststart \
  "$output"

echo "Done: $output"
echo "Extracted subtitle: $sup"

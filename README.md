# ocranime

Fast, local OCR for turning burned-in subtitles (hardsubs) from local or online Bilibili/YouTube/etc videos into SRT files.

## Install

Requires **Python 3.11+**, poetry, ffmpeg and optionally yt-dlp.

### macOS

```bash
# Install Homebrew if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install ffmpeg, Poetry
brew install ffmpeg
brew install poetry

# Optionally install yt-dlp
brew install yt-dlp

# Clone and install
git clone <repo-url> && cd ocranime
poetry install
```

### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv ffmpeg pipx

# Install Poetry
pipx install poetry

# Optionally install yt-dlp
pipx install yt-dlp

# Clone and install
git clone <repo-url> && cd ocranime
poetry install
```

### Global install (run from anywhere)

To use `ocranime` as a command from any directory:

```bash
poetry install
# Add the virtualenv bin to your PATH
echo 'export PATH="$(poetry env info -p)/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

For Ubuntu/macOS, replace `~/.zshrc` with `~/.bashrc`. Now you can run `ocranime` from anywhere:

```
ocranime video.mp4
```

## Usage

```
poetry run python ocranime.py video.mp4
```

### How it works

1. **Input resolution** — accepts local files, or downloads URLs/bare video IDs with yt-dlp when needed
2. **Auto-crop + frame extraction** — finds the subtitle region by default, then extracts cropped frames with ffmpeg at 4 fps; PQ/HLG HDR inputs are automatically tone-mapped to BT.709 SDR
3. **Fast OCR scan** — batches frames and can run worker processes in parallel; a typical 30 minute video usually finishes in a couple of minutes
4. **Chrome Screen AI OCR** — recognizes subtitle text locally with Chrome Screen AI
5. **Filtering + SRT generation** — removes optional watermarks, blind spots, and non-subtitle text clusters, useful for chat/UI overlays in YouTube/Bilibili videos; then groups subtitle frames, deduplicates OCR, and applies rules-based cleanup by default

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `-o/--output` | Output SRT path, or output folder for multiple inputs | `<video>.srt` |
| `--scan-backend` | OCR backend: `screenai`, `apple` | `screenai` |
| `--fps` | Frames per second to sample | `4` |
| `--quality` | Scale extracted frame width and height, e.g. `0.5` halves both | `1.0` |
| `--crop` | ffmpeg crop filter, or `auto` | `auto` |
| `--crop2 TOP BOTTOM` | OCR two crop regions stacked together | off |
| `--cleanup-backend` | Cleanup backend: `none`, `rules` | `rules` |
| `--scan-only` | Only detect subtitle frames, skip OCR/SRT | off |
| `--watermark TEXT` | Remove OCR lines matching a watermark | off |
| `--blind-spot W:H:X:Y` | Ignore a fixed region in the source frame | off |
| `--filter-cluster biggest\|N` | Keep one detected text-height cluster | off |
| `--raw-srt` | Also write pre-cleanup subtitles | off |

### Examples

```
# Local video with defaults
poetry run python ocranime.py video.mp4

# Manual bottom subtitle crop
poetry run python ocranime.py video.mp4 --crop "iw*0.8:ih*0.18:iw*0.1:ih*0.75"

# OCR top and bottom subtitle regions
poetry run python ocranime.py video.mp4 --crop2 "iw:ih*0.18:0:0" "iw:ih*0.18:0:ih*0.75"

# Download a bilibili video, OCR it and remove a repeated Bilibili streamer watermark from the subs
poetry run python ocranime.py "https://bilibili.com/video" --watermark "bilibili主播"

# Keep the largest text-height cluster, useful when chat/UI text is smaller
poetry run python ocranime.py video.mp4 --filter-cluster biggest

# Just detect subtitle frames without full OCR
poetry run python ocranime.py video.mp4 --scan-only
```

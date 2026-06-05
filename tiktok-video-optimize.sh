#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 input-video [output-video] [--mode fit|crop|blur] [--quality standard|high|safe]"
  echo "Example: $0 video-asli.mov video-tiktok.mp4 --mode blur --quality safe"
  exit 1
fi

INPUT="$1"
shift
OUTPUT=""
MODE="fit"
QUALITY="safe"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      if [[ $# -lt 2 ]]; then
        echo "Mode belum diisi. Pilih: fit, crop, atau blur." >&2
        exit 1
      fi
      MODE="$2"
      shift 2
      ;;
    --quality)
      if [[ $# -lt 2 ]]; then
        echo "Quality belum diisi. Pilih: standard, high, atau safe." >&2
        exit 1
      fi
      QUALITY="$2"
      shift 2
      ;;
    fit|crop|blur)
      MODE="$1"
      shift
      ;;
    *)
      if [[ -z "$OUTPUT" ]]; then
        OUTPUT="$1"
        shift
      else
        echo "Argumen tidak dikenali: $1" >&2
        exit 1
      fi
      ;;
  esac
done

case "$MODE" in
  fit|crop|blur) ;;
  *)
    echo "Mode tidak valid: $MODE. Pilih: fit, crop, atau blur." >&2
    exit 1
    ;;
esac

case "$QUALITY" in
  standard)
    VIDEO_BITRATE="8M"
    VIDEO_MAXRATE="10M"
    VIDEO_BUFSIZE="16M"
    ;;
  high)
    VIDEO_BITRATE="12M"
    VIDEO_MAXRATE="16M"
    VIDEO_BUFSIZE="24M"
    ;;
  safe)
    VIDEO_BITRATE="10M"
    VIDEO_MAXRATE="12M"
    VIDEO_BUFSIZE="20M"
    ;;
  *)
    echo "Quality tidak valid: $QUALITY. Pilih: standard, high, atau safe." >&2
    exit 1
    ;;
esac

if [[ ! -f "$INPUT" ]]; then
  echo "File tidak ditemukan: $INPUT" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg belum terinstall. Install dulu: sudo apt install ffmpeg" >&2
  exit 1
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe belum terinstall. Biasanya ikut paket ffmpeg." >&2
  exit 1
fi

if [[ -z "$OUTPUT" ]]; then
  BASENAME="$(basename "$INPUT")"
  NAME="${BASENAME%.*}"
  OUTPUT="${NAME}-tiktok.mp4"
fi

VIDEO_CODEC="$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$INPUT" || true)"
if [[ -z "$VIDEO_CODEC" ]]; then
  echo "File tidak memiliki stream video yang valid: $INPUT" >&2
  exit 1
fi

AUDIO_CODEC="$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$INPUT" || true)"
COLOR_TRANSFER="$(ffprobe -v error -select_streams v:0 -show_entries stream=color_transfer -of default=nw=1:nk=1 "$INPUT" || true)"
COLOR_SPACE="$(ffprobe -v error -select_streams v:0 -show_entries stream=color_space -of default=nw=1:nk=1 "$INPUT" || true)"

SAFE_FILTER_SUFFIX=",format=yuv420p"
if [[ "$QUALITY" == "safe" ]]; then
  case "${COLOR_TRANSFER}:${COLOR_SPACE}" in
    smpte2084:*|arib-std-b67:*|*:bt2020nc)
      SAFE_FILTER_SUFFIX=",zscale=t=linear:npl=100,format=gbrpf32le,tonemap=tonemap=hable:desat=0,zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p"
      ;;
  esac
fi

# TikTok-safe export:
# - 9:16 vertical 1080x1920
# - H.264 MP4, yuv420p for compatibility
# - 30fps to reduce compression artifacts
# - quality presets keep bitrate clean without making TikTok transcode too aggressively
FFMPEG_ARGS=(
  -y
  -i "$INPUT"
)

case "$MODE" in
  fit)
    FFMPEG_ARGS+=(
      -map 0:v:0
      -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30${SAFE_FILTER_SUFFIX}"
    )
    ;;
  crop)
    FFMPEG_ARGS+=(
      -map 0:v:0
      -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30${SAFE_FILTER_SUFFIX}"
    )
    ;;
  blur)
    FFMPEG_ARGS+=(
      -filter_complex "[0:v:0]split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=40:1[blur];[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];[blur][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30${SAFE_FILTER_SUFFIX}[v]"
      -map "[v]"
    )
    ;;
esac

if [[ -n "$AUDIO_CODEC" ]]; then
  FFMPEG_ARGS+=(-map 0:a:0)
fi

FFMPEG_ARGS+=(
  -c:v libx264 \
  -profile:v high \
  -level 4.1 \
  -pix_fmt yuv420p \
  -preset slow \
  -b:v "$VIDEO_BITRATE" \
  -maxrate "$VIDEO_MAXRATE" \
  -bufsize "$VIDEO_BUFSIZE" \
  -movflags +faststart
)

if [[ "$QUALITY" == "safe" ]]; then
  FFMPEG_ARGS+=(
    -color_primaries bt709
    -color_trc bt709
    -colorspace bt709
    -color_range tv
  )
fi

if [[ -n "$AUDIO_CODEC" ]]; then
  # Normalize audio only when the input actually has an audio stream.
  FFMPEG_ARGS+=(
    -c:a aac
    -b:a 192k
    -ar 48000
    -af "loudnorm=I=-14:TP=-1.5:LRA=11"
  )
else
  FFMPEG_ARGS+=(-an)
fi

FFMPEG_ARGS+=("$OUTPUT")

ffmpeg "${FFMPEG_ARGS[@]}"

echo "Selesai: $OUTPUT"
echo "Mode: $MODE"
echo "Quality: $QUALITY ($VIDEO_BITRATE)"
if [[ -n "$AUDIO_CODEC" ]]; then
  echo "Standar: 1080x1920, 30fps, H.264 MP4, AAC 192k"
else
  echo "Standar: 1080x1920, 30fps, H.264 MP4, tanpa audio"
fi

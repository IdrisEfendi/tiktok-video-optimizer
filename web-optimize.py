#!/usr/bin/env python3
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import warnings
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

warnings.filterwarnings("ignore", category=DeprecationWarning)
import cgi


ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "outputs"
SCRIPT_PATH = ROOT / "tiktok-video-optimize.sh"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
CLEANUP_AFTER_HOURS = int(os.getenv("CLEANUP_AFTER_HOURS", "24"))
CLEANUP_AFTER_SECONDS = CLEANUP_AFTER_HOURS * 60 * 60
MODES = {"fit", "crop", "blur"}
QUALITIES = {"standard", "high", "safe"}
PRESETS = {
    "safe-default": {"mode": "fit", "quality": "safe", "label": "TikTok Safe Default"},
    "small-file": {"mode": "fit", "quality": "standard", "label": "TikTok Small File"},
    "high-detail": {"mode": "blur", "quality": "high", "label": "TikTok High Detail"},
}
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_CLEANUP_AFTER_SECONDS = 6 * 60 * 60
PREVIEWS = {}
PREVIEWS_LOCK = threading.Lock()
PREVIEW_CLEANUP_AFTER_SECONDS = 2 * 60 * 60


def update_job(job_id, **updates):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def public_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job_id,
            "status": job["status"],
            "stage": job["stage"],
            "progress": job["progress"],
            "message": job["message"],
            "download_url": job.get("download_url"),
            "output_url": job.get("output_url"),
            "filename": job.get("filename"),
            "mode": job.get("mode"),
            "quality": job.get("quality"),
            "preset": job.get("preset"),
            "input_metadata": job.get("input_metadata"),
            "output_metadata": job.get("output_metadata"),
            "error": job.get("error"),
        }


def public_preview(preview_id):
    with PREVIEWS_LOCK:
        preview = PREVIEWS.get(preview_id)
        if not preview:
            return None
        return {
            "id": preview_id,
            "filename": preview["filename"],
            "preview_url": f"/preview/{preview_id}",
            "metadata": preview["metadata"],
        }


def safe_stem(filename):
    stem = Path(filename).stem or "video"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return stem[:80] or "video"


def safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def recommend_preset(metadata):
    codec = (metadata.get("video_codec") or "").lower()
    color_space = (metadata.get("color_space") or "").lower()
    color_transfer = (metadata.get("color_transfer") or "").lower()
    color_primaries = (metadata.get("color_primaries") or "").lower()
    width = metadata.get("width") or 0
    height = metadata.get("height") or 0
    duration = metadata.get("duration") or 0
    size = metadata.get("size") or 0

    color_values = {color_space, color_transfer, color_primaries}
    is_hdr = bool({"bt2020nc", "bt2020", "smpte2084", "arib-std-b67"} & color_values)
    is_hevc = codec in {"hevc", "h265"}
    is_long = duration >= 180
    is_large = size >= 500 * 1024 * 1024
    is_high_res = max(width, height) >= 2160 or (width * height) >= 2560 * 1440

    if is_hdr or is_hevc:
        return {
            "preset": "safe-default",
            "reason": "Input terdeteksi HEVC/HDR/warna wide-gamut. Safe Default membantu menormalkan output ke H.264 SDR Rec.709 untuk iPhone/Android.",
        }

    if is_long or is_large:
        return {
            "preset": "small-file",
            "reason": "Video cukup panjang atau besar. Small File membuat upload lebih ringan sambil tetap memakai format TikTok-safe.",
        }

    if is_high_res:
        return {
            "preset": "high-detail",
            "reason": "Resolusi input tinggi. High Detail menjaga detail lebih baik dengan quality lebih tinggi dan background blur.",
        }

    return {
        "preset": "safe-default",
        "reason": "Safe Default adalah pilihan seimbang untuk hasil yang konsisten lintas perangkat.",
    }


def cleanup_old_files():
    now = time.time()
    for directory in (UPLOAD_DIR, OUTPUT_DIR):
        directory.mkdir(exist_ok=True)
        for path in directory.iterdir():
            if path.is_file() and now - path.stat().st_mtime > CLEANUP_AFTER_SECONDS:
                path.unlink(missing_ok=True)

    with JOBS_LOCK:
        expired = [
            job_id
            for job_id, job in JOBS.items()
            if now - job.get("updated_at", now) > JOB_CLEANUP_AFTER_SECONDS
        ]
        for job_id in expired:
            JOBS.pop(job_id, None)

    with PREVIEWS_LOCK:
        expired = [
            preview_id
            for preview_id, preview in PREVIEWS.items()
            if now - preview.get("updated_at", now) > PREVIEW_CLEANUP_AFTER_SECONDS
        ]
        for preview_id in expired:
            preview = PREVIEWS.pop(preview_id, None)
            if preview:
                preview["path"].unlink(missing_ok=True)


def validate_video(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_entries",
            "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,avg_frame_rate,r_frame_rate",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        raise ValueError("File tidak bisa dibaca sebagai video valid.")

    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Metadata video tidak bisa dibaca.") from exc

    streams = metadata.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        raise ValueError("File tidak memiliki stream video.")

    duration = metadata.get("format", {}).get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except ValueError:
        duration = None

    video = video_streams[0]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]

    return {
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "audio_codec": audio_streams[0].get("codec_name") if audio_streams else None,
        "video_codec": video.get("codec_name", "unknown"),
        "width": video.get("width"),
        "height": video.get("height"),
        "pix_fmt": video.get("pix_fmt"),
        "color_space": video.get("color_space"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "duration": duration,
        "size": safe_int(metadata.get("format", {}).get("size")),
        "bit_rate": safe_int(metadata.get("format", {}).get("bit_rate")),
    }


def parse_ffmpeg_time(line):
    match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not match:
        return None

    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def run_optimize_job(job_id, input_path, output_path, output_name, mode, quality):
    try:
        update_job(
            job_id,
            status="running",
            stage="validating",
            progress=25,
            message="Memvalidasi metadata video.",
        )
        input_metadata = validate_video(input_path)
        duration = input_metadata.get("duration") or 0
        update_job(job_id, input_metadata=input_metadata)

        update_job(
            job_id,
            stage="optimizing",
            progress=35,
            message="Mengoptimalkan video dengan FFmpeg.",
        )
        process = subprocess.Popen(
            [
                "bash",
                str(SCRIPT_PATH),
                str(input_path),
                str(output_path),
                "--mode",
                mode,
                "--quality",
                quality,
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )

        output_tail = []
        if process.stdout:
            for line in process.stdout:
                output_tail.append(line.rstrip())
                output_tail = output_tail[-30:]

                seconds = parse_ffmpeg_time(line)
                if duration and seconds is not None:
                    encode_progress = min(seconds / duration, 1.0)
                    progress = 35 + int(encode_progress * 60)
                    update_job(
                        job_id,
                        progress=min(progress, 95),
                        message=f"Encoding video {min(int(encode_progress * 100), 99)}%.",
                    )

        return_code = process.wait()
        if return_code != 0:
            message = "\n".join(output_tail).strip() or "FFmpeg gagal memproses video."
            update_job(
                job_id,
                status="error",
                stage="failed",
                progress=100,
                message="Proses gagal.",
                error=message[-1200:],
            )
            output_path.unlink(missing_ok=True)
            return

        output_metadata = validate_video(output_path)
        update_job(
            job_id,
            status="done",
            stage="done",
            progress=100,
            message="Selesai. File hasil sudah siap diunduh.",
            download_url=f"/download/{output_name}",
            output_url=f"/output/{output_name}",
            filename=output_name,
            output_metadata=output_metadata,
        )
    except ValueError as exc:
        update_job(
            job_id,
            status="error",
            stage="failed",
            progress=100,
            message="Validasi video gagal.",
            error=str(exc),
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            stage="failed",
            progress=100,
            message="Proses gagal.",
            error=str(exc),
        )
    finally:
        input_path.unlink(missing_ok=True)


def page():
    return """<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TikTok Video Optimizer</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: {
            sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "sans-serif"],
          },
        },
      },
    };
  </script>
</head>
<body class="min-h-screen bg-slate-100 font-sans text-slate-950">
  <main class="mx-auto w-[min(1040px,calc(100%_-_32px))] py-8 max-md:w-[calc(100%_-_24px)] max-md:py-5">
    <header class="mb-5 flex items-end justify-between gap-4 max-md:block">
      <div>
        <p class="mb-1 text-xs font-bold uppercase tracking-[0.14em] text-teal-700">Local video tool</p>
        <h1 class="mb-2 text-[30px] font-extrabold leading-tight tracking-normal max-md:text-[26px]">TikTok Video Optimizer</h1>
        <p class="max-w-[660px] leading-relaxed text-slate-600">Convert video ke MP4 vertikal 1080x1920 dengan preset aman untuk upload TikTok.</p>
      </div>
      <div class="flex flex-wrap gap-2 text-xs font-bold text-slate-600 max-md:mt-4">
        <span class="rounded-full border border-slate-200 bg-white px-3 py-1.5">H.264</span>
        <span class="rounded-full border border-slate-200 bg-white px-3 py-1.5">AAC</span>
        <span class="rounded-full border border-slate-200 bg-white px-3 py-1.5">1080x1920</span>
      </div>
    </header>

    <section class="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <form class="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_12px_34px_rgba(15,23,42,0.06)]" id="uploadForm">
        <div class="sticky top-3 z-10 mb-3.5 grid gap-2.5 rounded-lg border border-slate-300 bg-slate-50/95 p-3 shadow-[0_12px_28px_rgba(22,24,29,0.08)] backdrop-blur max-md:static" id="statusPanel" aria-live="polite">
          <div class="flex items-start gap-2.5">
            <div class="grid h-7 w-7 flex-none place-items-center rounded-full bg-slate-200 text-sm font-black leading-none text-slate-600" id="statusIcon">i</div>
            <div class="min-w-0">
              <div class="mb-0.5 font-extrabold leading-tight text-slate-950" id="statusTitle">Siap</div>
              <div class="break-words leading-relaxed text-slate-500" id="status">Pilih video untuk mulai membaca metadata.</div>
            </div>
          </div>
          <div class="hidden h-2.5 overflow-hidden rounded-full bg-slate-200" id="progress">
            <div class="h-full w-0 rounded-full bg-teal-600 transition-[width] duration-200" id="progressBar"></div>
          </div>
        </div>
        <label class="grid min-h-[240px] cursor-pointer place-items-center rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 p-6 text-center transition hover:border-teal-600 hover:bg-teal-50 max-md:min-h-[200px]" id="dropzone" for="videoInput">
          <span>
            <span class="mx-auto mb-4 grid h-12 w-12 place-items-center rounded-full bg-white text-2xl font-black text-teal-700 shadow-sm">+</span>
            <strong class="mb-1.5 block text-xl">Pilih video</strong>
            <span class="block text-slate-500">atau tarik file ke area ini</span>
            <span class="mt-3 block text-sm text-slate-400">MP4, MOV, MKV, dan format FFmpeg lainnya</span>
          </span>
        </label>
        <input class="sr-only" id="videoInput" name="video" type="file" accept="video/*" required>
        <div class="my-3 min-h-6 break-words rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600" id="filename">Belum ada file dipilih.</div>
        <div class="my-3.5 grid gap-3 md:grid-cols-2" id="previewGrid" style="display: none;">
          <div class="overflow-hidden rounded-lg border border-slate-200 bg-slate-950">
            <video class="block aspect-[9/16] w-full bg-slate-950 object-contain" id="inputPreview" controls muted playsinline></video>
            <div class="bg-white px-2.5 py-2 text-sm font-bold text-slate-500">Input</div>
          </div>
          <div class="overflow-hidden rounded-lg border border-slate-200 bg-slate-950" id="outputPreviewBox" style="display: none;">
            <video class="block aspect-[9/16] w-full bg-slate-950 object-contain" id="outputPreview" controls playsinline></video>
            <div class="bg-white px-2.5 py-2 text-sm font-bold text-slate-500">Output</div>
          </div>
        </div>
        <div class="my-3.5 grid gap-2 md:grid-cols-2" id="metadata" style="display: none;"></div>
        <div class="my-3.5 rounded-lg border border-teal-200 bg-teal-50 p-3 leading-relaxed text-teal-700" id="recommendation" style="display: none;"></div>
        <div class="text-sm font-bold text-slate-500" id="outputMetadataTitle" style="display: none;">Output metadata</div>
        <div class="my-3.5 grid gap-2 md:grid-cols-2" id="outputMetadata" style="display: none;"></div>
        <details class="my-3.5 rounded-lg border border-slate-200 bg-white">
          <summary class="cursor-pointer list-none px-3 py-3 text-sm font-bold text-slate-700">Pengaturan output</summary>
          <div class="grid gap-3 border-t border-slate-200 p-3">
            <div class="grid gap-2">
              <div class="text-sm font-bold text-slate-500">Preset</div>
              <div class="grid grid-cols-3 gap-2">
                <label>
                  <input class="peer sr-only" type="radio" name="preset" value="safe-default" data-mode="fit" data-quality="safe" checked>
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">Safe Default</span>
                </label>
                <label>
                  <input class="peer sr-only" type="radio" name="preset" value="small-file" data-mode="fit" data-quality="standard">
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">Small File</span>
                </label>
                <label>
                  <input class="peer sr-only" type="radio" name="preset" value="high-detail" data-mode="blur" data-quality="high">
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">High Detail</span>
                </label>
              </div>
            </div>
            <div class="grid gap-2">
              <div class="text-sm font-bold text-slate-500">Mode output</div>
              <div class="grid grid-cols-3 gap-2">
                <label>
                  <input class="peer sr-only" type="radio" name="mode" value="fit" checked>
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">Fit</span>
                </label>
                <label>
                  <input class="peer sr-only" type="radio" name="mode" value="crop">
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">Crop</span>
                </label>
                <label>
                  <input class="peer sr-only" type="radio" name="mode" value="blur">
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">Blur</span>
                </label>
              </div>
            </div>
            <div class="grid gap-2">
              <div class="text-sm font-bold text-slate-500">Quality</div>
              <div class="grid grid-cols-3 gap-2">
                <label>
                  <input class="peer sr-only" type="radio" name="quality" value="safe" checked>
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">Safe</span>
                </label>
                <label>
                  <input class="peer sr-only" type="radio" name="quality" value="high">
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">High</span>
                </label>
                <label>
                  <input class="peer sr-only" type="radio" name="quality" value="standard">
                  <span class="grid min-h-[42px] cursor-pointer place-items-center rounded-lg border border-slate-200 bg-white p-2 text-center font-bold text-slate-950 transition peer-checked:border-teal-600 peer-checked:bg-teal-50 peer-checked:text-teal-700 peer-checked:shadow-[inset_0_0_0_1px_#0d9488]">Standard</span>
                </label>
              </div>
            </div>
          </div>
        </details>
        <button class="w-full rounded-lg bg-teal-600 px-4 py-3 font-bold text-white transition hover:bg-teal-700 disabled:cursor-not-allowed disabled:opacity-55" id="optimizeButton" type="submit">Optimize Video</button>
        <div class="mt-3 grid gap-2.5 md:grid-cols-2" id="result" style="display: none;">
          <a class="w-full rounded-lg bg-teal-600 px-4 py-3 text-center font-bold text-white no-underline transition hover:bg-teal-700" id="downloadLink" href="#">Download Hasil</a>
          <button class="w-full rounded-lg bg-red-700 px-4 py-3 font-bold text-white transition hover:bg-red-800 disabled:cursor-not-allowed disabled:opacity-55" id="deleteButton" type="button">Hapus Hasil</button>
        </div>
      </form>

      <aside class="sticky top-3 rounded-lg border border-slate-200 bg-white p-4 shadow-[0_12px_34px_rgba(15,23,42,0.06)] max-md:static" aria-label="Standar output">
        <div class="mb-3">
          <h2 class="font-extrabold text-slate-950">Output standar</h2>
          <p class="mt-1 text-sm leading-relaxed text-slate-500">Default aman untuk TikTok dan mayoritas perangkat.</p>
        </div>
        <div class="flex justify-between gap-3 border-b border-slate-200 py-2.5 text-sm text-slate-500"><span>Format</span><strong class="text-right text-slate-950">MP4</strong></div>
        <div class="flex justify-between gap-3 border-b border-slate-200 py-2.5 text-sm text-slate-500"><span>Resolusi</span><strong class="text-right text-slate-950">1080 x 1920</strong></div>
        <div class="flex justify-between gap-3 border-b border-slate-200 py-2.5 text-sm text-slate-500"><span>Frame rate</span><strong class="text-right text-slate-950">30 fps</strong></div>
        <div class="flex justify-between gap-3 border-b border-slate-200 py-2.5 text-sm text-slate-500"><span>Video codec</span><strong class="text-right text-slate-950">H.264</strong></div>
        <div class="flex justify-between gap-3 border-b border-slate-200 py-2.5 text-sm text-slate-500"><span>Audio</span><strong class="text-right text-slate-950">AAC 192 kbps</strong></div>
        <div class="flex justify-between gap-3 border-b border-slate-200 py-2.5 text-sm text-slate-500"><span>Loudness</span><strong class="text-right text-slate-950">-14 LUFS</strong></div>
        <div class="flex justify-between gap-3 py-2.5 text-sm text-slate-500"><span>Cleanup</span><strong class="text-right text-slate-950">otomatis</strong></div>
      </aside>
    </section>
  </main>

  <script>
    const form = document.querySelector("#uploadForm");
    const input = document.querySelector("#videoInput");
    const dropzone = document.querySelector("#dropzone");
    const filename = document.querySelector("#filename");
    const button = document.querySelector("#optimizeButton");
    const statusPanel = document.querySelector("#statusPanel");
    const statusTitle = document.querySelector("#statusTitle");
    const statusIcon = document.querySelector("#statusIcon");
    const statusBox = document.querySelector("#status");
    const result = document.querySelector("#result");
    const downloadLink = document.querySelector("#downloadLink");
    const deleteButton = document.querySelector("#deleteButton");
    const progress = document.querySelector("#progress");
    const progressBar = document.querySelector("#progressBar");
    const previewGrid = document.querySelector("#previewGrid");
    const inputPreview = document.querySelector("#inputPreview");
    const outputPreview = document.querySelector("#outputPreview");
    const outputPreviewBox = document.querySelector("#outputPreviewBox");
    const metadata = document.querySelector("#metadata");
    const recommendation = document.querySelector("#recommendation");
    const outputMetadata = document.querySelector("#outputMetadata");
    const outputMetadataTitle = document.querySelector("#outputMetadataTitle");
    let pollTimer = null;
    let previewId = null;
    let outputFilename = null;

    const statusPanelBase = "sticky top-3 z-10 mb-3.5 grid gap-2.5 rounded-lg border p-3 shadow-[0_12px_28px_rgba(22,24,29,0.08)] backdrop-blur max-md:static";
    const statusIconBase = "grid h-7 w-7 flex-none place-items-center rounded-full text-sm font-black leading-none";
    const statusTitleBase = "mb-0.5 font-extrabold leading-tight";
    const statusMessageBase = "break-words leading-relaxed";
    const statusStyles = {
      idle: {
        panel: "border-slate-300 bg-slate-50/95",
        icon: "bg-slate-200 text-slate-600",
        title: "text-slate-950",
        message: "text-slate-500",
        bar: "bg-teal-600",
      },
      loading: {
        panel: "border-blue-200 bg-blue-50/95",
        icon: "bg-blue-200 text-blue-700",
        title: "text-blue-700",
        message: "text-slate-600",
        bar: "bg-blue-600",
      },
      success: {
        panel: "border-green-200 bg-green-50/95",
        icon: "bg-green-200 text-green-700",
        title: "text-green-700",
        message: "text-green-700",
        bar: "bg-green-600",
      },
      error: {
        panel: "border-red-200 bg-red-50/95",
        icon: "bg-red-200 text-red-700",
        title: "text-red-700",
        message: "text-red-700",
        bar: "bg-red-600",
      },
    };

    function setStatus(type, title, message) {
      const style = statusStyles[type] || statusStyles.idle;
      const iconMap = {
        idle: "i",
        loading: "...",
        success: "ok",
        error: "!",
      };
      statusPanel.className = `${statusPanelBase} ${style.panel}`;
      statusIcon.className = `${statusIconBase} ${style.icon}`;
      statusTitle.className = `${statusTitleBase} ${style.title}`;
      statusBox.className = `${statusMessageBase} ${style.message}`;
      progressBar.className = `h-full w-0 rounded-full transition-[width] duration-200 ${style.bar}`;
      statusIcon.textContent = iconMap[type] || "i";
      statusTitle.textContent = title;
      statusBox.textContent = message;
    }

    function setRadioValue(name, value) {
      const field = document.querySelector(`input[name="${name}"][value="${value}"]`);
      if (field) field.checked = true;
    }

    function applyPreset(field) {
      if (!field) return;
      setRadioValue("mode", field.dataset.mode || "fit");
      setRadioValue("quality", field.dataset.quality || "safe");
    }

    function applyPresetValue(value) {
      const field = document.querySelector(`input[name="preset"][value="${value}"]`);
      if (!field) return;
      field.checked = true;
      applyPreset(field);
    }

    function presetLabel(value) {
      const field = document.querySelector(`input[name="preset"][value="${value}"]`);
      return field ? field.nextElementSibling.textContent : value;
    }

    function formatDuration(seconds) {
      if (!seconds) return "-";
      const total = Math.round(seconds);
      const minutes = Math.floor(total / 60);
      const rest = total % 60;
      return `${minutes}:${String(rest).padStart(2, "0")}`;
    }

    function formatBytes(bytes) {
      if (!bytes) return "-";
      const units = ["B", "KB", "MB", "GB"];
      let value = Number(bytes);
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
      }
      return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
    }

    function formatRate(rate) {
      if (!rate || !rate.includes("/")) return rate || "-";
      const [top, bottom] = rate.split("/").map(Number);
      if (!top || !bottom) return "-";
      return `${(top / bottom).toFixed(2)} fps`;
    }

    function renderMetadata(container, info) {
      const items = [
        ["Resolusi", `${info.width || "-"} x ${info.height || "-"}`],
        ["Durasi", formatDuration(info.duration)],
        ["Video", info.video_codec || "-"],
        ["Audio", info.has_audio ? (info.audio_codec || "ada") : "tanpa audio"],
        ["Frame rate", formatRate(info.avg_frame_rate)],
        ["Pixel", info.pix_fmt || "-"],
        ["Warna", [info.color_space, info.color_transfer, info.color_primaries].filter(Boolean).join(" / ") || "-"],
        ["Ukuran", formatBytes(info.size)],
        ["Bitrate", info.bit_rate ? `${(info.bit_rate / 1000000).toFixed(2)} Mbps` : "-"],
      ];

      container.innerHTML = items.map(([label, value]) => `
        <div class="min-w-0 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
          <span class="block text-xs font-bold text-slate-500">${label}</span>
          <strong class="mt-0.5 block break-words text-sm text-slate-950">${value}</strong>
        </div>
      `).join("");
      container.style.display = "grid";
    }

    function setProgress(value) {
      const safeValue = Math.max(0, Math.min(Number(value) || 0, 100));
      progress.style.display = "block";
      progressBar.style.width = `${safeValue}%`;
    }

    async function pollJob(jobId) {
      const response = await fetch(`/jobs/${jobId}`);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "Status job tidak tersedia.");
      }

      setProgress(data.progress);
      setStatus("loading", "Memproses", data.message || "Memproses video.");

      if (data.status === "done") {
        clearInterval(pollTimer);
        pollTimer = null;
        progress.style.display = "none";
        progressBar.style.width = "0%";
        downloadLink.href = data.download_url;
        outputPreview.src = data.output_url || data.download_url;
        outputFilename = data.filename;
        outputPreviewBox.style.display = "block";
        if (data.output_metadata) {
          outputMetadataTitle.style.display = "block";
          renderMetadata(outputMetadata, data.output_metadata);
        }
        result.style.display = "grid";
        button.disabled = false;
        setStatus("success", "Selesai", "File hasil sudah siap. Preview output dan tombol download tersedia di bawah.");
        return;
      }

      if (data.status === "error") {
        clearInterval(pollTimer);
        pollTimer = null;
        setStatus("error", "Proses gagal", data.error || data.message || "Proses gagal.");
        button.disabled = false;
      }
    }

    async function inspectFile(file) {
      const body = new FormData();
      body.append("video", file);
      const response = await fetch("/inspect", { method: "POST", body });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "File tidak bisa diinspeksi.");
      }

      previewId = data.preview_id;
      inputPreview.src = data.preview_url;
      previewGrid.style.display = "grid";
      renderMetadata(metadata, data.metadata);
      if (data.recommended_preset) {
        applyPresetValue(data.recommended_preset);
        recommendation.innerHTML = `
          <strong class="mb-1 block text-teal-700">Rekomendasi: ${presetLabel(data.recommended_preset)}</strong>
          <span>${data.recommendation_reason || ""}</span>
        `;
        recommendation.style.display = "block";
      }
      setStatus("success", "Metadata siap", "Preset rekomendasi sudah dipilih. Cek mode dan quality, lalu jalankan Optimize Video.");
    }

    async function setFile(file) {
      if (!file) return;
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      previewId = null;
      outputFilename = null;
      filename.textContent = file.name;
      result.style.display = "none";
      outputPreview.removeAttribute("src");
      outputPreviewBox.style.display = "none";
      metadata.style.display = "none";
      recommendation.style.display = "none";
      outputMetadata.style.display = "none";
      outputMetadataTitle.style.display = "none";
      progress.style.display = "none";
      progressBar.style.width = "0%";
      setStatus("loading", "Membaca video", "Mengupload preview dan membaca metadata.");

      try {
        await inspectFile(file);
      } catch (error) {
        setStatus("error", "Gagal membaca video", error.message);
      }
    }

    input.addEventListener("change", () => setFile(input.files[0]));

    const dropzoneActiveClasses = ["-translate-y-px", "border-teal-600", "bg-teal-50"];

    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add(...dropzoneActiveClasses);
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove(...dropzoneActiveClasses);
      });
    });

    dropzone.addEventListener("drop", (event) => setFile(event.dataTransfer.files[0]));

    inputPreview.addEventListener("error", () => {
      setStatus("error", "Preview tidak bisa diputar", "Preview input tidak bisa diputar di browser ini. Biasanya karena codec asli seperti HEVC/H.265, tapi Optimize Video tetap bisa dijalankan.");
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = input.files[0];
      if (!file) {
        setStatus("error", "Video belum dipilih", "Pilih video terlebih dahulu.");
        return;
      }

      button.disabled = true;
      result.style.display = "none";
      setProgress(5);
      setStatus("loading", "Mengupload", "Mengupload video.");

      try {
        const body = new FormData();
        if (previewId) {
          body.append("preview_id", previewId);
        } else {
          body.append("video", file);
        }
        const formData = new FormData(form);
        body.append("preset", formData.get("preset") || "safe-default");
        body.append("mode", formData.get("mode") || "fit");
        body.append("quality", formData.get("quality") || "safe");
        const response = await fetch("/optimize", { method: "POST", body });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || "Proses gagal.");
        }

        setProgress(data.progress || 20);
        previewId = null;
        setStatus("loading", "Menunggu proses", data.message || "Upload selesai. Menunggu proses optimize.");
        pollTimer = setInterval(() => {
          pollJob(data.job_id).catch((error) => {
            clearInterval(pollTimer);
            pollTimer = null;
            setStatus("error", "Status job gagal", error.message);
            button.disabled = false;
          });
        }, 1000);
        await pollJob(data.job_id);
      } catch (error) {
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
        setStatus("error", "Proses gagal", error.message);
        button.disabled = false;
      }
    });

    document.querySelectorAll('input[name="preset"]').forEach((field) => {
      field.addEventListener("change", () => applyPreset(field));
    });
    applyPreset(document.querySelector('input[name="preset"]:checked'));

    deleteButton.addEventListener("click", async () => {
      if (!outputFilename) return;

      deleteButton.disabled = true;
      try {
        const response = await fetch(`/output/${encodeURIComponent(outputFilename)}`, { method: "DELETE" });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || "Gagal menghapus hasil.");
        }

        outputFilename = null;
        downloadLink.removeAttribute("href");
        outputPreview.removeAttribute("src");
        outputPreviewBox.style.display = "none";
        outputMetadata.style.display = "none";
        outputMetadataTitle.style.display = "none";
        result.style.display = "none";
        setStatus("idle", "Hasil dihapus", "Hasil sudah dihapus. Pilih video lagi atau jalankan optimize ulang.");
      } catch (error) {
        setStatus("error", "Gagal menghapus hasil", error.message);
      } finally {
        deleteButton.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


class OptimizerHandler(BaseHTTPRequestHandler):
    server_version = "TikTokOptimizer/1.0"

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def write_response(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_html(self, body, status=HTTPStatus.OK):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.write_response(encoded)

    def send_html_headers(self, body, status=HTTPStatus.OK):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()

    def send_json(self, payload, status=HTTPStatus.OK):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.write_response(encoded)

    def serve_video_file(self, file_path, send_body=True, attachment_name=None):
        file_size = file_path.stat().st_size
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK

        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                raw_start, raw_end = match.groups()
                if raw_start:
                    start = int(raw_start)
                if raw_end:
                    end = int(raw_end)
                if start > end or start >= file_size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                end = min(end, file_size - 1)
                status = HTTPStatus.PARTIAL_CONTENT

        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        if attachment_name:
            self.send_header("Content-Disposition", f'attachment; filename="{html.escape(attachment_name)}"')
        self.end_headers()

        if not send_body:
            return

        try:
            with file_path.open("rb") as source:
                source.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.write_response(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        if self.path == "/":
            self.send_html(page())
            return

        if self.path == "/health":
            self.send_json({
                "status": "ok",
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "ffprobe": bool(shutil.which("ffprobe")),
            })
            return

        if self.path.startswith("/jobs/"):
            self.handle_job_status()
            return

        if self.path.startswith("/preview/"):
            self.handle_preview()
            return

        if self.path.startswith("/output/"):
            self.handle_output()
            return

        if self.path.startswith("/download/"):
            self.handle_download()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        if self.path == "/":
            self.send_html_headers(page())
            return

        if self.path == "/health":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return

        if self.path.startswith("/download/"):
            self.handle_download(send_body=False)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path == "/inspect":
            self.handle_inspect()
            return

        if self.path == "/optimize":
            self.handle_optimize()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        if self.path.startswith("/output/"):
            self.handle_delete_output()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def parse_multipart(self):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            self.send_json({"error": "Upload kosong."}, HTTPStatus.BAD_REQUEST)
            return None

        if content_length > MAX_UPLOAD_BYTES:
            self.send_json({"error": "File terlalu besar. Maksimal 2 GB."}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json({"error": "Request harus berupa multipart upload."}, HTTPStatus.BAD_REQUEST)
            return None

        return cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(content_length),
            },
        )

    def handle_inspect(self):
        cleanup_old_files()
        form = self.parse_multipart()
        if form is None:
            return

        field = form["video"] if "video" in form else None
        if field is None or not getattr(field, "filename", None):
            self.send_json({"error": "File video tidak ditemukan."}, HTTPStatus.BAD_REQUEST)
            return

        UPLOAD_DIR.mkdir(exist_ok=True)
        preview_id = uuid.uuid4().hex
        stem = safe_stem(field.filename)
        suffix = Path(field.filename).suffix or ".mp4"
        input_path = UPLOAD_DIR / f"{stem}-preview-{preview_id[:10]}{suffix}"

        try:
            with input_path.open("wb") as target:
                shutil.copyfileobj(field.file, target)

            metadata = validate_video(input_path)
            recommendation = recommend_preset(metadata)
            now = time.time()
            with PREVIEWS_LOCK:
                PREVIEWS[preview_id] = {
                    "path": input_path,
                    "filename": field.filename,
                    "metadata": metadata,
                    "recommendation": recommendation,
                    "created_at": now,
                    "updated_at": now,
                }

            self.send_json({
                "preview_id": preview_id,
                "preview_url": f"/preview/{preview_id}",
                "metadata": metadata,
                "recommended_preset": recommendation["preset"],
                "recommendation_reason": recommendation["reason"],
            })
        except ValueError as exc:
            input_path.unlink(missing_ok=True)
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            input_path.unlink(missing_ok=True)
            raise

    def handle_optimize(self):
        cleanup_old_files()

        form = self.parse_multipart()
        if form is None:
            return

        preset_field = form["preset"] if "preset" in form else None
        preset = preset_field.value if preset_field is not None else "safe-default"
        if preset not in PRESETS:
            self.send_json({"error": "Preset tidak valid."}, HTTPStatus.BAD_REQUEST)
            return

        mode_field = form["mode"] if "mode" in form else None
        mode = mode_field.value if mode_field is not None else PRESETS[preset]["mode"]
        if mode not in MODES:
            self.send_json({"error": "Mode output tidak valid."}, HTTPStatus.BAD_REQUEST)
            return

        quality_field = form["quality"] if "quality" in form else None
        quality = quality_field.value if quality_field is not None else PRESETS[preset]["quality"]
        if quality not in QUALITIES:
            self.send_json({"error": "Quality tidak valid."}, HTTPStatus.BAD_REQUEST)
            return

        UPLOAD_DIR.mkdir(exist_ok=True)
        OUTPUT_DIR.mkdir(exist_ok=True)

        upload_id = uuid.uuid4().hex[:10]
        job_id = uuid.uuid4().hex
        preview_id_field = form["preview_id"] if "preview_id" in form else None
        preview_id = preview_id_field.value if preview_id_field is not None else None

        if preview_id:
            with PREVIEWS_LOCK:
                preview = PREVIEWS.pop(preview_id, None)
            if not preview or not preview["path"].is_file():
                self.send_json({"error": "Preview tidak ditemukan. Pilih ulang video."}, HTTPStatus.BAD_REQUEST)
                return
            input_path = preview["path"]
            stem = safe_stem(preview["filename"])
        else:
            field = form["video"] if "video" in form else None
            if field is None or not getattr(field, "filename", None):
                self.send_json({"error": "File video tidak ditemukan."}, HTTPStatus.BAD_REQUEST)
                return
            stem = safe_stem(field.filename)
            suffix = Path(field.filename).suffix or ".mp4"
            input_path = UPLOAD_DIR / f"{stem}-{upload_id}{suffix}"

        output_name = f"{stem}-tiktok-{mode}-{quality}-{upload_id}.mp4"
        output_path = OUTPUT_DIR / output_name

        try:
            if not preview_id:
                with input_path.open("wb") as target:
                    shutil.copyfileobj(field.file, target)

            now = time.time()
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "status": "queued",
                    "stage": "queued",
                    "progress": 20,
                    "message": "Upload selesai. Job masuk antrean.",
                    "created_at": now,
                    "updated_at": now,
                    "mode": mode,
                    "quality": quality,
                    "preset": preset,
                    "filename": output_name,
                }

            thread = threading.Thread(
                target=run_optimize_job,
                args=(job_id, input_path, output_path, output_name, mode, quality),
                daemon=True,
            )
            thread.start()

            self.send_json({
                "job_id": job_id,
                "status_url": f"/jobs/{job_id}",
                "status": "queued",
                "progress": 20,
                "message": "Upload selesai. Job masuk antrean.",
            }, HTTPStatus.ACCEPTED)
        except Exception:
            input_path.unlink(missing_ok=True)
            raise

    def handle_job_status(self):
        job_id = Path(unquote(self.path.removeprefix("/jobs/"))).name
        job = public_job(job_id)
        if not job:
            self.send_json({"error": "Job tidak ditemukan."}, HTTPStatus.NOT_FOUND)
            return

        self.send_json(job)

    def handle_preview(self):
        preview_id = Path(unquote(self.path.removeprefix("/preview/"))).name
        with PREVIEWS_LOCK:
            preview = PREVIEWS.get(preview_id)
            if preview:
                preview["updated_at"] = time.time()

        if not preview or not preview["path"].is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.serve_video_file(preview["path"])

    def handle_output(self):
        filename = Path(unquote(self.path.removeprefix("/output/"))).name
        file_path = OUTPUT_DIR / filename
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.serve_video_file(file_path)

    def handle_delete_output(self):
        filename = Path(unquote(self.path.removeprefix("/output/"))).name
        file_path = OUTPUT_DIR / filename
        if not file_path.is_file():
            self.send_json({"error": "File hasil tidak ditemukan."}, HTTPStatus.NOT_FOUND)
            return

        file_path.unlink()
        with JOBS_LOCK:
            for job in JOBS.values():
                if job.get("filename") == filename:
                    job["download_url"] = None
                    job["output_url"] = None
                    job["message"] = "File hasil sudah dihapus."
                    job["updated_at"] = time.time()

        self.send_json({"ok": True, "filename": filename})

    def handle_download(self, send_body=True):
        filename = Path(unquote(self.path.removeprefix("/download/"))).name
        file_path = OUTPUT_DIR / filename
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.serve_video_file(file_path, send_body=send_body, attachment_name=filename)


def main():
    if not SCRIPT_PATH.is_file():
        raise SystemExit("tiktok-video-optimize.sh tidak ditemukan.")

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg belum terinstall. Install dulu: sudo apt install ffmpeg")

    if not shutil.which("ffprobe"):
        raise SystemExit("ffprobe belum terinstall. Biasanya ikut paket ffmpeg.")

    cleanup_old_files()

    server = ThreadingHTTPServer(("127.0.0.1", 8000), OptimizerHandler)
    print("TikTok Video Optimizer berjalan di http://127.0.0.1:8000")
    print("Tekan Ctrl+C untuk berhenti.")
    server.serve_forever()


if __name__ == "__main__":
    main()

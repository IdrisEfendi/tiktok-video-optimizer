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
        metadata = validate_video(input_path)
        duration = metadata.get("duration") or 0

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

        update_job(
            job_id,
            status="done",
            stage="done",
            progress=100,
            message="Selesai. File hasil sudah siap diunduh.",
            download_url=f"/download/{output_name}",
            output_url=f"/output/{output_name}",
            filename=output_name,
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
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7fb;
      --panel: #ffffff;
      --ink: #16181d;
      --muted: #677084;
      --line: #dfe3ea;
      --accent: #0d9488;
      --accent-strong: #0f766e;
      --danger: #b42318;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }

    main {
      width: min(920px, calc(100% - 32px));
      margin: 0 auto;
      padding: 40px 0;
    }

    header {
      margin-bottom: 24px;
    }

    h1 {
      margin: 0 0 8px;
      font-size: 32px;
      line-height: 1.1;
      letter-spacing: 0;
    }

    p {
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }

    .tool {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 300px;
      gap: 18px;
      align-items: start;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 12px 34px rgba(22, 24, 29, .06);
    }

    .dropzone {
      display: grid;
      place-items: center;
      min-height: 280px;
      border: 1.5px dashed #aab4c3;
      border-radius: 8px;
      background: #fbfcfe;
      text-align: center;
      padding: 24px;
      cursor: pointer;
    }

    .dropzone:hover,
    .dropzone.dragover {
      border-color: var(--accent);
      background: #f0fdfa;
    }

    .dropzone strong {
      display: block;
      margin-bottom: 6px;
      font-size: 18px;
    }

    input[type="file"] {
      position: absolute;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }

    .filename {
      min-height: 24px;
      margin: 14px 0;
      color: var(--ink);
      overflow-wrap: anywhere;
    }

    .preview-grid {
      display: none;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin: 14px 0;
    }

    .preview-box {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #0f1115;
    }

    .preview-box video {
      display: block;
      width: 100%;
      aspect-ratio: 9 / 16;
      background: #0f1115;
      object-fit: contain;
    }

    .preview-label {
      padding: 8px 10px;
      background: #ffffff;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }

    .metadata {
      display: none;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 14px 0;
    }

    .metadata-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: #fbfcfe;
      min-width: 0;
    }

    .metadata-item span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    .metadata-item strong {
      display: block;
      margin-top: 2px;
      color: var(--ink);
      font-size: 14px;
      overflow-wrap: anywhere;
    }

    .mode-group {
      display: grid;
      gap: 8px;
      margin: 14px 0;
    }

    .mode-title {
      color: var(--muted);
      font-size: 14px;
      font-weight: 700;
    }

    .mode-options {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .mode-option input {
      position: absolute;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }

    .mode-option span {
      display: grid;
      place-items: center;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--ink);
      font-weight: 700;
      cursor: pointer;
      text-align: center;
      padding: 8px;
    }

    .mode-option input:checked + span {
      border-color: var(--accent);
      background: #f0fdfa;
      color: var(--accent-strong);
    }

    button, .download {
      width: 100%;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      font: inherit;
      font-weight: 700;
      padding: 13px 16px;
      cursor: pointer;
      text-align: center;
      text-decoration: none;
    }

    button:hover, .download:hover {
      background: var(--accent-strong);
    }

    button:disabled {
      cursor: not-allowed;
      opacity: .55;
    }

    .settings {
      display: grid;
      gap: 12px;
    }

    .metric {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 14px;
    }

    .metric strong {
      color: var(--ink);
      text-align: right;
    }

    .status {
      min-height: 44px;
      margin-top: 14px;
      padding: 12px;
      border-radius: 8px;
      background: #f8fafc;
      color: var(--muted);
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .status.error {
      background: #fff1f0;
      color: var(--danger);
    }

    .progress {
      display: none;
      height: 10px;
      margin-top: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: #e5e7eb;
    }

    .progress-bar {
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
      transition: width .2s ease;
    }

    .result {
      display: none;
      margin-top: 12px;
    }

    @media (max-width: 760px) {
      main {
        width: min(100% - 24px, 920px);
        padding: 24px 0;
      }

      h1 { font-size: 26px; }

      .tool {
        grid-template-columns: 1fr;
      }

      .dropzone {
        min-height: 220px;
      }

      .preview-grid,
      .metadata {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>TikTok Video Optimizer</h1>
      <p>Upload video, lalu generate file MP4 vertikal 1080x1920 yang lebih aman untuk TikTok.</p>
    </header>

    <section class="tool">
      <form class="panel" id="uploadForm">
        <label class="dropzone" id="dropzone" for="videoInput">
          <span>
            <strong>Pilih atau tarik video ke sini</strong>
            <span>MP4, MOV, MKV, atau format lain yang didukung FFmpeg</span>
          </span>
        </label>
        <input id="videoInput" name="video" type="file" accept="video/*" required>
        <div class="filename" id="filename">Belum ada file dipilih.</div>
        <div class="preview-grid" id="previewGrid">
          <div class="preview-box">
            <video id="inputPreview" controls muted playsinline></video>
            <div class="preview-label">Input</div>
          </div>
          <div class="preview-box" id="outputPreviewBox" style="display: none;">
            <video id="outputPreview" controls playsinline></video>
            <div class="preview-label">Output</div>
          </div>
        </div>
        <div class="metadata" id="metadata"></div>
        <div class="mode-group">
          <div class="mode-title">Mode output</div>
          <div class="mode-options">
            <label class="mode-option">
              <input type="radio" name="mode" value="fit" checked>
              <span>Fit</span>
            </label>
            <label class="mode-option">
              <input type="radio" name="mode" value="crop">
              <span>Crop</span>
            </label>
            <label class="mode-option">
              <input type="radio" name="mode" value="blur">
              <span>Blur</span>
            </label>
          </div>
        </div>
        <div class="mode-group">
          <div class="mode-title">Quality</div>
          <div class="mode-options">
            <label class="mode-option">
              <input type="radio" name="quality" value="safe" checked>
              <span>Safe</span>
            </label>
            <label class="mode-option">
              <input type="radio" name="quality" value="high">
              <span>High</span>
            </label>
            <label class="mode-option">
              <input type="radio" name="quality" value="standard">
              <span>Standard</span>
            </label>
          </div>
        </div>
        <button id="optimizeButton" type="submit">Optimize Video</button>
        <div class="status" id="status">Siap memproses video.</div>
        <div class="progress" id="progress">
          <div class="progress-bar" id="progressBar"></div>
        </div>
        <div class="result" id="result">
          <a class="download" id="downloadLink" href="#">Download Hasil</a>
        </div>
      </form>

      <aside class="panel settings" aria-label="Standar output">
        <div class="metric"><span>Format</span><strong>MP4</strong></div>
        <div class="metric"><span>Resolusi</span><strong>1080 x 1920</strong></div>
        <div class="metric"><span>Frame rate</span><strong>30 fps</strong></div>
        <div class="metric"><span>Video codec</span><strong>H.264</strong></div>
        <div class="metric"><span>Audio</span><strong>AAC 192 kbps</strong></div>
        <div class="metric"><span>Loudness</span><strong>-14 LUFS</strong></div>
        <div class="metric"><span>Default quality</span><strong>Safe</strong></div>
        <div class="metric"><span>Cleanup</span><strong>otomatis</strong></div>
      </aside>
    </section>
  </main>

  <script>
    const form = document.querySelector("#uploadForm");
    const input = document.querySelector("#videoInput");
    const dropzone = document.querySelector("#dropzone");
    const filename = document.querySelector("#filename");
    const button = document.querySelector("#optimizeButton");
    const statusBox = document.querySelector("#status");
    const result = document.querySelector("#result");
    const downloadLink = document.querySelector("#downloadLink");
    const progress = document.querySelector("#progress");
    const progressBar = document.querySelector("#progressBar");
    const previewGrid = document.querySelector("#previewGrid");
    const inputPreview = document.querySelector("#inputPreview");
    const outputPreview = document.querySelector("#outputPreview");
    const outputPreviewBox = document.querySelector("#outputPreviewBox");
    const metadata = document.querySelector("#metadata");
    let pollTimer = null;
    let previewId = null;

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

    function renderMetadata(info) {
      const items = [
        ["Resolusi", `${info.width || "-"} x ${info.height || "-"}`],
        ["Durasi", formatDuration(info.duration)],
        ["Video", info.video_codec || "-"],
        ["Audio", info.has_audio ? (info.audio_codec || "ada") : "tanpa audio"],
        ["Frame rate", formatRate(info.avg_frame_rate)],
        ["Pixel", info.pix_fmt || "-"],
        ["Warna", [info.color_space, info.color_transfer, info.color_primaries].filter(Boolean).join(" / ") || "-"],
        ["Ukuran", formatBytes(info.size)],
      ];

      metadata.innerHTML = items.map(([label, value]) => `
        <div class="metadata-item">
          <span>${label}</span>
          <strong>${value}</strong>
        </div>
      `).join("");
      metadata.style.display = "grid";
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
      statusBox.textContent = data.message || "Memproses video.";

      if (data.status === "done") {
        clearInterval(pollTimer);
        pollTimer = null;
        downloadLink.href = data.download_url;
        outputPreview.src = data.output_url || data.download_url;
        outputPreviewBox.style.display = "block";
        result.style.display = "block";
        button.disabled = false;
        return;
      }

      if (data.status === "error") {
        clearInterval(pollTimer);
        pollTimer = null;
        statusBox.className = "status error";
        statusBox.textContent = data.error || data.message || "Proses gagal.";
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
      renderMetadata(data.metadata);
      statusBox.textContent = "Metadata siap. Pilih mode dan quality, lalu Optimize Video.";
    }

    async function setFile(file) {
      if (!file) return;
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      previewId = null;
      filename.textContent = file.name;
      result.style.display = "none";
      outputPreview.removeAttribute("src");
      outputPreviewBox.style.display = "none";
      metadata.style.display = "none";
      progress.style.display = "none";
      progressBar.style.width = "0%";
      statusBox.className = "status";
      statusBox.textContent = "Mengupload preview dan membaca metadata.";

      try {
        await inspectFile(file);
      } catch (error) {
        statusBox.className = "status error";
        statusBox.textContent = error.message;
      }
    }

    input.addEventListener("change", () => setFile(input.files[0]));

    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });

    dropzone.addEventListener("drop", (event) => setFile(event.dataTransfer.files[0]));

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = input.files[0];
      if (!file) {
        statusBox.className = "status error";
        statusBox.textContent = "Pilih video terlebih dahulu.";
        return;
      }

      button.disabled = true;
      result.style.display = "none";
      setProgress(5);
      statusBox.className = "status";
      statusBox.textContent = "Mengupload video.";

      try {
        const body = new FormData();
        if (previewId) {
          body.append("preview_id", previewId);
        } else {
          body.append("video", file);
        }
        const formData = new FormData(form);
        body.append("mode", formData.get("mode") || "fit");
        body.append("quality", formData.get("quality") || "safe");
        const response = await fetch("/optimize", { method: "POST", body });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || "Proses gagal.");
        }

        setProgress(data.progress || 20);
        previewId = null;
        statusBox.textContent = data.message || "Upload selesai. Menunggu proses optimize.";
        pollTimer = setInterval(() => {
          pollJob(data.job_id).catch((error) => {
            clearInterval(pollTimer);
            pollTimer = null;
            statusBox.className = "status error";
            statusBox.textContent = error.message;
            button.disabled = false;
          });
        }, 1000);
        await pollJob(data.job_id);
      } catch (error) {
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
        statusBox.className = "status error";
        statusBox.textContent = error.message;
        button.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


class OptimizerHandler(BaseHTTPRequestHandler):
    server_version = "TikTokOptimizer/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_html(self, body, status=HTTPStatus.OK):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

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
        self.wfile.write(encoded)

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
            now = time.time()
            with PREVIEWS_LOCK:
                PREVIEWS[preview_id] = {
                    "path": input_path,
                    "filename": field.filename,
                    "metadata": metadata,
                    "created_at": now,
                    "updated_at": now,
                }

            self.send_json({
                "preview_id": preview_id,
                "preview_url": f"/preview/{preview_id}",
                "metadata": metadata,
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

        mode_field = form["mode"] if "mode" in form else None
        mode = mode_field.value if mode_field is not None else "fit"
        if mode not in MODES:
            self.send_json({"error": "Mode output tidak valid."}, HTTPStatus.BAD_REQUEST)
            return

        quality_field = form["quality"] if "quality" in form else None
        quality = quality_field.value if quality_field is not None else "safe"
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

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(preview["path"].stat().st_size))
        self.end_headers()
        with preview["path"].open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def handle_output(self):
        filename = Path(unquote(self.path.removeprefix("/output/"))).name
        file_path = OUTPUT_DIR / filename
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()
        with file_path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def handle_download(self, send_body=True):
        filename = Path(unquote(self.path.removeprefix("/download/"))).name
        file_path = OUTPUT_DIR / filename
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{html.escape(filename)}"')
        self.end_headers()
        if not send_body:
            return

        with file_path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)


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

# TikTok Video Optimizer

Tool ini mengubah video agar lebih aman untuk upload TikTok:

- 1080 × 1920 vertical
- MP4 H.264
- 30 fps
- Bitrate video ±12 Mbps
- Audio AAC 192 kbps
- `faststart` supaya MP4 lebih kompatibel

## Cara pakai

### Web upload

Jalankan server lokal:

```bash
python3 web-optimize.py
```

Buka:

```text
http://127.0.0.1:8000
```

Pilih/upload video, lalu tool akan menampilkan preview input, metadata, dan rekomendasi preset otomatis berdasarkan codec, warna, durasi, ukuran, dan resolusi input. Setelah itu pilih preset atau atur mode output dan quality, lalu klik tombol **Optimize Video**. Progress proses akan tampil sampai preview output, metadata output, dan tombol download hasil muncul. File hasil bisa dihapus dari UI dengan tombol **Hapus Hasil**.

Preset:

- `Safe Default`: mode `Fit` + quality `Safe`, pilihan utama untuk upload lintas iPhone/Android.
- `Small File`: mode `Fit` + quality `Standard`, untuk file lebih kecil.
- `High Detail`: mode `Blur` + quality `High`, untuk video yang butuh detail lebih tinggi.

Mode output:

- `Fit`: seluruh video terlihat, area kosong ditambahkan jika rasio tidak 9:16.
- `Crop`: video memenuhi layar 9:16, sebagian tepi bisa terpotong.
- `Blur`: video utama tetap utuh, background blur mengisi layar 9:16.

Quality:

- `Safe`: default. 10 Mbps, H.264, yuv420p, Rec.709 metadata, dan tonemap HDR/BT.2020 ke SDR jika terdeteksi. Ini paling disarankan untuk upload lintas iPhone/Android.
- `High`: 12 Mbps, cocok untuk video detail tinggi.
- `Standard`: 8 Mbps, file lebih kecil dan upload lebih ringan.

### Command line

```bash
./tiktok-video-optimize.sh video-asli.mov video-tiktok.mp4
```

Kalau output tidak diisi, file otomatis dibuat dengan akhiran `-tiktok.mp4`.

Pilih mode dan quality lewat `--mode` dan `--quality`:

```bash
./tiktok-video-optimize.sh video-asli.mov video-tiktok.mp4 --mode blur --quality safe
```

Mode yang tersedia: `fit`, `crop`, `blur`. Default-nya `fit`.

Quality yang tersedia: `safe`, `high`, `standard`. Default-nya `safe`.

### Health check

```text
http://127.0.0.1:8000/health
```

Endpoint ini mengembalikan status server dan ketersediaan `ffmpeg`/`ffprobe`.

## Syarat

Butuh `ffmpeg` dan `ffprobe`.

Ubuntu/Linux Mint:

```bash
sudo apt install ffmpeg
```

## Catatan

Tool ini tidak bisa menjamin TikTok 100% tidak kompres, karena TikTok tetap melakukan kompresi server-side. Tapi format ini mengurangi risiko video jadi pecah/buram.

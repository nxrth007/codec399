# codec399

A small local web app that re-encodes any video with the "399 fps mosaic" methodology:
upload a file, let it process, and download the result from the Finished tab.

The methodology was reverse-engineered from two reference files by decoding every frame, parsing
every VP9 frame header, and walking the MP4 box tree. This is what those files contain and what the
tool reproduces:

| Property | Reference files | codec399 |
|---|---|---|
| Container | MP4 `isom/iso2/mp41`, faststart (ftyp, moov, free, mdat) | same |
| Video | VP9 profile 0, yuv420p, limited range, tag `vp09`, 1:1 `pasp`, `vpcC` subsampling 0 | same |
| Frame rate | constant 399 fps, track timescale 12768 (399 x 32) | same |
| Keyframes | at the start and every 60.000 s (slot 23940), landing on a mosaic slot | same |
| Source cadence | 30 fps; each source frame shown for exactly one 2.5 ms slot | same |
| Sharp slot placement | `slot(k) = round((k / 30 - 0.018) * 399)`, `slot(0) = 0` | same, 0 misses |
| Stream length | the source duration in slots; later frames dropped | same |
| Held image | one frozen mosaic for the whole file | same |
| Mosaic source | the frame at 25 % of the frame count | same |
| Mosaic look | 54 px block average (20 blocks across 1080 px), then `Y' = 0.5175 (Y - 128) + 185.5`, chroma scaled by 0.5175 about neutral | same, fits to 1.7 luma levels |
| Quantiser | index 94-97 on sharp frames | CRF 24 (index 96) |
| Tiles / parallel mode | single tile, frame-parallel off | same |
| Reference scheme | hidden alt-ref per group, hold frames coded as ~30 byte copies, held image drift 0.03 | 2-pass alt-ref, ~23 byte copies, drift 0.01 |
| Audio | AAC-LC stereo 96 kHz, FFmpeg's AAC encoder, 512 kbps target (lands at 334-415) | same |
| Metadata | compressor name `Lavc61.19.100 libeve-vp9`, `udta` zeroed and renamed `skip` (129 bytes) | same |
| Bitrate at 1080x1920 | 14-16 Mbps | about 11-12 Mbps at the same quantiser |

Because the sharp frames sit 18 ms before each 30 fps instant, a 60 Hz player that presents the
latest decoded frame at each refresh shows the real frame on every other refresh. Anything that
samples on the 30 fps grid, or picks the frame nearest each 30 fps tick, only ever sees the mosaic.

## Requirements

* Python 3.9 or newer
* FFmpeg and ffprobe on your PATH, built with `libvpx-vp9` (the usual Windows, macOS and Linux
  builds all have it). Set `FFMPEG` / `FFPROBE` environment variables to point at other binaries.

```bash
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Then open http://127.0.0.1:5399. On Windows you can double-click `run.bat`. Set `PORT` to use a
different port.

1. **Upload** tab: click Upload (or drop a file), then Process video.
2. **Processing** tab: progress bar with a time estimate. Jobs run one at a time and queue.
3. **Finished** tab: Download, plus a summary of the stream that was produced.

Faithful mode is a two-pass encode and runs at about 12 slots per second for a 1080x1920 source,
so a two minute clip takes around an hour. The "Fast encode" checkbox uses a single realtime
pass and is several times quicker at lower quality and without the alt-ref reference scheme.

Finished outputs live in `jobs/<id>/output.mp4` and are listed again after a restart; uploads are
deleted once a job finishes.

## Command line

```bash
python pipeline.py input.mp4 output.mp4 [--fast]
python verify.py output.mp4
```

`verify.py` decodes a file and checks the structure: 30 single-slot sharp frames per second, every
one on the predicted slot, a frozen held image, a single keyframe, and it prints the container
details next to the reference values.

## What still differs from the reference files

* The reference files were encoded with Eve, a commercial VP9 encoder from Two Orioles. This tool
  uses FFmpeg's `libvpx-vp9` with matching stream parameters and reference scheme. At the same
  quantiser libvpx spends fewer bytes (about 11-12 Mbps versus 14-16), and small header fields
  such as the interpolation filter choice and motion-vector precision are the encoder's own.
* The compressor-name string in the container is written to match the reference files. It is a
  label only; change `ENCODER_TAG` in `pipeline.py` if you prefer the truthful `libvpx` tag.
* The 25 % mosaic frame rule is exact in one reference file and within two frames in the other
  (the frames there are near-identical, so it cannot be pinned tighter).

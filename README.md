# codec399

A small local web app that re-encodes any video with the "399 fps mosaic" methodology:
upload a file, let it process, and download the result from the Finished tab.

The methodology was reverse-engineered frame by frame from two reference files. What those files
contain, and what this tool reproduces:

| Property | Value |
|---|---|
| Container | MP4 (`isom`, `isomiso2mp41`) |
| Video | VP9 profile 0, yuv420p, limited range, tag `vp09` |
| Frame rate | constant 399 fps, track timescale 12768 (399 x 32) |
| Keyframes | one, at the start |
| Source cadence | 30 fps; each source frame is shown for exactly one 2.5 ms slot |
| Sharp slot placement | `slot(k) = round((k / 30 - 0.018) * 399)`, `slot(0) = 0` |
| Everything else | one frozen mosaic frame, identical for the whole file |
| Mosaic source | the frame at 24 % of the clip duration |
| Mosaic look | 54 px block average on a 1080 px wide frame (20 blocks across), then `Y' = 0.548 Y + 118.3`, chroma scaled by 0.49 around neutral |
| Stream length | ends where source frame N would have landed |
| Audio | AAC-LC stereo, 96 kHz |
| Bitrate | about 14 to 16 Mbps for 1080x1920 (CRF 20 here) |

The slot formula matched all 6,565 sharp frames in the two reference files with zero misses, and the
mosaic model fits their frozen frame to within about two luma levels.

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
2. **Processing** tab: progress bar and time estimate. Jobs run one at a time and queue.
3. **Finished** tab: Download, plus a summary of the stream that was produced.

Encoding speed is about 45 slots per second for a 1080x1920 source in faithful mode, so a two minute
clip takes 15 to 20 minutes. The "Fast encode" checkbox uses libvpx's realtime mode and is several
times quicker at lower quality.

Finished outputs live in `jobs/<id>/output.mp4` and are listed again after a restart; uploads are
deleted once a job finishes.

## Command line

```bash
python pipeline.py input.mp4 output.mp4 [--fast]
python verify.py output.mp4
```

`verify.py` decodes a file and checks the structure: 30 single-slot sharp frames per second, every
one on the predicted slot, a frozen held image, and a single keyframe.

## What differs from the reference files

* The reference files were encoded with Eve, a commercial VP9 encoder from Two Orioles
  (`libeve-vp9` in the container tag). This tool uses FFmpeg's `libvpx-vp9` with matching stream
  parameters, so the structure and playback behaviour are the same but the encoder tag reads
  `libvpx`.
* The 24 % position of the mosaic reference frame is inferred from the two samples (24.07 % and
  24.33 %), which is the best available rule.
* The reference streams end 10 to 15 ms before where this tool ends them.
* Inputs are kept at their own resolution (made even if needed). The mosaic block size scales with
  width so the 20-blocks-across look is preserved; for 1080 px wide input it is exactly 54 px.

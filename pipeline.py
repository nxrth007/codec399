"""codec399 - re-encode any video with the frozen-mosaic / single-slot methodology.

Everything below was measured from the two reference files (CODEC.mp4 and
CODEC2.mp4) by decoding every frame, parsing every VP9 frame header and walking
the MP4 box tree.  What this module reproduces:

Stream structure
* 399 fps VP9 profile 0, yuv420p, limited range, MP4 track timescale 12768
  (= 399 * 32), a keyframe at the start and every 60 s (slot 23940) after it.
* The source is normalised to 30 fps.  Source frame k is shown for exactly one
  399 fps slot, placed 18 ms before its nominal instant:
      slot(k) = round((k / 30 - 0.018) * 399)      (slot(0) = 0)
  This matched all 6,565 sharp frames of the reference files with no misses.
* The stream length is the source duration in slots; frames whose slot falls
  beyond it are dropped (the reference files end on the source duration, not on
  the last frame).
* Every other slot holds one frozen "mosaic" image for the whole file.

Mosaic
* Built from the source frame at 25 % of the frame count (frame 868 of 3472 in
  CODEC.mp4 is exactly one quarter).
* Block average on a 54 px grid anchored at the top-left (20 blocks across a
  1080 px frame; partial edge blocks are averaged on their own), then a plain
  contrast/brightness collapse applied in YUV:
      Y' = 0.5175 * (Y - 128) + 128 + 57.5        C' = 0.5175 * (C - 128) + 128
  That fits both reference mosaics to within two luma levels (their own VP9
  quantisation noise is about half a level).

Encoder behaviour (Eve VP9 in the originals, libvpx-vp9 here)
* Quantiser index 94-97 on the shown sharp frames -> CRF 24 (index 96).
* Single tile, frame-parallel mode off, no error resilience, a hidden
  alt-ref frame per group so that hold frames are coded as 20-30 byte copies
  of a reference that is never overwritten -> 2-pass with auto-alt-ref and no
  alt-ref filtering.  Hold packets come out at ~23 bytes (Eve: ~30) and the
  held image drifts by 0.01 luma levels across the file (Eve: 0.03).
* AAC-LC stereo at 96 kHz from FFmpeg's own AAC encoder with a 512 kbps target,
  which that encoder undershoots to whatever the content needs (the reference
  files land at 334 and 415 kbps with a 512000 max-bitrate field).

Container
* FFmpeg MP4 muxer with faststart (ftyp, moov, free, mdat), 1:1 `pasp`,
  `vpcC` chroma-subsampling code 0, compressor name string, and a 129 byte
  `udta` box that was zeroed and renamed to `skip` after muxing (see
  mp4patch.py, which also mirrors the bitrate fields).
"""
import json
import os
import subprocess
import threading

import numpy as np

import audiocloak
import mp4patch

SLOT_FPS = 399
SRC_FPS = 30
TIMESCALE = 12768
LEAD_SEC = 0.018
REF_POS = 0.25
BLOCKS_ACROSS = 20
CONTRAST = 0.5175
BRIGHTNESS = 57.5
AUDIO_RATE = 96000
AUDIO_BITRATE = "512k"
CRF = 24
KEYFRAME_SLOTS = 60 * SLOT_FPS             # both reference files have their second keyframe at exactly 60.000 s
ENCODER_TAG = "Lavc61.19.100 libeve-vp9"   # compressor-name string found in the reference files (label only)
UDTA_PAD = "x" * 15                        # sizes the muxer's udta/meta box to the reference 129 bytes before it is zeroed

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


class PipelineError(RuntimeError):
    pass


def sharp_slot(k):
    """399 fps slot index that shows 30 fps source frame k."""
    if k == 0:
        return 0
    return int(round((k / SRC_FPS - LEAD_SEC) * SLOT_FPS))


def total_slots(duration):
    """Stream length in slots for a source of the given duration in seconds."""
    return max(1, int(round(duration * SLOT_FPS)))


def block_size(width):
    return max(1, int(round(width / BLOCKS_ACROSS)))


def probe(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise PipelineError("The file has no video stream.")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    w, h = int(video["width"]), int(video["height"])
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    if abs(rotation) % 180 == 90:
        w, h = h, w
    w -= w % 2
    h -= h % 2
    duration = float(video.get("duration") or info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise PipelineError("Could not read the video duration.")
    return {"width": w, "height": h, "duration": duration, "has_audio": has_audio,
            "src_fps": video.get("avg_frame_rate"), "codec": video.get("codec_name")}


def _block_mean(plane, b):
    """Average every b x b block (partial blocks at the edges are averaged too)."""
    h, w = plane.shape
    ri = np.arange(0, h, b)
    ci = np.arange(0, w, b)
    sums = np.add.reduceat(np.add.reduceat(plane.astype(np.float64), ri, axis=0), ci, axis=1)
    rh = np.diff(np.append(ri, h))
    cw = np.diff(np.append(ci, w))
    means = sums / (rh[:, None] * cw[None, :])
    return np.repeat(np.repeat(means, rh, axis=0), cw, axis=1)


def build_mosaic(frame_yuv420p, width, height):
    """Turn one raw yuv420p frame into the frozen mosaic image (raw yuv420p bytes)."""
    b = block_size(width)
    ysz = width * height
    csz = ysz // 4
    buf = np.frombuffer(frame_yuv420p, dtype=np.uint8)
    y = buf[:ysz].reshape(height, width)
    u = buf[ysz:ysz + csz].reshape(height // 2, width // 2)
    v = buf[ysz + csz:ysz + 2 * csz].reshape(height // 2, width // 2)
    y2 = CONTRAST * (_block_mean(y, b) - 128) + 128 + BRIGHTNESS
    u2 = CONTRAST * (_block_mean(u, max(1, b // 2)) - 128) + 128
    v2 = CONTRAST * (_block_mean(v, max(1, b // 2)) - 128) + 128
    out = np.concatenate([np.rint(p).clip(0, 255).astype(np.uint8).ravel() for p in (y2, u2, v2)])
    return out.tobytes()


def _decode_filter(w, h):
    return f"fps={SRC_FPS}:round=near,scale={w}:{h}:flags=bicubic,format=yuv420p"


def _count_frames(src, w, h):
    """Number of frames the 30 fps decode will produce."""
    res = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-nostats", "-i", src, "-vf", _decode_filter(w, h),
         "-f", "null", "-progress", "pipe:1", "-"],
        capture_output=True, text=True,
    )
    frames = 0
    for line in res.stdout.splitlines():
        if line.startswith("frame="):
            frames = int(line[6:])
    if frames <= 0:
        raise PipelineError("The decoder produced no frames: " + res.stderr.strip())
    return frames


def _grab_frame(src, w, h, index):
    fsz = w * h * 3 // 2
    res = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-i", src, "-vf", _decode_filter(w, h) + f",select='eq(n,{index})'",
         "-vsync", "0", "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
        capture_output=True,
    )
    if res.returncode != 0 or len(res.stdout) < fsz:
        raise PipelineError("Could not decode the mosaic reference frame from the input.")
    return res.stdout[:fsz]


def _drain(stream, sink):
    for line in iter(stream.readline, b""):
        sink.append(line)
    stream.close()


def _stream_slots(src, w, h, mosaic, n_slots, enc_cmd, state, progress_key, on_progress, cancel=None):
    """Decode the source at 30 fps and feed the 399 fps slot stream into `enc_cmd`.

    Progress is counted from the slots written: the pipe only buffers a few frames ahead of
    the encoder, and in the first pass ffmpeg reports no frames at all."""
    fsz = w * h * 3 // 2
    dec = subprocess.Popen(
        [FFMPEG, "-v", "error", "-nostdin", "-i", src, "-vf", _decode_filter(w, h), "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dec_err, enc_err = [], []
    threads = [
        threading.Thread(target=_drain, args=(dec.stderr, dec_err), daemon=True),
        threading.Thread(target=_drain, args=(enc.stderr, enc_err), daemon=True),
        threading.Thread(target=_drain, args=(enc.stdout, []), daemon=True),
    ]
    for t in threads:
        t.start()
    slot = 0
    k = 0
    cancelled = False

    def tick():
        state[progress_key] = slot
        if on_progress:
            on_progress(state)

    try:
        while slot < n_slots:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            frame = dec.stdout.read(fsz)
            if len(frame) < fsz:
                break
            target = sharp_slot(k)
            if target >= n_slots:
                break
            while slot < target:
                enc.stdin.write(mosaic)
                slot += 1
            enc.stdin.write(frame)
            slot += 1
            k += 1
            state["frames_in"] = k
            if k % 4 == 0:
                tick()
        while slot < n_slots and not cancelled:
            enc.stdin.write(mosaic)
            slot += 1
    finally:
        if cancelled:
            enc.kill()
        try:
            enc.stdin.close()
        except OSError:
            pass
        dec.stdout.close()
        dec.kill()
        dec.wait()
        enc.wait()
        for t in threads:
            t.join(timeout=5)
    if cancelled:
        raise PipelineError("Cancelled.")
    if enc.returncode != 0:
        raise PipelineError("Encoder failed: " + b"".join(enc_err).decode("utf-8", "replace").strip())
    tick()
    return k


def _encoder_args(w, h, fast, passno, passlog):
    args = ["-vf", "setsar=1", "-c:v", "libvpx-vp9", "-profile:v", "0", "-pix_fmt", "yuv420p",
            "-color_range", "tv", "-chroma_sample_location", "left", "-b:v", "0", "-crf", str(CRF),
            "-tile-columns", "0", "-frame-parallel", "0", "-row-mt", "1", "-threads", str(os.cpu_count() or 4),
            "-g", str(KEYFRAME_SLOTS), "-keyint_min", str(KEYFRAME_SLOTS),
            "-r", str(SLOT_FPS), "-video_track_timescale", str(TIMESCALE)]
    if fast:
        args += ["-deadline", "realtime", "-cpu-used", "8", "-auto-alt-ref", "0", "-lag-in-frames", "0"]
    else:
        args += ["-deadline", "good", "-cpu-used", "4", "-auto-alt-ref", "1", "-lag-in-frames", "25",
                 "-arnr-maxframes", "1", "-arnr-strength", "0", "-pass", str(passno), "-passlogfile", passlog]
    return args


def process(src, dst, on_progress=None, fast=False, cancel=None, encode_audio=False):
    """Encode `src` to `dst`.  on_progress(state) receives a dict with keys
    stage, pass1, pass2, est_slots, frames_in, progress.  `cancel` is an optional
    threading.Event; setting it aborts the encode with PipelineError("Cancelled.").
    `encode_audio` runs the soundtrack through audiocloak before it is muxed; the
    video is unaffected.  Returns a summary dict."""
    info = probe(src)
    w, h = info["width"], info["height"]
    n_slots = total_slots(info["duration"])
    state = {"stage": "preparing", "pass1": 0, "pass2": 0, "est_slots": n_slots, "frames_in": 0, "progress": 0.0}

    def report(stage=None):
        if stage:
            state["stage"] = stage
        done = (0.08 * state["pass1"] + 0.92 * state["pass2"]) if not fast else state["pass2"]
        state["progress"] = min(0.999, done / n_slots)
        if on_progress:
            on_progress(state)

    report("preparing")
    n_frames = _count_frames(src, w, h)
    ref_index = min(n_frames - 1, int(round(n_frames * REF_POS)))
    mosaic = build_mosaic(_grab_frame(src, w, h, ref_index), w, h)

    audio_src = src
    cloaked_wav = None
    cloak_audio = bool(info["has_audio"] and encode_audio)
    if cloak_audio:
        if cancel is not None and cancel.is_set():
            raise PipelineError("Cancelled.")
        report("audio")
        cloaked_wav = dst + ".audio.wav"
        audiocloak.process(src, cloaked_wav, FFMPEG, FFPROBE)
        audio_src = cloaked_wav

    head = [FFMPEG, "-v", "error", "-nostats", "-nostdin", "-y",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", f"{w}x{h}",
            "-framerate", str(SLOT_FPS), "-i", "pipe:0"]
    passlog = dst + ".2pass"

    if not fast:
        report("analysing")
        _stream_slots(src, w, h, mosaic, n_slots,
                      head + _encoder_args(w, h, False, 1, passlog) + ["-an", "-f", "null", os.devnull],
                      state, "pass1", lambda s: report(), cancel)
        state["pass1"] = n_slots

    report("encoding")
    cmd = head[:]
    if info["has_audio"]:
        cmd += ["-i", audio_src, "-map", "0:v:0", "-map", "1:a:0"]
    cmd += _encoder_args(w, h, fast, 2, passlog)
    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-ar", str(AUDIO_RATE), "-ac", "2", "-b:a", AUDIO_BITRATE]
    cmd += ["-metadata:s:v:0", "encoder=" + ENCODER_TAG, "-metadata", "comment=" + UDTA_PAD,
            "-movflags", "+faststart", dst]
    try:
        frames_used = _stream_slots(src, w, h, mosaic, n_slots, cmd, state, "pass2", lambda s: report(), cancel)
    finally:
        for fn in (passlog + "-0.log", passlog + "-0.log.mbtree", passlog, cloaked_wav):
            if fn is None:
                continue
            try:
                os.remove(fn)
            except OSError:
                pass

    mp4patch.patch(dst)
    state["pass2"] = n_slots
    report("done")
    return {
        "width": w, "height": h, "source_frames": frames_used, "slots": n_slots,
        "output_duration": n_slots / SLOT_FPS, "mosaic_ref_frame": ref_index, "mosaic_ref_time": ref_index / SRC_FPS,
        "block_px": block_size(w), "has_audio": info["has_audio"], "output_bytes": os.path.getsize(dst),
        "source_codec": info["codec"], "source_fps": info["src_fps"], "fast": fast,
        "encode_audio": cloak_audio,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python pipeline.py input.mp4 output.mp4 [--fast] [--encode-audio]")
        sys.exit(2)

    def _cb(s):
        print(f"\r{s['stage']}: {s['progress'] * 100:5.1f}%", end="", flush=True)

    summary = process(sys.argv[1], sys.argv[2], _cb, fast="--fast" in sys.argv,
                      encode_audio="--encode-audio" in sys.argv)
    print()
    print(json.dumps(summary, indent=2))

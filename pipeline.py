"""codec399 - re-encode any video with the frozen-mosaic / single-slot methodology.

Structure reproduced (measured from the two reference files):

* Output is a 399 fps VP9 (profile 0, yuv420p, limited range) stream in MP4,
  track timescale 12768 (= 399 * 32), one keyframe at the start only.
* The source is normalised to 30 fps.  Source frame k is shown for exactly one
  399 fps slot, placed 18 ms *before* its nominal instant:
      slot(k) = round((k / 30 - 0.018) * 399)      (slot(0) = 0)
  This matched all 6,565 sharp frames of the reference files with no misses.
* Every other slot holds one frozen "mosaic" image for the whole file: a 54 px
  block average (20 blocks across a 1080 px frame) of the frame sitting at 24 %
  of the clip duration, with contrast collapsed:
      Y' = 0.548 * Y + 118.3        U',V' = 0.49 * (C - 128) + 128
* Audio is re-encoded to AAC-LC stereo at 96 kHz.

The reference files were produced with the Eve VP9 encoder from Two Orioles,
which is commercial.  This module uses the libvpx-vp9 encoder in FFmpeg with
matching stream parameters, so the stream structure is identical; only the
encoder name tag in the container differs.
"""
import json
import os
import subprocess
import threading

import numpy as np

SLOT_FPS = 399
SRC_FPS = 30
TIMESCALE = 12768
LEAD_SEC = 0.018
REF_POS = 0.24
BLOCKS_ACROSS = 20
Y_GAIN, Y_OFFSET = 0.548, 118.3
C_GAIN = 0.49
AUDIO_RATE = 96000
AUDIO_BITRATE = "384k"
CRF = 20

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


class PipelineError(RuntimeError):
    pass


def sharp_slot(k):
    """399 fps slot index that shows 30 fps source frame k."""
    if k == 0:
        return 0
    return int(round((k / SRC_FPS - LEAD_SEC) * SLOT_FPS))


def total_slots(n_frames):
    """Stream length in slots for n source frames (where frame n would land)."""
    return int(round((n_frames / SRC_FPS - LEAD_SEC) * SLOT_FPS))


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
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
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
    y2 = Y_GAIN * _block_mean(y, b) + Y_OFFSET
    u2 = C_GAIN * (_block_mean(u, max(1, b // 2)) - 128) + 128
    v2 = C_GAIN * (_block_mean(v, max(1, b // 2)) - 128) + 128
    out = np.concatenate([np.rint(p).clip(0, 255).astype(np.uint8).ravel() for p in (y2, u2, v2)])
    return out.tobytes()


def _decode_filter(w, h):
    return f"fps={SRC_FPS}:round=near,scale={w}:{h}:flags=bicubic,format=yuv420p"


def _grab_reference(src, w, h, t_ref):
    fsz = w * h * 3 // 2
    for t in (t_ref, 0.0):
        res = subprocess.run(
            [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", src, "-vf", _decode_filter(w, h),
             "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
            capture_output=True,
        )
        if res.returncode == 0 and len(res.stdout) >= fsz:
            return res.stdout[:fsz], t
    raise PipelineError("Could not decode a reference frame from the input.")


def _drain(stream, sink):
    for line in iter(stream.readline, b""):
        sink.append(line)
    stream.close()


def _progress_reader(stream, state, on_progress):
    for line in iter(stream.readline, b""):
        line = line.decode("utf-8", "replace").strip()
        if line.startswith("frame="):
            try:
                state["encoded"] = int(line[6:])
            except ValueError:
                continue
            if on_progress:
                on_progress(state)
    stream.close()


def process(src, dst, on_progress=None, fast=False):
    """Encode `src` to `dst`.  on_progress(state) receives a dict with keys
    stage, encoded, est_slots, frames_in.  Returns a summary dict."""
    info = probe(src)
    w, h = info["width"], info["height"]
    fsz = w * h * 3 // 2
    est_frames = max(1, int(round(info["duration"] * SRC_FPS)))
    state = {"stage": "preparing", "encoded": 0, "est_slots": total_slots(est_frames), "frames_in": 0}
    if on_progress:
        on_progress(state)

    ref, t_ref = _grab_reference(src, w, h, REF_POS * info["duration"])
    mosaic = build_mosaic(ref, w, h)

    dec = subprocess.Popen(
        [FFMPEG, "-v", "error", "-nostdin", "-i", src, "-vf", _decode_filter(w, h),
         "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    enc_cmd = [FFMPEG, "-v", "error", "-nostats", "-y",
               "-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", f"{w}x{h}",
               "-framerate", str(SLOT_FPS), "-i", "pipe:0"]
    if info["has_audio"]:
        enc_cmd += ["-i", src, "-map", "0:v:0", "-map", "1:a:0"]
    enc_cmd += ["-c:v", "libvpx-vp9", "-profile:v", "0", "-pix_fmt", "yuv420p", "-color_range", "tv",
                "-b:v", "0", "-crf", str(CRF),
                "-deadline", "realtime" if fast else "good", "-cpu-used", "8" if fast else "4",
                "-row-mt", "1", "-threads", str(os.cpu_count() or 4),
                "-g", "999999", "-keyint_min", "999999", "-auto-alt-ref", "0", "-lag-in-frames", "0",
                "-r", str(SLOT_FPS), "-video_track_timescale", str(TIMESCALE)]
    if info["has_audio"]:
        enc_cmd += ["-c:a", "aac", "-ar", str(AUDIO_RATE), "-ac", "2", "-b:a", AUDIO_BITRATE]
    enc_cmd += ["-progress", "pipe:1", dst]
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    dec_err, enc_err = [], []
    threads = [
        threading.Thread(target=_drain, args=(dec.stderr, dec_err), daemon=True),
        threading.Thread(target=_drain, args=(enc.stderr, enc_err), daemon=True),
        threading.Thread(target=_progress_reader, args=(enc.stdout, state, on_progress), daemon=True),
    ]
    for t in threads:
        t.start()

    state["stage"] = "encoding"
    slot = 0
    k = 0
    try:
        while True:
            frame = dec.stdout.read(fsz)
            if len(frame) < fsz:
                break
            target = sharp_slot(k)
            while slot < target:
                enc.stdin.write(mosaic)
                slot += 1
            enc.stdin.write(frame)
            slot += 1
            k += 1
            state["frames_in"] = k
        n_frames = k
        if n_frames == 0:
            raise PipelineError("The decoder produced no frames: " + b"".join(dec_err).decode("utf-8", "replace"))
        end = total_slots(n_frames)
        while slot < end:
            enc.stdin.write(mosaic)
            slot += 1
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        dec.stdout.close()
        dec.wait()
        enc.wait()
        for t in threads:
            t.join(timeout=5)

    if enc.returncode != 0:
        raise PipelineError("Encoder failed: " + b"".join(enc_err).decode("utf-8", "replace").strip())
    state["stage"] = "done"
    state["encoded"] = slot
    if on_progress:
        on_progress(state)
    return {
        "width": w, "height": h, "source_frames": n_frames, "slots": slot,
        "output_duration": slot / SLOT_FPS, "mosaic_ref_time": t_ref, "block_px": block_size(w),
        "has_audio": info["has_audio"], "output_bytes": os.path.getsize(dst),
        "source_codec": info["codec"], "source_fps": info["src_fps"],
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python pipeline.py input.mp4 output.mp4 [--fast]")
        sys.exit(2)

    def _cb(s):
        print(f"\r{s['stage']}: {s['encoded']}/{s['est_slots']} slots", end="", flush=True)

    summary = process(sys.argv[1], sys.argv[2], _cb, fast="--fast" in sys.argv)
    print()
    print(json.dumps(summary, indent=2))

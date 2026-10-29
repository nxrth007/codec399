"""Optional "Encode Audio" stage.

Applies the middle-ground audio encode (a dialled-down phonetic frequency inversion blended with
pitch jitter, micro-gapping and a procedural noise floor) to a video's soundtrack before it is
muxed into the output.  The DSP is a straight port of audio_edit_v1.py; ffmpeg replaces librosa
and soundfile for decoding and writing, so the only extra dependencies are numpy and scipy.

As in the original, the soundtrack is mixed down to mono at its native sample rate and the result
is peak-normalised to 1.0.  pipeline.process encodes that mono track to stereo AAC as usual.
"""
import json
import os
import subprocess

import numpy as np
from scipy import signal


class AudioEncodeError(RuntimeError):
    pass


def cloak(audio, sr):
    """Return the cloaked copy of a mono float waveform `audio` sampled at `sr` Hz."""
    audio = np.asarray(audio, dtype=np.float64)
    n_steps = len(audio)
    if n_steps < 2:
        return audio
    t = np.arange(n_steps) / sr
    nyquist = sr / 2

    # Feature 1: phonetic frequency inversion at 1/8th strength.
    carrier_freq = 2800
    modulator = np.sin(2 * np.pi * carrier_freq * t)
    raw_inversion = audio * modulator
    b_inv, a_inv = signal.butter(4, min(3200 / nyquist, 0.99), btype="low")
    filtered_inversion = signal.lfilter(b_inv, a_inv, raw_inversion)
    scaled_inversion = filtered_inversion * 0.125

    # Feature 2: asynchronous pitch jitter and timeline warping.
    vibrato_mod = 1.0 + 0.012 * np.sin(2 * np.pi * 8.0 * t) * np.cos(2 * np.pi * 3.5 * t)
    old_indices = np.arange(n_steps)
    new_indices = np.cumsum(vibrato_mod)
    new_indices = (new_indices / new_indices[-1]) * (n_steps - 1)
    base_mix = audio + scaled_inversion
    warped_wave = np.interp(new_indices, old_indices, base_mix)

    # Feature 3: micro-gapping, 3 ms of silence every 1.5 s.
    gap_interval = int(1.5 * sr)
    gap_duration = int(0.003 * sr)
    for i in range(gap_interval, n_steps, gap_interval):
        if i + gap_duration < n_steps:
            warped_wave[i:i + gap_duration] = 0.0

    # Feature 4: protective noise and procedural hum floor.
    uneven_noise = np.random.normal(0, 1, n_steps)
    b_pink, a_pink = signal.butter(1, min(1000 / nyquist, 0.99), btype="low")
    pink_noise = signal.lfilter(b_pink, a_pink, uneven_noise)
    ambient_hum = np.sin(2 * np.pi * 45.0 * t) * np.cos(2 * np.pi * 1.2 * t)
    crackle = np.random.choice([0, 1], size=n_steps, p=[0.9996, 0.0004]) * np.random.normal(0, 0.15, n_steps)
    ambient_layer = ambient_hum * 0.015 + crackle * 0.04

    return warped_wave + (pink_noise * 0.012) + ambient_layer


def normalize(wave):
    """Peak-normalise to 1.0, as librosa.util.normalize does by default."""
    peak = float(np.max(np.abs(wave))) if len(wave) else 0.0
    return wave / peak if peak > 0 else wave


def sample_rate(src, ffprobe="ffprobe"):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
         "-print_format", "json", src],
        capture_output=True, text=True, check=True,
    ).stdout
    streams = json.loads(out).get("streams") or []
    if not streams:
        raise AudioEncodeError("The file has no audio stream.")
    return int(streams[0]["sample_rate"])


def decode_mono(src, sr, ffmpeg="ffmpeg"):
    """Decode the first audio stream to a mono float32 array at `sr` Hz."""
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-i", src, "-map", "0:a:0", "-vn",
         "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AudioEncodeError("ffmpeg could not decode the audio: " + proc.stderr.decode(errors="replace").strip())
    return np.frombuffer(proc.stdout, dtype=np.float32)


def write_wav(wave, sr, dst, ffmpeg="ffmpeg"):
    data = np.asarray(wave, dtype=np.float32).tobytes()
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
         "-c:a", "pcm_f32le", dst],
        input=data, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AudioEncodeError("ffmpeg could not write the audio: " + proc.stderr.decode(errors="replace").strip())


def process(src, dst, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """Cloak the soundtrack of `src` and write it as a mono WAV to `dst`.  Returns a summary dict."""
    sr = sample_rate(src, ffprobe)
    audio = decode_mono(src, sr, ffmpeg)
    if len(audio) == 0:
        raise AudioEncodeError("The audio stream is empty.")
    cloaked = normalize(cloak(audio, sr))
    write_wav(cloaked, sr, dst, ffmpeg)
    return {"sample_rate": sr, "samples": int(len(audio)), "output_bytes": os.path.getsize(dst)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python audioencode.py input.(mp4|wav) output.wav")
        sys.exit(2)
    print(json.dumps(process(sys.argv[1], sys.argv[2]), indent=2))

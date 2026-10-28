"""Check that a video has the codec399 structure.  usage: python verify.py file.mp4"""
import json
import os
import subprocess
import sys

import numpy as np

import pipeline

W, H = 90, 160


def main(path):
    info = json.loads(subprocess.run(
        [pipeline.FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, check=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    print(f"video: {v['codec_name']} {v.get('profile')} {v['width']}x{v['height']} {v['r_frame_rate']} fps "
          f"pix_fmt={v['pix_fmt']} range={v.get('color_range')} timebase={v['time_base']} tag={v.get('codec_tag_string')}")
    print(f"audio: {a['codec_name'] if a else 'none'} {a['sample_rate'] + ' Hz ' + str(a['channels']) + 'ch' if a else ''}")
    print(f"encoder tag: {v.get('tags', {}).get('encoder')}")

    raw = subprocess.run([pipeline.FFMPEG, "-v", "error", "-i", path, "-vf", f"scale={W}:{H},format=gray",
                          "-f", "rawvideo", "pipe:1"], capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W).astype(np.int16)
    n = len(fr)
    d = np.zeros(n); d[1:] = np.abs(fr[1:] - fr[:-1]).mean(axis=(1, 2))
    d2 = np.zeros(n); d2[1:-1] = np.abs(fr[2:] - fr[:-2]).mean(axis=(1, 2))
    sharp = np.zeros(n, bool)
    sharp[1:-1] = (d[1:-1] >= 0.5) & (d[2:] >= 0.5) & (d2[1:-1] < 0.5)
    sharp[0] = True
    si = np.where(sharp)[0]
    pred = np.array([pipeline.sharp_slot(k) for k in range(len(si))])
    held = fr[~sharp]
    dh = np.abs(np.diff(held, axis=0)).mean(axis=(1, 2)) if len(held) > 1 else np.zeros(0)
    flags = subprocess.run([pipeline.FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "packet=flags", "-of", "csv=p=0", path],
                           capture_output=True, text=True).stdout.split()
    keyframes = [f for f in flags if f.startswith("K")]
    gaps = np.diff(si)
    print(f"slots: {n} ({n / pipeline.SLOT_FPS:.3f} s)")
    print(f"single-slot sharp frames: {len(si)} -> {len(si) / (n / pipeline.SLOT_FPS):.2f} per second")
    print(f"gap between sharp frames (slots): {sorted(set(gaps.tolist()))}")
    print(f"sharp slots matching slot(k) = round((k/30 - 0.018)*399): {(pred == si).sum()} of {len(si)}")
    print(f"expected stream length for {len(si)} frames: {pipeline.total_slots(len(si))} slots (actual {n})")
    changes = int((dh >= 1.0).sum())
    print(f"held image: {changes} content changes over the file; encoder drift between held slots "
          f"max {dh.max() if len(dh) else 0:.2f} luma levels (reference files: 0.03, libvpx: about 0.3)")
    print(f"mean luma: sharp {fr[sharp].mean():.1f}, held {held.mean():.1f}")
    print(f"keyframes: {len(keyframes)}")
    ok = (pred == si).all() and changes == 0 and len(keyframes) == 1
    print("RESULT:", "structure matches" if ok else "structure differs")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or not os.path.isfile(sys.argv[1]):
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))

"""Check that a video has the codec399 structure.  usage: python verify.py file.mp4"""
import json
import os
import struct
import subprocess
import sys

import numpy as np

import pipeline

W, H = 90, 160


def _boxes(buf, start, end):
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[off:off + 8])
        hs = 8
        if size == 1:
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            hs = 16
        elif size == 0:
            size = end - off
        yield typ, off + hs, off + size
        off += size


def _find(buf, path, start, end):
    for typ, body, bend in _boxes(buf, start, end):
        if typ == path[0]:
            if len(path) == 1:
                yield body, bend
            else:
                yield from _find(buf, path[1:], body + {b"meta": 4, b"stsd": 8}.get(typ, 0), bend)


def container_report(path):
    with open(path, "rb") as fh:
        buf = fh.read()
    order = [t.decode("latin1") for t, _, _ in _boxes(buf, 0, len(buf))]
    udta = []
    for body, bend in _find(buf, [b"moov", b"udta"], 0, len(buf)):
        for t, b, e in _boxes(buf, body, bend):
            udta.append((t.decode("latin1"), e - b + 8, set(buf[b:e]) == {0}))
    vp09 = {}
    for body, bend in _find(buf, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"], 0, len(buf)):
        for t, b, e in _boxes(buf, body + 8, bend):
            if t == b"vp09":
                name = buf[b + 42:b + 74]
                vp09["compressorname"] = name[1:1 + name[0]].decode("latin1", "replace")
                for ct, cb, ce in _boxes(buf, b + 78, e):
                    vp09[ct.decode("latin1")] = buf[cb:ce].hex()
    return order, udta, vp09


def main(path):
    info = json.loads(subprocess.run(
        [pipeline.FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, check=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    print(f"video: {v['codec_name']} {v.get('profile')} {v['width']}x{v['height']} {v['r_frame_rate']} fps "
          f"pix_fmt={v['pix_fmt']} range={v.get('color_range')} timebase={v['time_base']} tag={v.get('codec_tag_string')}")
    print(f"audio: {a['codec_name'] if a else 'none'} {a['sample_rate'] + ' Hz ' + str(a['channels']) + 'ch ' + str(int(a.get('bit_rate', 0)) // 1000) + ' kbps' if a else ''}")
    print(f"encoder tag: {v.get('tags', {}).get('encoder')}")

    order, udta, vp09 = container_report(path)
    print(f"box order: {' '.join(order)}")
    print(f"udta children: {udta}  (reference: [('skip', 129, True)])")
    print(f"vp09 children: vpcC={vp09.get('vpcC')} pasp={vp09.get('pasp')} btrt={vp09.get('btrt')}")
    print(f"   (reference: vpcC=010000000028800202020000 pasp=0000000100000001 btrt=0000 max=avg)")

    flags = subprocess.run([pipeline.FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "packet=flags", "-of", "csv=p=0", path],
                           capture_output=True, text=True).stdout.split()
    keyframes = [f for f in flags if f.startswith("K")]

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
    gaps = np.diff(si)
    print(f"slots: {n} ({n / pipeline.SLOT_FPS:.3f} s)")
    print(f"single-slot sharp frames: {len(si)} -> {len(si) / (n / pipeline.SLOT_FPS):.2f} per second")
    print(f"gap between sharp frames (slots): {sorted(set(gaps.tolist()))}")
    print(f"sharp slots matching slot(k) = round((k/30 - 0.018)*399): {(pred == si).sum()} of {len(si)}")
    changes = int((dh >= 1.0).sum())
    print(f"held image: {changes} content changes over the file; encoder drift between held slots "
          f"max {dh.max() if len(dh) else 0:.2f} luma levels (reference files: 0.36 at the first held slot, 0.03 after)")
    print(f"mean luma: sharp {fr[sharp].mean():.1f}, held {held.mean():.1f}")
    key_idx = [i for i, f in enumerate(flags) if f.startswith("K")]
    expected_keys = list(range(0, n, pipeline.KEYFRAME_SLOTS))
    print(f"keyframes at slots {key_idx} (expected every 60 s: {expected_keys})")
    ok = (pred == si).all() and changes == 0 and key_idx == expected_keys
    print("RESULT:", "structure matches" if ok else "structure differs")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or not os.path.isfile(sys.argv[1]):
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))

"""In-place MP4 touch-ups that reproduce the reference files' container quirks.

The reference files were muxed by FFmpeg and then post-processed: the `udta/meta`
box that carries the muxer version was zeroed and renamed to `skip`, the video
`btrt` box carries the measured average bitrate in both fields, and the `mp4a`
sample-entry rate field is 0 (96 kHz does not fit the 16-bit field).  None of
these change sizes, so they are patched in place after muxing.
"""
import mmap
import struct


def _boxes(mm, start, end):
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", mm[off:off + 8])
        hs = 8
        if size == 1:
            size = struct.unpack(">Q", mm[off + 8:off + 16])[0]
            hs = 16
        elif size == 0:
            size = end - off
        if size < hs:
            return
        yield typ, off, off + hs, off + size
        off += size


def _find(mm, path, start, end):
    for typ, off, body, bend in _boxes(mm, start, end):
        if typ == path[0]:
            if len(path) == 1:
                yield off, body, bend
            else:
                skip = {b"meta": 4, b"stsd": 8}.get(typ, 0)
                yield from _find(mm, path[1:], body + skip, bend)


def _track_bitrate(mm, trak_body, trak_end):
    """Average bitrate of a track from its stsz sample sizes and mdhd duration."""
    total = 0
    for _, body, bend in _find(mm, [b"mdia", b"minf", b"stbl", b"stsz"], trak_body, trak_end):
        sample_size, count = struct.unpack(">II", mm[body + 4:body + 12])
        if sample_size:
            total = sample_size * count
        else:
            sizes = struct.unpack(f">{count}I", mm[body + 12:body + 12 + 4 * count])
            total = sum(sizes)
    for _, body, bend in _find(mm, [b"mdia", b"mdhd"], trak_body, trak_end):
        version = mm[body]
        if version == 0:
            timescale, duration = struct.unpack(">II", mm[body + 12:body + 20])
        else:
            timescale = struct.unpack(">I", mm[body + 20:body + 24])[0]
            duration = struct.unpack(">Q", mm[body + 24:body + 32])[0]
        if duration:
            return int(round(total * 8 * timescale / duration))
    return 0


def patch(path):
    with open(path, "r+b") as fh:
        mm = mmap.mmap(fh.fileno(), 0)
        try:
            size = mm.size()
            for _, moov_body, moov_end in _find(mm, [b"moov"], 0, size):
                # 1. udta/meta -> skip, payload zeroed
                for off, body, bend in _find(mm, [b"udta", b"meta"], moov_body, moov_end):
                    mm[off + 4:off + 8] = b"skip"
                    mm[body:bend] = bytes(bend - body)
                for _, trak_body, trak_end in _find(mm, [b"trak"], moov_body, moov_end):
                    for _, body, bend in _find(mm, [b"mdia", b"minf", b"stbl", b"stsd"], trak_body, trak_end):
                        for typ, eoff, ebody, eend in _boxes(mm, body + 8, bend):
                            if typ == b"vp09":
                                # 2. btrt: bufferSizeDB 0, max = avg = measured bitrate
                                rate = _track_bitrate(mm, trak_body, trak_end)
                                for ctyp, _, cbody, cend in _boxes(mm, ebody + 78, eend):
                                    if ctyp == b"btrt" and cend - cbody == 12:
                                        mm[cbody:cend] = struct.pack(">III", 0, rate, rate)
                            elif typ == b"mp4a":
                                # 3. sample rate field: 0 when it does not fit 16 bits
                                rate_field = struct.unpack(">I", mm[ebody + 24:ebody + 28])[0] >> 16
                                if rate_field:
                                    mm[ebody + 24:ebody + 28] = b"\x00\x00\x00\x00"
            mm.flush()
        finally:
            mm.close()


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        patch(p)
        print("patched", p)

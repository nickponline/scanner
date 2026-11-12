"""Decode the package emitted by main.swift and check it against CONTRACTS.md §2.
Run with `python3 -I verify.py <pkg_dir>` (stdlib only)."""
import base64, json, os, struct, sys, zlib

def crc32c_table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        t.append(c)
    return t
T = crc32c_table()

def crc32c(data, crc=0):
    crc ^= 0xFFFFFFFF
    for b in data:
        crc = T[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF

def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]

def check(cond, msg):
    if not cond:
        print("FAIL:", msg); sys.exit(1)

pkg = sys.argv[1]
check(crc32c(b"123456789") == 0xE3069283, "python crc32c self-test")

# --- depth ---
raw = open(os.path.join(pkg, "depth/seg_000.bin"), "rb").read()
off, recs = 0, []
while off < len(raw):
    check(raw[off:off+4] == b"SDP1", f"magic at {off}")
    i, t, w, h, n = struct.unpack_from("<IdHHI", raw, off + 4)
    off += 24
    payload = raw[off:off+n]; off += n
    check(payload[0] == 0x78, "zlib header byte")
    body = zlib.decompress(payload)  # zlib-wrapped, not raw deflate
    check(len(body) == w * h * 5, f"payload size {len(body)} vs {w*h*5}")
    depth = struct.unpack_from(f"<{w*h}f", body, 0)
    conf = body[w*h*4:]
    r = len(recs)
    for k in (0, 1, w*h//2, w*h-1):
        exp = f32(r + f32(k * f32(0.001)))  # same float32 ops as main.swift
        check(depth[k] == exp, f"depth[{r}][{k}] {depth[k]} != {exp}")
        check(conf[k] == (r + k) % 3, f"conf[{r}][{k}]")
    check(i == r * 3 and abs(t - (1000.5 + r / 10)) < 1e-12, "record header i/t")
    recs.append((i, w, h))
check(off == len(raw) and [x[1:] for x in recs] == [(4, 3), (256, 192), (256, 192)], "record layout")
print(f"depth: {len(recs)} records OK {recs}")

# --- sidecars ---
frames = [json.loads(l) for l in open(os.path.join(pkg, "frames/seg_000.jsonl"))]
check([f["i"] for f in frames] == list(range(9)), "frame indices")
check(len(frames[0]["T"]) == 16 and frames[0]["T"][12:15] == [0.5, 1.25, -2.0], "T column-major translation")
check(frames[4]["track"] == "limited:excessiveMotion" and frames[5]["iso"] is None, "track/iso")
check([f["i"] for f in frames if f["depth"]] == [r[0] for r in recs], "depth flags match depth records")
still = json.loads(open(os.path.join(pkg, "stills/stills.jsonl")).readline())
check(still["file"] == "stills/000000.jpg" and still["w"] == 4032, "still line")
imu = open(os.path.join(pkg, "imu/seg_000.csv")).read().splitlines()
check(imu[0] == "t,ax,ay,az,gx,gy,gz,grav_x,grav_y,grav_z,heading_deg" and len(imu) == 101
      and all(len(l.split(",")) == 11 for l in imu), "imu csv")
print(f"sidecars: {len(frames)} frames, imu {len(imu)-1} rows OK")

# --- ply ---
ply = open(os.path.join(pkg, "arkit_mesh.ply"), "rb").read()
hdr_end = ply.index(b"end_header\n") + len(b"end_header\n")
hdr = ply[:hdr_end].decode()
check("binary_little_endian" in hdr and "element vertex 7" in hdr and "element face 3" in hdr, "ply header")
body = ply[hdr_end:]
check(len(body) == 7 * 12 + 3 * 13, "ply body size")
faces = [struct.unpack_from("<BIII", body, 84 + 13 * k) for k in range(3)]
check(faces == [(3, 0, 1, 2), (3, 3, 4, 5), (3, 3, 5, 6)], f"ply faces {faces}")
print("ply OK")

# --- manifest ---
m = json.load(open(os.path.join(pkg, "manifest.json")))
check(m["schema"] == "capture_manifest.v1" and m["capture_source"] == "iphone_arkit", "manifest schema")
check(m["scan_id"].startswith("scan_") and len(m["scan_id"]) == 31, "scan_id")
paths = [f["path"] for f in m["files"]]
expected = sorted(["arkit_mesh.ply", "depth/seg_000.bin", "frames/seg_000.jsonl", "imu/seg_000.csv",
                   "stills/000000.jpg", "stills/stills.jsonl", "video/seg_000.mov"])
check(paths == expected, f"manifest files {paths}")
seg = m["segments"][0]
check(seg["video"] == "video/seg_000.mov" and seg["depth"] == "depth/seg_000.bin", "segment paths")
for f in m["files"]:
    data = open(os.path.join(pkg, f["path"]), "rb").read()
    check(f["size"] == len(data), f"size {f['path']}")
    want = base64.b64encode(struct.pack(">I", crc32c(data))).decode()
    check(f["crc32c"] == want, f"crc32c {f['path']} {f['crc32c']} != {want}")
print(f"manifest: {len(m['files'])} files, sizes + crc32c OK")

try:
    import google_crc32c  # optional cross-check (uv run --with google-crc32c)
    for f in m["files"]:
        data = open(os.path.join(pkg, f["path"]), "rb").read()
        check(base64.b64encode(google_crc32c.Checksum(data).digest()).decode() == f["crc32c"], "google-crc32c")
    print("google-crc32c cross-check OK")
except ImportError:
    pass
print("ALL OK")

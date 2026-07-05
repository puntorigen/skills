"""
Shared binary-PLY reader/writer for the object-to-3d Gaussian-splat scripts.

Brush exports a standard 3DGS binary_little_endian PLY. We parse it with a numpy
structured dtype built from the header, so the code is robust to property ORDER
(Brush emits the SH color coefficients first, NOT x/y/z) and preserves EVERY
property byte-for-byte on write - so a filtered `cleaned.ply` is still a fully
valid, colored, previewable splat.

Per Gaussian (SH degree d -> 3 + 3*((d+1)^2-1) + 1 + 4 + 3 + 3 float32 props):
    f_dc_0..2     DC spherical-harmonic color; rgb = 0.5 + 0.2820948*f_dc
    f_rest_*      higher-order SH (ignored by cleanup/mesh)
    opacity       logit; rendered alpha = sigmoid(opacity)
    rot_0..3      rotation quaternion (w, x, y, z)
    scale_0..2    log std-devs; linear axis length = exp(scale)
    x, y, z       Gaussian center

Only binary_little_endian is supported (what Brush writes).
"""

import sys
import numpy as np

SH_C0 = 0.28209479177387814  # Y_0^0, the DC spherical-harmonic basis constant

# PLY scalar type -> little-endian numpy dtype
_PLY_TO_NP = {
    "char": "<i1", "int8": "<i1",
    "uchar": "<u1", "uint8": "<u1",
    "short": "<i2", "int16": "<i2",
    "ushort": "<u2", "uint16": "<u2",
    "int": "<i4", "int32": "<i4",
    "uint": "<u4", "uint32": "<u4",
    "float": "<f4", "float32": "<f4",
    "double": "<f8", "float64": "<f8",
}
_NP_TO_PLY = {"i1": "char", "u1": "uchar", "i2": "short", "u2": "ushort",
              "i4": "int", "u4": "uint", "f4": "float", "f8": "double"}


class SplatPly:
    """A parsed Gaussian-splat PLY: structured array `data` + preserved comments."""

    def __init__(self, data, comments):
        self.data = data            # numpy structured array, one record per Gaussian
        self.comments = comments    # list[str], header comment lines (verbatim)

    # --- named-column accessors (float64 for math) ----------------------------
    @property
    def names(self):
        return list(self.data.dtype.names)

    def has(self, name):
        return name in self.data.dtype.names

    def col(self, name):
        return np.asarray(self.data[name], dtype=np.float64)

    def xyz(self):
        return np.stack([self.col("x"), self.col("y"), self.col("z")], axis=1)

    def opacity_alpha(self):
        """Rendered opacity = sigmoid(opacity logit)."""
        o = self.col("opacity")
        return 1.0 / (1.0 + np.exp(-o))

    def scales_linear(self):
        """Per-axis linear std-dev = exp(scale_i), shape (N, 3)."""
        return np.exp(np.stack(
            [self.col("scale_0"), self.col("scale_1"), self.col("scale_2")], axis=1))

    def quats_wxyz(self):
        """Normalized rotation quaternions (w, x, y, z), shape (N, 4)."""
        q = np.stack([self.col("rot_0"), self.col("rot_1"),
                      self.col("rot_2"), self.col("rot_3")], axis=1)
        n = np.linalg.norm(q, axis=1, keepdims=True)
        n[n == 0] = 1.0
        return q / n

    def rgb(self):
        """Base color in [0,1] from the DC SH coefficients, shape (N, 3)."""
        dc = np.stack([self.col("f_dc_0"), self.col("f_dc_1"), self.col("f_dc_2")], axis=1)
        return np.clip(0.5 + SH_C0 * dc, 0.0, 1.0)

    def __len__(self):
        return len(self.data)

    def select(self, mask_or_idx):
        """Return a new SplatPly keeping only the given rows (all props preserved)."""
        return SplatPly(self.data[mask_or_idx].copy(), list(self.comments))


def read_ply(path):
    """Parse a binary_little_endian Gaussian-splat PLY into a SplatPly."""
    with open(path, "rb") as f:
        raw = f.read()
    # header ends at the line "end_header"
    marker = b"end_header"
    idx = raw.find(marker)
    if idx < 0:
        raise ValueError(f"{path}: not a PLY (no end_header found)")
    hdr_end = raw.find(b"\n", idx)
    if hdr_end < 0:
        raise ValueError(f"{path}: malformed header")
    header = raw[:hdr_end].decode("ascii", errors="replace")
    body = raw[hdr_end + 1:]

    fmt = None
    count = None
    props = []          # list of (name, numpy_dtype_str)
    comments = []
    in_vertex = False
    for line in header.splitlines():
        line = line.strip()
        if not line:
            continue
        toks = line.split()
        key = toks[0]
        if key == "format":
            fmt = toks[1] if len(toks) > 1 else None
        elif key == "comment":
            comments.append(line[len("comment"):].strip())
        elif key == "element":
            in_vertex = (len(toks) > 1 and toks[1] == "vertex")
            if in_vertex:
                count = int(toks[2])
        elif key == "property" and in_vertex:
            if toks[1] == "list":
                raise ValueError(f"{path}: list properties are not supported")
            ply_type, name = toks[1], toks[2]
            np_dt = _PLY_TO_NP.get(ply_type)
            if np_dt is None:
                raise ValueError(f"{path}: unknown property type {ply_type!r}")
            props.append((name, np_dt))

    if fmt != "binary_little_endian":
        raise ValueError(f"{path}: unsupported PLY format {fmt!r} "
                         f"(only binary_little_endian; that is what Brush writes)")
    if count is None or not props:
        raise ValueError(f"{path}: no vertex element / properties in header")

    dtype = np.dtype([(n, dt) for n, dt in props])
    need = count * dtype.itemsize
    if len(body) < need:
        raise ValueError(f"{path}: truncated body ({len(body)} < {need} bytes)")
    data = np.frombuffer(body[:need], dtype=dtype, count=count).copy()
    return SplatPly(data, comments)


def write_ply(path, splat):
    """Write a SplatPly back out as binary_little_endian, preserving all props."""
    data = splat.data
    lines = ["ply", "format binary_little_endian 1.0"]
    for c in splat.comments:
        lines.append(f"comment {c}")
    lines.append(f"element vertex {len(data)}")
    for name in data.dtype.names:
        base = np.dtype(data.dtype[name]).str.lstrip("<>=|")
        ply_type = _NP_TO_PLY.get(base)
        if ply_type is None:
            raise ValueError(f"cannot map numpy dtype {base!r} back to a PLY type")
        lines.append(f"property {ply_type} {name}")
    lines.append("end_header\n")
    header = "\n".join(lines).encode("ascii")
    # numpy stores little-endian already (dtypes built with '<'); ensure it
    le = data.astype(data.dtype.newbyteorder("<"), copy=False)
    with open(path, "wb") as f:
        f.write(header)
        f.write(le.tobytes())


def eprint(*a):
    print(*a, file=sys.stderr)

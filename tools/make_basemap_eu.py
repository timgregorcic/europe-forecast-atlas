"""Crop the world basemap to Europe -> site/basemap-eu.json.

Input: basemap.json from https://github.com/tehoro/weather-explorer-web (site/basemap.json, MIT),
itself built from public-domain Natural Earth 1:50m data. Format there: {"scale": 100, "land":
[ring...], "borders": [line...]}, each a flat list of delta-encoded integer lon/lat pairs (1/100 deg).

Output: {"coast": [[lon, lat, lon, lat, ...], ...], "borders": [...]}, plain degrees rounded to
0.01, only the pieces that touch the Europe domain (with a margin), lines split where they leave it.

  python tools/make_basemap_eu.py path/to/basemap.json > site/basemap-eu.json
"""
import json
import sys

W, E, S, N = -45.0, 65.0, 18.0, 82.0  # a little wider than the data domain


def decode(flat, scale):
    """Points with longitude wrapped to [-180, 180). Some source rings (Eurasia) are stored
    unwrapped across the date line, with longitudes below -180."""
    pts, x, y = [], 0, 0
    for i in range(0, len(flat), 2):
        x += flat[i]
        y += flat[i + 1]
        pts.append((((x / scale) + 180) % 360 - 180, y / scale))
    return pts


def split_dateline(pts):
    """Split a line where it jumps across the date line after wrapping."""
    out, cur = [], [pts[0]]
    for a, b in zip(pts, pts[1:]):
        if abs(b[0] - a[0]) > 180:
            out.append(cur)
            cur = []
        cur.append(b)
    out.append(cur)
    return out


def clip_runs(pts):
    """Split a polyline into runs of points inside the box (keeping one point either side)."""
    inside = [W <= lo <= E and S <= la <= N for lo, la in pts]
    runs, cur = [], []
    for i, p in enumerate(pts):
        near = inside[i] or (i > 0 and inside[i - 1]) or (i + 1 < len(pts) and inside[i + 1])
        if near:
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) > 1]


def main():
    b = json.load(open(sys.argv[1]))
    sc = b["scale"]
    out = {}
    for key, name in (("land", "coast"), ("borders", "borders")):
        lines = []
        for flat in b[key]:
            for part in split_dateline(decode(flat, sc)):
                for run in clip_runs(part):
                    lines.append([v for p in run for v in (round(p[0], 2), round(p[1], 2))])
        out[name] = lines
    json.dump(out, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()

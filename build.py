#!/usr/bin/env python3
"""Builds the per-course hole map files from Geofabrik extracts.

  python3 build.py --work DIR --output DIR [--regions ID,ID | --input FILE ...]
                   [--region-index URL_OR_FILE] [--course-list URL_OR_FILE]
                   [--previous-build FILE] [--min-files N] [--report FILE]
                   [--dry-run]

Regions are filtered, cut to the area near golf courses and discarded one at
a time; the small near-course files are then merged, assigned to courses,
trimmed and written as <output>/v1/<course id>.json.gz. The set is built in
a staging folder under --work, checked by four publish guards, and moved to
--output (which must start absent or empty) only when all pass. Needs
osmium-tool on PATH. Python standard library only. The boxes, assign and
trim logic is the measured prototype's (FINDINGS F002-F008), kept
whole-in-memory.
"""
import argparse
import gzip
import json
import math
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from array import array
from datetime import datetime, timezone

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
COURSE_LIST_URL = "https://austinhector.github.io/roundreplay/courses-v1.json"
SPLIT_OVER = 6_000_000_000
MIN_FILES = 15_000       # guard 1
MAX_DROP = 0.05          # guard 1
MAX_MISSING = 0.01       # guard 3
MAX_FILE_BYTES = 1_000_000  # guard 4
# Geofabrik lists these beside their own parts (all of `us` beside every
# state), so downloading them only repeats data. `us-pacific` stays: it may
# hold territories no state file covers.
AGGREGATES = {"us", "us-midwest", "us-northeast", "us-south", "us-west",
              "alps", "britain-and-ireland", "dach", "great-britain", "sea"}
USER_AGENT = "roundreplay-holes-build (https://github.com/AustinHector/roundreplay-holes)"
RSS_UNIT = "bytes" if sys.platform == "darwin" else "KB"
RSS_SCALE = 1 if sys.platform == "darwin" else 1024

PAD = 0.002  # degrees, about 200 m
CELL = 0.05  # grid cell for the course index, degrees
PAD_M = 250.0
KEEP_TAGS = ("golf", "natural", "leisure", "name", "ref", "par", "handicap")
FEATURES = ("hole", "green", "tee", "fairway")
WATER = ("water_hazard", "lateral_water_hazard")

T0 = time.time()
STEPS = []      # one record per step
CMDS = []       # peak child RSS of commands in the step being timed
STATE = {"downloaded_bytes": 0, "download_seconds": 0.0, "min_free_bytes": None,
         "work": None, "failed_step": None}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{now()} +{time.time() - T0:7.0f}s] {msg}", flush=True)


def self_rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * RSS_SCALE


def children_rss():
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * RSS_SCALE


def sample_free_disk():
    """Free bytes on the work folder; keeps the lowest reading seen."""
    if not STATE["work"] or not os.path.isdir(STATE["work"]):
        return None
    free = shutil.disk_usage(STATE["work"]).free
    if STATE["min_free_bytes"] is None or free < STATE["min_free_bytes"]:
        STATE["min_free_bytes"] = free
    return free


class step:
    """Times a block; records wall seconds and peak RSS afterwards."""

    def __init__(self, name, region=None):
        self.name, self.region = name, region

    def __enter__(self):
        del CMDS[:]
        self.t = time.time()
        log(f"{self.region or 'all'}: {self.name} start")
        return self

    def __exit__(self, exc_type, exc, tb):
        free = sample_free_disk()
        if exc_type is not None and STATE["failed_step"] is None:
            STATE["failed_step"] = f"{self.region or 'all'}: {self.name}"
        rec = {"step": self.name, "region": self.region,
               "free_bytes": free,
               "seconds": round(time.time() - self.t, 1),
               "step_children_peak_rss_bytes": max(CMDS) if CMDS else 0,
               "cumulative_children_peak_rss_bytes": children_rss(),
               "script_peak_rss_bytes": self_rss(),
               "ok": exc_type is None}
        STEPS.append(rec)
        log(f"{self.region or 'all'}: {self.name} {'done' if exc_type is None else 'FAILED'} "
            f"{rec['seconds']}s children_peak={rec['step_children_peak_rss_bytes'] // 2**20}MB "
            f"script_peak={rec['script_peak_rss_bytes'] // 2**20}MB")
        return False


def run(cmd, capture=False):
    """Runs a command; os.wait4 gives that child's own peak RSS."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE if capture else None)
    out = p.stdout.read() if capture else b""
    _, status, usage = os.wait4(p.pid, 0)
    p.returncode = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
        else (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status))
    CMDS.append(usage.ru_maxrss * RSS_SCALE)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}")
    return out.decode("utf-8", "replace").strip()


def open_url(url, method="GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    for attempt in (1, 2, 3):
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError:
            raise
        except OSError as e:
            log(f"{method} {url} attempt {attempt} failed: {e}")
            if attempt == 3:
                raise
            time.sleep(5)


# --- Regions ----------------------------------------------------------------

def head_size(url):
    with open_url(url, "HEAD") as r:
        return int(r.headers["Content-Length"])


def load_index(src):
    if src.startswith(("http://", "https://")):
        with open_url(src) as r:
            return json.load(r)["features"]
    with open(src, encoding="utf-8") as f:
        return json.load(f)["features"]


def choose_regions(only, index_src):
    """Returns ([(id, url, size)], [{"region", "point"}] the shape check added)."""
    feats = load_index(index_src)
    props = [f["properties"] for f in feats]
    added = []
    if only:
        chosen = []
        for want in only:
            hit = next((p for p in props if p["id"] == want), None) \
                or next((p for p in props if p["id"].split("/")[-1] == want), None)
            if hit is None or "pbf" not in hit.get("urls", {}):
                raise SystemExit(f"unknown Geofabrik region id: {want}")
            chosen.append(hit)
        with ThreadPoolExecutor(max_workers=8) as pool:
            sizes = list(pool.map(head_size, [p["urls"]["pbf"] for p in chosen]))
        final = [(p["id"], p["urls"]["pbf"], size) for p, size in zip(chosen, sizes)]
    else:
        final = []
        split_level([p for p in props if "parent" not in p], props, final)
        added = add_uncovered(feats, props, final)
    log(f"{len(final)} regions, {sum(s for _, _, s in final) / 1e9:.1f} GB to download")
    for rid, url, size in final:
        log(f"  region {rid}  {size / 1e6:.0f} MB  {url}")
    return final, added


def split_level(level, props, final):
    while level:
        with ThreadPoolExecutor(max_workers=8) as pool:
            sizes = list(pool.map(head_size, [p["urls"]["pbf"] for p in level]))
        queue, level = level, []
        for p, size in zip(queue, sizes):
            level.extend(split_or_keep(p, size, props, final))


def split_or_keep(p, size, feats, final):
    """Appends p to final, or returns the children to size next."""
    children = [c for c in feats if c.get("parent") == p["id"] and "pbf" in c.get("urls", {})]
    if size > SPLIT_OVER:
        skipped = sorted(c["id"] for c in children if c["id"] in AGGREGATES)
        children = [c for c in children if c["id"] not in AGGREGATES]
        if not children:
            raise SystemExit(f"region {p['id']} is {size / 1e9:.1f} GB with no children: "
                             "list it in AGGREGATES or raise SPLIT_OVER")
        log(f"region {p['id']} is {size / 1e9:.1f} GB: split into {len(children)} children"
            + (f", skipping aggregates {', '.join(skipped)}" if skipped else ""))
        return children
    final.append((p["id"], p["urls"]["pbf"], size))
    return []


# --- Region shapes (D007, F013) ------------------------------------------------

def shape_parts(feature):
    """A region's polygon parts as [(bbox, [ring, ...])]: outer ring first,
    then its holes; each ring a list of (lon, lat)."""
    geo = feature.get("geometry")
    if not geo:
        return None
    polys = [geo["coordinates"]] if geo["type"] == "Polygon" else geo["coordinates"]
    parts = []
    for poly in polys:
        rings = [[(pt[0], pt[1]) for pt in ring] for ring in poly]
        if not rings or not rings[0]:
            continue
        xs, ys = [p[0] for p in rings[0]], [p[1] for p in rings[0]]
        parts.append(((min(xs), min(ys), max(xs), max(ys)), rings))
    return parts


def crossings(rings, y):
    """Sorted x where the horizontal line at y crosses a ring edge."""
    xs = []
    for ring in rings:
        for i in range(len(ring) - 1):
            (x1, y1), (x2, y2) = ring[i], ring[i + 1]
            if (y1 > y) != (y2 > y):
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    return sorted(xs)


def interior_point(part):
    """Midpoint of the widest stretch inside the part on the horizontal line
    through the middle of its bounding box (outside its holes)."""
    (x0, y0, x1, y1), rings = part
    y = (y0 + y1) / 2
    xs = crossings(rings, y)
    stretches = [(b - a, (a + b) / 2) for a, b in zip(xs[0::2], xs[1::2])]
    if not stretches:
        return rings[0][0]
    return (max(stretches)[1], y)


def in_shape(parts, x, y):
    for (x0, y0, x1, y1), rings in parts:
        if x0 <= x <= x1 and y0 <= y <= y1 and sum(1 for cx in crossings(rings, y) if cx > x) % 2 == 1:
            return True
    return False


def add_uncovered(feats, props, final):
    """Adds every region with a part whose interior point lies in no chosen
    region, until a pass adds nothing. Returns [{"region", "point"}]."""
    shapes = {f["properties"]["id"]: shape_parts(f) for f in feats}
    points = {}
    added = []
    changed = True
    while changed:
        changed = False
        chosen = {rid for rid, _, _ in final}
        for rid in sorted(chosen):
            if not shapes.get(rid):
                raise SystemExit(f"region {rid} has no shape in the region index")
        for p in props:
            rid = p["id"]
            if rid in chosen or rid in AGGREGATES or "pbf" not in p.get("urls", {}):
                continue  # an aggregate is skipped on purpose (D005, D008); its polygon takes in sea no part covers
            if not shapes.get(rid):
                log(f"shape check: {rid} has no shape, skipped")
                continue
            if rid not in points:
                points[rid] = [interior_point(part) for part in shapes[rid]]
            miss = next((pt for pt in points[rid]
                         if not any(in_shape(shapes[c], *pt) for c in chosen)), None)
            if miss is None:
                continue
            log(f"shape check: adding {rid}, a part's interior point "
                f"({miss[0]:.5f}, {miss[1]:.5f}) lies in no chosen region")
            size = head_size(p["urls"]["pbf"])
            if size > SPLIT_OVER:
                kids = [c for c in props if c.get("parent") == rid and "pbf" in c.get("urls", {})
                        and c["id"] not in AGGREGATES and c["id"] not in chosen]
                if not kids:
                    raise SystemExit(f"region {rid} is {size / 1e9:.1f} GB and splitting it adds no new region; "
                                     f"its part at ({miss[0]:.5f}, {miss[1]:.5f}) is covered by no chosen region")
                log(f"region {rid} is {size / 1e9:.1f} GB: split into {len(kids)} children")
                split_level(kids, props, final)
            else:
                final.append((rid, p["urls"]["pbf"], size))
            added.append({"region": rid, "point": [round(miss[0], 6), round(miss[1], 6)]})
            chosen = {r for r, _, _ in final}
            changed = True
    return added


# --- OPL reading and course boxes (prototype golfextract.py) -----------------

_esc = re.compile(r"%([0-9a-fA-F]+)%")


def unescape(s):
    return _esc.sub(lambda m: chr(int(m.group(1), 16)), s)


def parse_tags(field):
    tags = {}
    if not field:
        return tags
    for kv in field.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            tags[unescape(k)] = unescape(v)
    return tags


def read_opl(path):
    """Yields (kind, id, tags, payload). Node payload (lon, lat); way payload
    (refs, coords array lon,lat,...); relation payload [(type, ref, role)]."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(" ")
            head = parts[0]
            kind, oid = head[0], int(head[1:])
            fields = {p[0]: p[1:] for p in parts[1:] if p}
            tags = parse_tags(fields.get("T", ""))
            if kind == "n":
                if "x" not in fields or not fields["x"]:
                    continue
                yield kind, oid, tags, (float(fields["x"]), float(fields["y"]))
            elif kind == "w":
                refs = []
                coords = array("d")
                for item in fields.get("N", "").split(","):
                    if not item:
                        continue
                    xi = item.find("x")
                    yi = item.find("y")
                    if xi < 0 or yi < 0:
                        continue  # node location missing
                    refs.append(int(item[1:xi]))
                    coords.append(float(item[xi + 1:yi]))
                    coords.append(float(item[yi + 1:]))
                yield kind, oid, tags, (refs, coords)
            elif kind == "r":
                members = []
                for item in fields.get("M", "").split(","):
                    if not item:
                        continue
                    at = item.find("@")
                    members.append((item[0], int(item[1:at]), unescape(item[at + 1:])))
                yield kind, oid, tags, members


def load(path):
    nodes, ways, rels = {}, {}, {}
    for kind, oid, tags, payload in read_opl(path):
        if kind == "n":
            nodes[oid] = (tags, payload)
        elif kind == "w":
            ways[oid] = (tags, payload)
        else:
            rels[oid] = (tags, payload)
    return nodes, ways, rels


def course_rings(ways, rels):
    """{(kind, id): (tags, [coords arrays of outer rings or segments])}."""
    courses = {}
    for wid, (tags, (refs, coords)) in ways.items():
        if tags.get("leisure") == "golf_course" and len(refs) >= 4 and refs[0] == refs[-1]:
            courses[("way", wid)] = (tags, [coords])
    for rid, (tags, members) in rels.items():
        if tags.get("leisure") != "golf_course" or tags.get("type") != "multipolygon":
            continue
        outers = [ways[ref][1][1] for (t, ref, role) in members
                  if t == "w" and role in ("outer", "") and ref in ways]
        if outers:
            courses[("relation", rid)] = (tags, outers)
    return courses


def bbox(arrays):
    xs = [a[i] for a in arrays for i in range(0, len(a), 2)]
    ys = [a[i] for a in arrays for i in range(1, len(a), 2)]
    return min(xs), min(ys), max(xs), max(ys)


def merge_overlaps(boxes):
    """osmium's point-in-polygon is even-odd over the whole MultiPolygon, so
    an overlap of two boxes counts as outside both. Merge overlapping boxes
    into their enclosing box until none overlap; a superset is safe."""
    def overlaps(a, b):
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]
    changed = True
    while changed:
        changed = False
        grid, out = {}, []
        for box in boxes:
            while True:
                cells = [(gx, gy) for gx in range(int(box[0] // CELL), int(box[2] // CELL) + 1)
                         for gy in range(int(box[1] // CELL), int(box[3] // CELL) + 1)]
                hit = next((i for c in cells for i in grid.get(c, ()) if out[i] is not None and overlaps(out[i], box)), None)
                if hit is None:
                    break
                other = out[hit]
                out[hit] = None
                box = (min(box[0], other[0]), min(box[1], other[1]), max(box[2], other[2]), max(box[3], other[3]))
                changed = True
            out.append(box)
            for c in cells:
                grid.setdefault(c, []).append(len(out) - 1)
        boxes = [b for b in out if b is not None]
    return boxes


def write_boxes(courses, path):
    boxes = []
    for _, (_, arrays) in courses.items():
        x0, y0, x1, y1 = bbox(arrays)
        boxes.append((x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD))
    boxes = merge_overlaps(boxes)
    polys = [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] for x0, y0, x1, y1 in boxes]
    with open(path, "w") as f:
        json.dump({"type": "Feature", "properties": {},
                   "geometry": {"type": "MultiPolygon", "coordinates": polys}}, f)
    return len(boxes)


# --- Assign (prototype golfextract.py) ---------------------------------------

def inside(x, y, arrays):
    """Even-odd ray cast over every segment of every outer ring."""
    hit = False
    for a in arrays:
        n = len(a) // 2
        for i in range(n - 1):
            x1, y1, x2, y2 = a[2 * i], a[2 * i + 1], a[2 * i + 2], a[2 * i + 3]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                hit = not hit
    return hit


def assign(nodes, ways, rels, courses):
    """Yields (course key, [Overpass-shaped elements]) for courses with a golf=hole."""
    boxes = {key: bbox(arrays) for key, (_, arrays) in courses.items()}
    grid = {}
    for key, (x0, y0, x1, y1) in boxes.items():
        for gx in range(int(x0 // CELL), int(x1 // CELL) + 1):
            for gy in range(int(y0 // CELL), int(y1 // CELL) + 1):
                grid.setdefault((gx, gy), []).append(key)

    def containing(points):
        found = set()
        for x, y in points:
            for key in grid.get((int(x // CELL), int(y // CELL)), ()):
                if key in found:
                    continue
                x0, y0, x1, y1 = boxes[key]
                if x0 <= x <= x1 and y0 <= y <= y1 and inside(x, y, courses[key][1]):
                    found.add(key)
        return found

    def way_points(coords):
        return ((coords[i], coords[i + 1]) for i in range(0, len(coords), 2))

    def geometry(coords):
        return [{"lat": coords[i + 1], "lon": coords[i]} for i in range(0, len(coords), 2)]

    wanted_tags = lambda tags: "golf" in tags or tags.get("natural") == "water"
    per_course = {key: [] for key in courses}
    for nid, (tags, (x, y)) in nodes.items():
        if wanted_tags(tags):
            for key in containing([(x, y)]):
                per_course[key].append(("node", nid))
    for wid, (tags, (refs, coords)) in ways.items():
        if wanted_tags(tags):
            for key in containing(way_points(coords)):
                per_course[key].append(("way", wid))
    for rid, (tags, members) in rels.items():
        if wanted_tags(tags):
            pts = []
            for t, ref, _ in members:
                if t == "w" and ref in ways:
                    pts.extend(way_points(ways[ref][1][1]))
                elif t == "n" and ref in nodes:
                    pts.append(nodes[ref][1])
            for key in containing(pts):
                per_course[key].append(("relation", rid))

    def element(kind, oid):
        if kind == "node":
            tags, (x, y) = nodes[oid]
            return {"type": "node", "id": oid, "lat": y, "lon": x, "tags": tags}
        if kind == "way":
            tags, (_, coords) = ways[oid]
            return {"type": "way", "id": oid, "tags": tags, "geometry": geometry(coords)}
        tags, members = rels[oid]
        out_members = []
        for t, ref, role in members:
            if t == "w" and ref in ways:
                out_members.append({"type": "way", "ref": ref, "role": role, "geometry": geometry(ways[ref][1][1])})
            elif t == "n" and ref in nodes:
                x, y = nodes[ref][1]
                out_members.append({"type": "node", "ref": ref, "role": role, "lat": y, "lon": x})
        return {"type": "relation", "id": oid, "tags": tags, "members": out_members}

    order = {"node": 0, "way": 1, "relation": 2}
    store = {"node": nodes, "way": ways, "relation": rels}
    for key, items in per_course.items():
        if not any(store[kind][oid][0].get("golf") == "hole" for kind, oid in items):
            continue
        items = sorted(set(items) | {key}, key=lambda k: (order[k[0]], k[1]))
        yield key, [element(kind, oid) for kind, oid in items]


# --- Trim (prototype trim.py, unchanged) -------------------------------------

def is_hazard(tags):
    golf = tags.get("golf")
    if golf in FEATURES:
        return False
    return golf == "bunker" or golf in WATER or tags.get("natural") == "water"


def wanted(tags):
    return tags.get("golf") in FEATURES or is_hazard(tags) or tags.get("leisure") == "golf_course"


def coords(geometry):
    return [(p["lon"], p["lat"]) for p in geometry or []]


def chain(members):
    """Geo.rings(chaining:) on lists of (lon, lat)."""
    pending = [m for m in members if len(m) >= 2]
    rings = []
    while pending:
        ring = list(pending.pop(0))
        while ring[0] != ring[-1]:
            last = ring[-1]
            idx = next((i for i, m in enumerate(pending) if m[0] == last or m[-1] == last), None)
            if idx is None:
                break
            nxt = pending.pop(idx)
            if nxt[-1] == last:
                nxt = nxt[::-1]
            ring.extend(nxt[1:])
        rings.append(ring)
    return rings


def clip(ring, box):
    """Sutherland-Hodgman against an axis-aligned lon/lat box."""
    x0, y0, x1, y1 = box
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    for edge in range(4):
        if not pts:
            break
        def inside_edge(p):
            return (p[0] >= x0, p[0] <= x1, p[1] >= y0, p[1] <= y1)[edge]

        def cross(a, b):
            if edge < 2:
                x = x0 if edge == 0 else x1
                t = (x - a[0]) / (b[0] - a[0])
                return (x, a[1] + t * (b[1] - a[1]))
            y = y0 if edge == 2 else y1
            t = (y - a[1]) / (b[1] - a[1])
            return (a[0] + t * (b[0] - a[0]), y)
        out = []
        for i, cur in enumerate(pts):
            prev = pts[i - 1]
            if inside_edge(cur):
                if not inside_edge(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside_edge(prev):
                out.append(cross(prev, cur))
        pts = out
    if len(pts) < 3:
        return None
    pts = [(round(x, 7), round(y, 7)) for x, y in pts]
    return pts + [pts[0]]


def ring_geom(ring):
    return [{"lat": y, "lon": x} for x, y in ring]


def element_points(e):
    if e.get("geometry"):
        return coords(e["geometry"])
    if e.get("members"):
        return [p for m in e["members"] for p in coords(m.get("geometry"))]
    if "lat" in e:
        return [(e["lon"], e["lat"])]
    return []


def trim(course_id, elements):
    kind, num = course_id.split(":")[1:]
    anchor = [e for e in elements if (e["type"], e["id"]) == (kind, int(num))]
    lines = [e for e in elements if (e.get("tags") or {}).get("golf") == "hole"]
    pts = [p for e in anchor + lines for p in element_points(e)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mid = (min(ys) + max(ys)) / 2
    dy = PAD_M / 111320.0
    dx = PAD_M / (111320.0 * max(0.01, math.cos(math.radians(mid))))
    box = (min(xs) - dx, min(ys) - dy, max(xs) + dx, max(ys) + dy)

    kept = []
    for e in elements:
        tags = e.get("tags") or {}
        if not wanted(tags):
            continue
        slim = {k: tags[k] for k in KEEP_TAGS if k in tags}
        if not is_hazard(tags) or tags.get("leisure") == "golf_course":
            out = {"type": e["type"], "id": e["id"], "tags": slim}
            for key in ("geometry", "lat", "lon"):
                if key in e:
                    out[key] = e[key]
            if "members" in e:
                out["members"] = [{k: m[k] for k in ("type", "ref", "role", "geometry", "lat", "lon") if k in m} for m in e["members"]]
            kept.append(out)
            continue
        if e["type"] == "node":
            continue  # a point hazard never parses
        if e.get("geometry"):
            rings = [coords(e["geometry"])]
        else:
            rings = chain([coords(m.get("geometry")) for m in e.get("members", []) if m.get("role") == "outer"])
        clipped = [c for c in (clip(r, box) for r in rings) if c]
        if not clipped:
            continue
        if e["type"] == "way" and len(clipped) == 1:
            kept.append({"type": "way", "id": e["id"], "tags": slim, "geometry": ring_geom(clipped[0])})
        else:
            kept.append({"type": "relation", "id": e["id"], "tags": slim,
                         "members": [{"type": "way", "ref": 0, "role": "outer", "geometry": ring_geom(r)} for r in clipped]})
    return kept


# --- Build -------------------------------------------------------------------

def download(url, dest):
    for attempt in (1, 2):
        t = time.time()
        got = 0
        try:
            with open_url(url) as r, open(dest, "wb") as f:
                expected = r.headers.get("Content-Length")
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            if expected is not None and got != int(expected):
                raise OSError(f"got {got} bytes, Content-Length said {expected}")
            STATE["downloaded_bytes"] += got
            STATE["download_seconds"] += time.time() - t
            return got
        except Exception as e:
            STATE["downloaded_bytes"] += got
            STATE["download_seconds"] += time.time() - t
            log(f"download attempt {attempt} failed: {e}")
            if attempt == 2:
                raise
            time.sleep(10)


def note_free_disk(work, rec):
    free = sample_free_disk()
    rec["free_bytes_after"] = free
    log(f"free disk {free / 1e9:.1f} GB")


def safe_remove(*paths):
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def process_region(rid, url, size, work, found_ids, region_recs, local=None, slug=None):
    """local is an --input file: used in place, never removed."""
    slug = slug or rid.replace("/", "-")
    rec = {"region": rid, "bytes_expected": size}
    pbf = local or os.path.join(work, f"{slug}.osm.pbf")
    filtered = os.path.join(work, f"{slug}-filtered.osm.pbf")
    near = os.path.join(work, f"{slug}-near.osm.pbf")

    if not local:
        with step("download", rid):
            rec["bytes_downloaded"] = download(url, pbf)
    with step("fileinfo", rid):
        stamp = run(["osmium", "fileinfo", "-g", "header.option.timestamp", pbf], capture=True)
        if not stamp:
            stamp = run(["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp", pbf], capture=True)
        rec["timestamp"] = stamp
    with step("filter", rid):
        run(["osmium", "tags-filter", pbf, "nwr/leisure=golf_course",
             "nwr/golf=hole,green,tee,fairway,bunker,water_hazard,lateral_water_hazard",
             "nwr/natural=water", "-o", filtered, "--overwrite"])
    with step("course-boxes", rid):
        courses_pbf = os.path.join(work, f"{slug}-courses.osm.pbf")
        courses_opl = os.path.join(work, f"{slug}-courses.opl")
        run(["osmium", "tags-filter", filtered, "nwr/leisure=golf_course", "-o", courses_pbf, "--overwrite"])
        run(["osmium", "add-locations-to-ways", courses_pbf, "-f", "opl,add_metadata=false", "-o", courses_opl, "--overwrite"])
        _, ways, rels = load(courses_opl)
        courses = course_rings(ways, rels)
        found_ids.update(("w" if k == "way" else "r") + str(i) for k, i in courses)
        rec["courses"] = len(courses)
        boxes_path = os.path.join(work, f"{slug}-boxes.geojson")
        rec["boxes_after_merge"] = write_boxes(courses, boxes_path) if courses else 0
        del ways, rels, courses
        safe_remove(courses_pbf, courses_opl)
    if rec["boxes_after_merge"]:
        with step("extract", rid):
            run(["osmium", "extract", "-p", boxes_path, "-s", "smart", filtered, "-o", near, "--overwrite"])
        rec["near_file"] = near
    safe_remove(boxes_path if rec["boxes_after_merge"] else "", "" if local else pbf, filtered)  # the next region needs the disk
    note_free_disk(work, rec)
    region_recs.append(rec)
    return rec


def load_course_list(src):
    if src.startswith(("http://", "https://")):
        with open_url(src) as r:
            data = json.load(r)
    else:
        with open(src, encoding="utf-8") as f:
            data = json.load(f)
    return [row[0] for row in data["courses"] if row and str(row[0])[:1] in ("w", "r")]


def fmt_md(report):
    lines = ["## Hole maps build", "",
             f"Result: {'ok' if report['ok'] else 'FAILED in ' + str(report['failed_step']) + ': ' + str(report['error'])}",
             "",
             f"Files: {report['file_count']}, compressed {report['compressed_bytes'] / 1e6:.1f} MB. "
             f"Downloaded {report['downloaded_bytes'] / 1e9:.2f} GB in {report['download_seconds']:.0f} s. "
             f"Min free disk {(report['min_free_bytes'] or 0) / 1e9:.1f} GB. Total {report['total_seconds']:.0f} s.",
             "", f"Peak RSS units reported by the OS: {RSS_UNIT}; table values are MB.", "",
             "| Region | Step | Seconds | Child peak MB | Script peak MB | Free GB |", "|---|---|---:|---:|---:|---:|"]
    for s in report["steps"]:
        free = s.get("free_bytes")
        lines.append(f"| {s['region'] or '-'} | {s['step']} | {s['seconds']} | "
                     f"{s['step_children_peak_rss_bytes'] // 2**20} | {s['script_peak_rss_bytes'] // 2**20} | "
                     f"{'-' if free is None else f'{free / 1e9:.1f}'} |")
    cov = report.get("coverage")
    if cov:
        lines += ["", f"Course-list coverage: {cov['found']} of {cov['listed']} ({cov['percent']:.2f}%)"]
    return "\n".join(lines) + "\n"


def write_report(args, result):
    report = {"ok": result["ok"], "error": result["error"], "failed_step": STATE["failed_step"],
              "generated": result["generated"], "osm_base": result["osm_base"], "rss_unit_reported_by_os": RSS_UNIT,
              "regions": result["regions"], "steps": STEPS, "file_count": result["count"], "compressed_bytes": result["total"],
              "downloaded_bytes": STATE["downloaded_bytes"], "download_seconds": round(STATE["download_seconds"], 1),
              "min_free_bytes": STATE["min_free_bytes"], "total_seconds": round(time.time() - T0, 1)}
    if result["ok"] and not args.dry_run:
        try:
            listed = load_course_list(args.course_list)
            hit = sum(1 for i in listed if i in result["found_ids"])
            report["coverage"] = {"listed": len(listed), "found": hit, "percent": 100.0 * hit / max(1, len(listed))}
            log(f"coverage {hit} of {len(listed)}")
        except Exception as e:
            report["coverage_error"] = str(e)
            log(f"coverage unavailable: {e}")
    if args.report:
        with open(args.report, "w") as f:
            json.dump(report, f, indent=1)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(fmt_md(report))


def terminate(signum, frame):
    raise SystemExit(f"terminated by signal {signum}")


# --- Publish guards -----------------------------------------------------------

def check_file_count(count, min_files, previous):
    """Guard 1. previous is the last build's file count, or None."""
    fails = []
    if count < min_files:
        fails.append(f"guard 1 (file count): {count} files, at least {min_files} required")
    if previous is None:
        log("guard 1: no previous build, the 5% drop check is skipped")
    elif count < previous * (1 - MAX_DROP):
        fails.append(f"guard 1 (file count): {count} files is {100.0 * (previous - count) / previous:.1f}% "
                     f"fewer than the previous build's {previous}, at most {MAX_DROP * 100:.0f}% allowed")
    return fails


def check_contents(v1_dir):
    """Guard 2: every file inflates and holds an elements array with a golf=hole."""
    bad = []
    for name in sorted(os.listdir(v1_dir)):
        try:
            with gzip.open(os.path.join(v1_dir, name), "rb") as f:
                elements = json.loads(f.read().decode("utf-8"))["elements"]
            if not isinstance(elements, list) or not any((e.get("tags") or {}).get("golf") == "hole" for e in elements):
                raise ValueError("no golf=hole element")
        except Exception as e:
            bad.append(f"{name} ({e})")
    if not bad:
        return []
    return [f"guard 2 (file contents): {len(bad)} files do not inflate or hold a golf=hole, none allowed; "
            f"first: {', '.join(bad[:5])}"]


def check_coverage(listed, found):
    """Guard 3. Returns (failures, coverage record)."""
    hit = sum(1 for i in listed if i in found)
    missing = len(listed) - hit
    cov = {"listed": len(listed), "found": hit, "percent": 100.0 * hit / max(1, len(listed))}
    if not listed:
        return ["guard 3 (course list coverage): the course list has 0 usable ids, at least 1 required"], cov
    if missing > len(listed) * MAX_MISSING:
        return [f"guard 3 (course list coverage): {missing} of {len(listed)} listed ids have no outline "
                f"({100.0 * missing / len(listed):.2f}%), at most {MAX_MISSING * 100:.0f}% allowed"], cov
    return [], cov


def check_sizes(v1_dir):
    """Guard 4: no file over MAX_FILE_BYTES compressed."""
    sizes = [(os.path.getsize(os.path.join(v1_dir, n)), n) for n in os.listdir(v1_dir)]
    big = sorted((b, n) for b, n in sizes if b > MAX_FILE_BYTES)[::-1]
    if not big:
        return []
    return [f"guard 4 (file size): {len(big)} files over {MAX_FILE_BYTES} bytes, none allowed; "
            f"largest {big[0][1]} at {big[0][0]} bytes"]


def read_previous(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return int(json.load(f)["files"])


def run_guards(args, result, v1_dir):
    """Runs all four and reports every failure, not just the first."""
    fails = []
    try:
        previous = read_previous(args.previous_build)
    except Exception as e:
        previous = None
        fails.append(f"guard 1 (file count): previous build file {args.previous_build} is unreadable: {e}")
    fails += check_file_count(result["count"], args.min_files, previous)
    fails += check_contents(v1_dir)
    try:
        fails_3, result["coverage"] = check_coverage(load_course_list(args.course_list), result["found_ids"])
        fails += fails_3
    except Exception as e:
        result["coverage_error"] = str(e)
        fails.append(f"guard 3 (course list coverage): course list unavailable: {e}")
    fails += check_sizes(v1_dir)
    for line in fails:
        log("FAILED " + line)
    if fails:
        raise SystemExit("publish guards failed:\n" + "\n".join(fails))
    log("all four publish guards passed")


def stage_seconds():
    out = {}
    for rec in STEPS:
        out[rec["step"]] = round(out.get(rec["step"], 0.0) + rec["seconds"], 1)
    return out


def write_build_json(path, result, v1_dir):
    sizes = {n: os.path.getsize(os.path.join(v1_dir, n)) for n in os.listdir(v1_dir)}
    big = max(sizes, key=lambda n: sizes[n]) if sizes else None
    cov = result["coverage"]
    info = {"generated": result["generated"], "osm_base": result["osm_base"],
            "files": len(sizes), "bytes": sum(sizes.values()),
            "largest": {"id": big[:-len(".json.gz")].replace("-", ":"), "bytes": sizes[big]} if big else None,
            "course_list_ids": cov["listed"], "course_list_missing": cov["listed"] - cov["found"],
            "regions": [r["region"] for r in result["regions"]], "regions_added": result["regions_added"],
            "seconds": stage_seconds()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
        f.write("\n")


# --- Build -------------------------------------------------------------------

def build(args, result):
    only = [r.strip() for r in args.regions.split(",") if r.strip()]
    if args.input and only:
        raise SystemExit("--input and --regions cannot be used together")
    for path in args.input:
        if not os.path.isfile(path):
            raise SystemExit(f"input file not found: {path}")

    if args.input:
        jobs = [(os.path.basename(p), None, None, p, f"input{i}") for i, p in enumerate(args.input, 1)]
    else:
        with step("region-list"):
            regions, result["regions_added"] = choose_regions(only, args.region_index)
        jobs = [(rid, url, size, None, None) for rid, url, size in regions]
    if args.dry_run:
        return

    found_ids, region_recs = result["found_ids"], result["regions"]
    for rid, url, size, local, slug in jobs:
        process_region(rid, url, size, args.work, found_ids, region_recs, local, slug)

    near_files = [r["near_file"] for r in region_recs if "near_file" in r]
    if not near_files:
        raise SystemExit("no region produced a near-course file")
    merged = os.path.join(args.work, "merged.osm.pbf")
    near_opl = os.path.join(args.work, "near.opl")
    with step("merge"):
        # Confirmed by experiment (report builder-01): merging a file with itself
        # leaves the object counts unchanged, so duplicates collapse.
        run(["osmium", "merge", *near_files, "-o", merged, "--overwrite"])
        safe_remove(*near_files)
    with step("add-locations"):
        run(["osmium", "add-locations-to-ways", merged, "-f", "opl,add_metadata=false", "-o", near_opl, "--overwrite"])
    with step("load"):
        nodes, ways, rels = load(near_opl)
        courses = course_rings(ways, rels)
        log(f"loaded {len(nodes)} nodes, {len(ways)} ways, {len(rels)} relations, {len(courses)} courses")

    stamps = sorted(r["timestamp"] for r in region_recs if r.get("timestamp"))
    result["osm_base"] = stamps[0] if stamps else None
    stage = os.path.join(args.work, "stage")
    shutil.rmtree(stage, ignore_errors=True)
    outdir = os.path.join(stage, "v1")
    os.makedirs(outdir)
    with step("assign-trim-write"):
        for key, elements in assign(nodes, ways, rels, courses):
            course_id = f"osm:{key[0]}:{key[1]}"
            body = json.dumps({"version": 1, "generated": result["generated"], "osm_base": result["osm_base"] or "",
                               "copyright": "© OpenStreetMap contributors", "license": "ODbL 1.0",
                               "elements": trim(course_id, elements)},
                              separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            data = gzip.compress(body, 6, mtime=0)
            with open(os.path.join(outdir, course_id.replace(":", "-") + ".json.gz"), "wb") as f:
                f.write(data)
            result["count"] += 1
            result["total"] += len(data)
    log(f"done: {result['count']} files, {result['total'] / 1e6:.1f} MB compressed")

    with step("guards"):
        run_guards(args, result, outdir)
    write_build_json(os.path.join(stage, "build.json"), result, outdir)
    if os.path.isdir(args.output):
        os.rmdir(args.output)  # main() checked that it is empty
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    shutil.move(stage, args.output)
    log(f"published {result['count']} files to {args.output}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--regions", default="")
    ap.add_argument("--input", action="append", default=[], help="local .osm or .osm.pbf file instead of Geofabrik regions; repeatable")
    ap.add_argument("--region-index", default=INDEX_URL, help="Geofabrik index-v1.json, file or URL")
    ap.add_argument("--course-list", default=COURSE_LIST_URL)
    ap.add_argument("--previous-build", help="the last build.json")
    ap.add_argument("--min-files", type=int, default=MIN_FILES)
    ap.add_argument("--report")
    ap.add_argument("--dry-run", action="store_true", help="print the region list and stop")
    args = ap.parse_args()

    if os.path.exists(args.output) and (not os.path.isdir(args.output) or os.listdir(args.output)):
        raise SystemExit(f"--output {args.output} must be absent or an empty folder")
    os.makedirs(args.work, exist_ok=True)
    STATE["work"] = args.work
    signal.signal(signal.SIGTERM, terminate)  # GitHub sends it on cancel and timeout

    result = {"ok": False, "error": None, "generated": now(), "osm_base": None, "count": 0, "total": 0,
           "found_ids": set(), "regions": [], "regions_added": []}
    try:
        build(args, result)
        result["ok"] = True
    except BaseException as e:
        result["error"] = str(e.code) if isinstance(e, SystemExit) and e.code is not None else repr(e)
        raise
    finally:
        try:
            write_report(args, result)
        except Exception as e:
            log(f"report not written: {e}")


if __name__ == "__main__":
    main()

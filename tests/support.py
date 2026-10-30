"""Helpers for the build tests: tiny OSM files and a way to run build.py."""
import gzip
import json
import os
import subprocess
import sys
from xml.sax.saxutils import quoteattr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build.py")
KEATON = os.path.join(ROOT, "tests", "fixtures", "keaton.osm")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class World:
    """Ways in lon/lat; write() saves a chosen subset as sorted OSM XML.
    Nodes are shared by coordinate, so two files written from one World give
    the same object the same ids, as two Geofabrik extracts do."""

    def __init__(self):
        self.ways = {}
        self.node_ids = {}

    def way(self, wid, tags, coords):
        self.ways[wid] = (tags, [self.node_ids.setdefault(c, len(self.node_ids) + 1) for c in coords])
        return wid

    def write(self, path, way_ids):
        used = {n for w in way_ids for n in self.ways[w][1]}
        coords = {v: k for k, v in self.node_ids.items()}
        out = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
        for n in sorted(used):
            lon, lat = coords[n]
            out.append(f'<node id="{n}" version="1" lat="{lat}" lon="{lon}"/>')
        for w in sorted(way_ids):
            tags, refs = self.ways[w]
            nds = "".join(f'<nd ref="{r}"/>' for r in refs)
            kv = "".join(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items())
            out.append(f'<way id="{w}" version="1">{nds}{kv}</way>')
        out.append("</osm>")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def course(world, cid, box, holes):
    """A golf course outline plus hole lines; holes is [(way id, ref, [(lon, lat), ...])]."""
    world.way(cid, {"leisure": "golf_course", "name": f"Course {cid}"}, rect(*box))
    ids = [cid]
    for wid, ref, line in holes:
        world.way(wid, {"golf": "hole", "ref": str(ref), "par": "4"}, line)
        ids.append(wid)
    return ids


def run_build(tmp, inputs, ids, output="out", extra=(), listed=None):
    """Runs build.py on local inputs. ids are the course-list ids; listed
    replaces the list outright. Returns the CompletedProcess."""
    path = os.path.join(tmp, "courses.json")
    with open(path, "w") as f:
        json.dump({"courses": [[i] for i in (listed if listed is not None else ids)]}, f)
    cmd = [sys.executable, "-I", BUILD, "--work", os.path.join(tmp, "work"),
           "--output", os.path.join(tmp, output), "--course-list", path, "--min-files", "1"]
    for p in inputs:
        cmd += ["--input", p]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


def read_set(directory):
    """{file name: parsed JSON} for every file in <directory>/v1."""
    root = os.path.join(directory, "v1")
    out = {}
    for name in sorted(os.listdir(root)):
        with gzip.open(os.path.join(root, name), "rb") as f:
            out[name] = json.loads(f.read().decode("utf-8"))
    return out


def hole_refs(doc):
    return {e["tags"]["ref"] for e in doc["elements"] if e.get("tags", {}).get("golf") == "hole"}

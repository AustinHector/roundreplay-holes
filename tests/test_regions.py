import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.support import BUILD

SIZES = {}  # region id -> bytes the test server reports for its extract


class Head(BaseHTTPRequestHandler):
    def do_HEAD(self):
        rid = self.path.strip("/").replace(".osm.pbf", "")
        self.send_response(200)
        self.send_header("Content-Length", str(SIZES.get(rid, 1_000_000)))
        self.end_headers()

    def log_message(self, *args):
        pass


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def feature(port, rid, parent, polygons):
    """polygons is a list of polygon parts, each a list of rings; None means no shape."""
    props = {"id": rid, "name": rid, "urls": {"pbf": f"http://127.0.0.1:{port}/{rid}.osm.pbf"}}
    if parent:
        props["parent"] = parent
    geometry = {"type": "MultiPolygon", "coordinates": polygons} if polygons is not None else None
    return {"type": "Feature", "properties": props, "geometry": geometry}


class RegionShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Head)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        SIZES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def dry_run(self, features, extra=()):
        index = os.path.join(self.tmp, "index.json")
        with open(index, "w") as f:
            json.dump({"features": features}, f)
        env = dict(os.environ, no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
        r = subprocess.run([sys.executable, "-I", BUILD, "--work", os.path.join(self.tmp, "w"),
                            "--output", os.path.join(self.tmp, "o"), "--region-index", index,
                            "--dry-run", *extra], capture_output=True, text=True, env=env)
        listed = re.findall(r"\]   region (\S+)  ", r.stdout)
        return r, listed

    def test_a_child_inside_its_parents_shape_is_not_added_and_one_outside_is(self):
        p = self.port
        r, listed = self.dry_run([
            feature(p, "parent", None, [[square(0, 0, 10, 10)]]),
            feature(p, "inside", "parent", [[square(1, 1, 3, 3)]]),
            feature(p, "outside", "parent", [[square(20, 0, 22, 2)]]),
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(listed, ["parent", "outside"])
        self.assertIn("adding outside", r.stdout)
        self.assertNotIn("adding inside", r.stdout)

    def test_a_regions_subset_skips_the_shape_check(self):
        p = self.port
        r, listed = self.dry_run([
            feature(p, "parent", None, [[square(0, 0, 10, 10)]]),
            feature(p, "outside", "parent", [[square(20, 0, 22, 2)]]),
        ], extra=["--regions", "parent"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(listed, ["parent"])

    def test_an_aggregate_whose_area_its_siblings_cover_is_not_added(self):
        p = self.port
        SIZES["europe"] = 7_000_000_000
        r, listed = self.dry_run([
            feature(p, "europe", None, [[square(0, 0, 10, 10)]]),
            feature(p, "west", "europe", [[square(0, 0, 5, 10)]]),
            feature(p, "east", "europe", [[square(5, 0, 10, 10)]]),
            feature(p, "dach", "europe", [[square(0, 0, 10, 10)]]),
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(listed, ["west", "east"])
        self.assertIn("skipping aggregates dach", r.stdout)

    def test_an_uncovered_concave_part_is_added_though_its_vertex_average_is_covered(self):
        p = self.port
        u_shape = [[20, 0], [30, 0], [30, 10], [28, 10], [28, 2], [22, 2], [22, 10], [20, 10], [20, 0]]
        # The U's vertex average is (25, 5.5), in its cavity; a small chosen region covers exactly that.
        r, listed = self.dry_run([
            feature(p, "base", None, [[square(0, 0, 10, 10)]]),
            feature(p, "cavity", None, [[square(24, 4, 26, 7)]]),
            feature(p, "ushape", "base", [[u_shape]]),
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(listed, ["base", "cavity", "ushape"])
        point = re.search(r"adding ushape, a part's interior point \(([-\d.]+), ([-\d.]+)\)", r.stdout)
        self.assertEqual((float(point.group(1)), float(point.group(2))), (29.0, 5.0))

    def test_a_part_with_a_hole_gets_a_point_outside_the_hole(self):
        p = self.port
        ring = square(20, 0, 30, 10)
        hole = square(21, 4, 29, 6)  # the middle line runs through the hole
        r, listed = self.dry_run([
            feature(p, "base", None, [[square(0, 0, 10, 10)]]),
            feature(p, "donut", "base", [[ring, hole]]),
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(listed, ["base", "donut"])
        point = re.search(r"adding donut, a part's interior point \(([-\d.]+), ([-\d.]+)\)", r.stdout)
        x = float(point.group(1))
        self.assertTrue(20 < x < 21 or 29 < x < 30, x)

    def test_a_split_parent_with_an_area_no_child_covers_fails_naming_region_and_point(self):
        p = self.port
        SIZES["big"] = 7_000_000_000
        r, listed = self.dry_run([
            feature(p, "big", None, [[square(0, 0, 10, 10)]]),
            feature(p, "kid", "big", [[square(0, 0, 4, 10)]]),
        ])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("region big", r.stderr)
        self.assertIn("(5.00000, 5.00000)", r.stderr)

    def test_a_chosen_region_with_no_shape_fails_naming_it(self):
        p = self.port
        r, listed = self.dry_run([feature(p, "shapeless", None, None)])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("region shapeless has no shape", r.stderr)

    def test_an_added_region_over_6_gb_is_split_into_children(self):
        p = self.port
        SIZES["isles"] = 7_000_000_000
        r, listed = self.dry_run([
            feature(p, "base", None, [[square(0, 0, 10, 10)]]),
            feature(p, "isles", "base", [[square(20, 0, 30, 10)]]),
            feature(p, "isle-a", "isles", [[square(20, 0, 25, 10)]]),
            feature(p, "isle-b", "isles", [[square(25, 0, 30, 10)]]),
        ])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(listed, ["base", "isle-a", "isle-b"])


if __name__ == "__main__":
    unittest.main()

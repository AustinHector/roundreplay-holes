import json
import os
import tempfile
import unittest

from tests.support import KEATON, World, course, hole_refs, read_set, rect, run_build


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr)

    def test_keaton_gives_one_file_with_its_18_holes(self):
        self.ok(run_build(self.tmp, [KEATON], ["w27130579"]))
        files = read_set(os.path.join(self.tmp, "out"))
        self.assertEqual(list(files), ["osm-way-27130579.json.gz"])
        doc = files["osm-way-27130579.json.gz"]
        holes = [e for e in doc["elements"] if e["type"] == "way" and e["tags"].get("golf") == "hole"]
        self.assertEqual(len(holes), 18)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["license"], "ODbL 1.0")

    def test_two_courses_with_overlapping_padded_boxes_both_keep_their_features(self):
        w = World()
        # Outlines are apart, but each hole lies wholly in the zone both padded boxes cover (F005).
        a = course(w, 1001, (0, 0, 0.004, 0.004), [(2001, 1, [(0.0035, 0.001), (0.0038, 0.003)])])
        b = course(w, 1002, (0.005, 0, 0.009, 0.004), [(2002, 7, [(0.0052, 0.001), (0.0058, 0.003)])])
        path = os.path.join(self.tmp, "overlap.osm")
        w.write(path, a + b)
        self.ok(run_build(self.tmp, [path], ["w1001", "w1002"]))
        files = read_set(os.path.join(self.tmp, "out"))
        self.assertEqual(hole_refs(files["osm-way-1001.json.gz"]), {"1"})
        self.assertEqual(hole_refs(files["osm-way-1002.json.gz"]), {"7"})

    def test_a_lake_larger_than_the_box_is_clipped_and_closed(self):
        w = World()
        ids = course(w, 1001, (0, 0, 0.004, 0.004), [(2001, 1, [(0.001, 0.001), (0.003, 0.003)])])
        lake = w.way(3001, {"natural": "water"},
                     [(0.002, 0.002), (0.03, 0.002), (0.03, 0.03), (0.002, 0.03), (0.002, 0.002)])
        path = os.path.join(self.tmp, "lake.osm")
        w.write(path, ids + [lake])
        self.ok(run_build(self.tmp, [path], ["w1001"]))
        doc = read_set(os.path.join(self.tmp, "out"))["osm-way-1001.json.gz"]
        water = [e for e in doc["elements"] if e["id"] == 3001]
        self.assertEqual(len(water), 1)
        ring = water[0]["geometry"]
        self.assertEqual(ring[0], ring[-1])
        # The box is the outline padded 250 m (0.00225 degrees); the lake reaches 0.03.
        for p in ring:
            self.assertTrue(0.002 - 1e-7 <= p["lon"] <= 0.00625 and 0.002 - 1e-7 <= p["lat"] <= 0.00625, p)
        self.assertGreaterEqual(len(ring), 4)

    def test_a_course_on_a_border_split_across_two_inputs_is_merged(self):
        w = World()
        box = (0, 0, 0.004, 0.004)
        west = course(w, 1001, box, [(2001, 1, [(0.0005, 0.001), (0.001, 0.003)]),
                                    (2002, 2, [(0.0015, 0.001), (0.0018, 0.003)])])
        east = course(w, 1001, box, [(2003, 3, [(0.0025, 0.001), (0.0028, 0.003)]),
                                    (2004, 4, [(0.0035, 0.001), (0.0038, 0.003)])])
        a, b = os.path.join(self.tmp, "west.osm"), os.path.join(self.tmp, "east.osm")
        w.write(a, west)
        w.write(b, east)
        self.ok(run_build(self.tmp, [a, b], ["w1001"]))
        doc = read_set(os.path.join(self.tmp, "out"))["osm-way-1001.json.gz"]
        self.assertEqual(hole_refs(doc), {"1", "2", "3", "4"})
        self.ok(run_build(self.tmp, [a], ["w1001"], output="west-only"))
        self.assertEqual(hole_refs(read_set(os.path.join(self.tmp, "west-only"))["osm-way-1001.json.gz"]), {"1", "2"})

    def test_gzip_header_has_no_flags_name_or_time(self):
        self.ok(run_build(self.tmp, [KEATON], ["w27130579"]))
        with open(os.path.join(self.tmp, "out", "v1", "osm-way-27130579.json.gz"), "rb") as f:
            head = f.read(10)
        self.assertEqual(head[:2], b"\x1f\x8b")
        self.assertEqual(head[3], 0)          # FLG: no FNAME, FCOMMENT, FEXTRA or FHCRC
        self.assertEqual(head[4:8], b"\0\0\0\0")  # MTIME

    def test_inputs_are_left_alone(self):
        before = os.stat(KEATON)
        self.ok(run_build(self.tmp, [KEATON], ["w27130579"]))
        after = os.stat(KEATON)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_build_json_has_every_key(self):
        self.ok(run_build(self.tmp, [KEATON], ["w27130579"]))
        with open(os.path.join(self.tmp, "out", "build.json")) as f:
            info = json.load(f)
        self.assertEqual(set(info), {"generated", "osm_base", "files", "bytes", "largest", "course_list_ids",
                                     "course_list_missing", "regions", "regions_added", "seconds"})
        self.assertEqual(info["files"], 1)
        self.assertEqual(info["largest"]["id"], "osm:way:27130579")
        self.assertEqual(info["largest"]["bytes"], info["bytes"])
        self.assertEqual((info["course_list_ids"], info["course_list_missing"]), (1, 0))
        self.assertEqual(info["regions"], ["keaton.osm"])
        self.assertEqual(info["regions_added"], [])
        self.assertIsNone(info["osm_base"])  # an input with no timestamp
        self.assertIn("assign-trim-write", info["seconds"])


if __name__ == "__main__":
    unittest.main()

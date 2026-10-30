import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.support import BUILD, World, course, read_set, run_build
import build


def strip_generated(files):
    for doc in files.values():
        doc.pop("generated", None)
    return files


class SplitTests(unittest.TestCase):
    def test_largest_first_to_the_lightest_shard_ties_to_the_lowest_index(self):
        # 10 -> s0 (10); 9 -> s1 (9); 8 -> s1 (17); 7 -> s0 (17); 3 -> s0 on the tie (20); 3 -> s1 (20)
        self.assertEqual(build.assign_shards([10, 9, 8, 7, 3, 3], 2), [0, 1, 1, 0, 0, 1])

    def test_equal_sizes_deal_round_robin_from_shard_0(self):
        self.assertEqual(build.assign_shards([5, 5, 5, 5, 5], 3), [0, 1, 2, 0, 1])

    def test_fewer_regions_than_shards_leaves_the_rest_empty(self):
        self.assertEqual(build.assign_shards([4, 9], 5), [1, 0])


class ShardRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        w = World()
        a = course(w, 1001, (0, 0, 0.004, 0.004), [(2001, 1, [(0.001, 0.001), (0.003, 0.003)]),
                                             (2003, 2, [(0.001, 0.003), (0.003, 0.001)])])  # the larger file, so shard 0
        b = course(w, 1002, (1, 1, 1.004, 1.004), [(2002, 7, [(1.001, 1.001), (1.003, 1.003)])])
        self.a, self.b = os.path.join(self.tmp, "a.osm"), os.path.join(self.tmp, "b.osm")
        w.write(self.a, a)
        w.write(self.b, b)
        self.ids = ["w1001", "w1002"]
        self.courses = os.path.join(self.tmp, "courses.json")
        with open(self.courses, "w") as f:
            json.dump({"courses": [[i] for i in self.ids]}, f)
        self.plan = os.path.join(self.tmp, "plan.json")

    def py(self, *argv):
        return subprocess.run([sys.executable, "-I", BUILD, *argv], capture_output=True, text=True)

    def ok(self, r):
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr)
        return r

    def make_plan(self, shards, inputs=None):
        argv = ["--plan-out", self.plan, "--shards", str(shards)]
        for p in inputs or [self.a, self.b]:
            argv += ["--input", p]
        self.ok(self.py(*argv))
        with open(self.plan) as f:
            return json.load(f)

    def make_shard(self, k, name=None):
        out = os.path.join(self.tmp, "shards", name or f"shard-{k}")
        return self.ok(self.py("--plan", self.plan, "--shard", str(k), "--work", os.path.join(self.tmp, f"w{k}"),
                               "--shard-out", out)), out

    def merge(self, output="merged", folder=None):
        return self.py("--plan", self.plan, "--from-shards", folder or os.path.join(self.tmp, "shards"),
                       "--work", os.path.join(self.tmp, "wm"), "--output", os.path.join(self.tmp, output),
                       "--course-list", self.courses, "--min-files", "1")

    def shards_ready(self, n=2):
        self.make_plan(n)
        for k in range(n):
            self.make_shard(k)

    def test_the_plan_lists_every_region_with_its_shard_and_empty_shards_succeed(self):
        plan = self.make_plan(4)
        self.assertEqual(plan["shards"], 4)
        self.assertEqual([(r["id"], r["shard"]) for r in plan["regions"]], [("a.osm", 0), ("b.osm", 1)])
        self.assertTrue(all(r["url"] is None and os.path.isfile(r["local"]) for r in plan["regions"]))
        _, out = self.make_shard(3)
        with open(os.path.join(out, "shard.json")) as f:
            record = json.load(f)
        self.assertEqual((record["shard"], record["regions"], record["found_ids"]), (3, [], []))

    def test_plan_shard_and_merge_match_the_single_job(self):
        self.ok(run_build(self.tmp, [self.a, self.b], self.ids))
        self.shards_ready()
        self.ok(self.merge())
        merged = os.path.join(self.tmp, "merged")
        self.assertEqual(strip_generated(read_set(merged)), strip_generated(read_set(os.path.join(self.tmp, "out"))))
        with open(os.path.join(merged, "build.json")) as f:
            info = json.load(f)
        with open(os.path.join(self.tmp, "out", "build.json")) as f:
            single = json.load(f)
        self.assertEqual(info["regions"], single["regions"])
        self.assertEqual([(s["shard"], s["regions"]) for s in info["shards"]], [(0, 1), (1, 1)])
        self.assertTrue({"bytes", "download_seconds", "seconds"} <= set(info["shards"][0]))
        self.assertEqual(info["files"], 2)

    def test_a_shard_with_no_regions_still_merges(self):
        self.make_plan(3)
        for k in range(3):
            self.make_shard(k)
        self.ok(self.merge())
        self.assertEqual(len(read_set(os.path.join(self.tmp, "merged"))), 2)

    def fails(self, *needles):
        r = self.merge()
        self.assertNotEqual(r.returncode, 0)
        for n in needles:
            self.assertIn(n, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "merged")))

    def edit_record(self, k, change):
        path = os.path.join(self.tmp, "shards", f"shard-{k}", "shard.json")
        with open(path) as f:
            record = json.load(f)
        change(record)
        with open(path, "w") as f:
            json.dump(record, f)

    def test_the_merge_fails_naming_a_shard_whose_record_is_missing(self):
        self.shards_ready()
        os.remove(os.path.join(self.tmp, "shards", "shard-1", "shard.json"))
        self.fails("shard 1 has no record", "region b.osm is missing")

    def test_the_merge_fails_naming_a_region_a_shard_did_not_record(self):
        self.shards_ready()
        self.edit_record(1, lambda rec: rec["regions"].clear())
        self.fails("region b.osm is missing")

    def test_the_merge_fails_naming_a_region_recorded_twice(self):
        self.shards_ready()
        with open(os.path.join(self.tmp, "shards", "shard-0", "shard.json")) as f:
            first = json.load(f)["regions"][0]
        self.edit_record(1, lambda rec: rec["regions"].append(first))
        self.fails("region a.osm is recorded twice")

    def test_the_merge_fails_naming_a_region_whose_near_course_file_is_absent(self):
        self.shards_ready()
        os.remove(os.path.join(self.tmp, "shards", "shard-0", "regions", "input1-near.osm.pbf"))
        self.fails("region a.osm", "near-course file", "absent")

    def test_the_merge_fails_naming_a_region_whose_record_has_no_near_course_file(self):
        self.shards_ready()
        self.edit_record(0, lambda rec: rec["regions"][0].pop("near_file"))
        self.fails("region a.osm", "no near-course file")

    def test_a_region_with_no_courses_needs_no_near_course_file(self):
        w = World()
        empty = w.way(5, {"highway": "road"}, [(0, 0), (0.001, 0.001)])
        path = os.path.join(self.tmp, "empty.osm")
        w.write(path, [empty])
        with open(self.courses, "w") as f:
            json.dump({"courses": [["w1001"]]}, f)
        self.make_plan(2, [self.a, path])
        for k in range(2):
            self.make_shard(k)
        self.ok(self.merge())

    def test_every_problem_is_named_in_one_failure(self):
        self.shards_ready()
        os.remove(os.path.join(self.tmp, "shards", "shard-0", "shard.json"))
        self.edit_record(1, lambda rec: rec["regions"].clear())
        self.fails("shard 0 has no record", "region a.osm is missing", "region b.osm is missing")

    def test_a_folder_without_a_record_is_not_a_shard(self):
        self.shards_ready()
        os.makedirs(os.path.join(self.tmp, "shards", "report-merge"))
        self.ok(self.merge())

    def test_flags_that_do_not_belong_to_a_mode_are_refused(self):
        r = self.py("--plan-out", self.plan, "--input", self.a, "--output", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--output cannot be used with --plan-out", r.stderr)
        r = self.py("--plan", self.plan, "--shard", "0", "--work", "w", "--shard-out", "o", "--course-list", "c")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--course-list cannot be used with --shard", r.stderr)
        r = self.py("--plan", self.plan, "--shard", "0", "--from-shards", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("different modes", r.stderr)
        r = self.py("--plan", self.plan, "--work", "w")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--plan goes with", r.stderr)
        r = self.py("--plan", self.plan, "--from-shards", "x", "--work", "w")
        self.assertEqual(r.returncode, 2)
        self.assertIn("also needs --output", r.stderr)


if __name__ == "__main__":
    unittest.main()

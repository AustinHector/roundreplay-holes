import argparse
import gzip
import json
import os
import tempfile
import unittest

import build
from tests.support import KEATON, run_build


def write_gz(path, doc):
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(doc).encode("utf-8"))


HOLE = {"type": "way", "id": 1, "tags": {"golf": "hole"}, "geometry": []}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_guard_1_file_count(self):
        self.assertEqual(build.check_file_count(15000, 15000, None), [])
        fails = build.check_file_count(14999, 15000, None)
        self.assertEqual(len(fails), 1)
        self.assertIn("guard 1", fails[0])
        self.assertIn("14999", fails[0])
        self.assertIn("15000", fails[0])

    def test_guard_1_drop_against_the_previous_build(self):
        self.assertEqual(build.check_file_count(950, 1, 1000), [])
        fails = build.check_file_count(949, 1, 1000)
        self.assertEqual(len(fails), 1)
        self.assertIn("guard 1", fails[0])
        self.assertIn("1000", fails[0])

    def test_guard_2_contents(self):
        good = os.path.join(self.tmp, "good")
        os.mkdir(good)
        write_gz(os.path.join(good, "osm-way-1.json.gz"), {"elements": [HOLE]})
        self.assertEqual(build.check_contents(good), [])
        for name, body in (("no-elements", {"version": 1}),
                           ("no-hole", {"elements": [{"type": "way", "id": 1, "tags": {"golf": "green"}}]}),
                           ("empty", {"elements": []})):
            bad = os.path.join(self.tmp, name)
            os.mkdir(bad)
            write_gz(os.path.join(bad, "osm-way-1.json.gz"), body)
            write_gz(os.path.join(bad, "osm-way-2.json.gz"), {"elements": [HOLE]})
            fails = build.check_contents(bad)
            self.assertEqual(len(fails), 1, name)
            self.assertIn("guard 2", fails[0])
            self.assertIn("osm-way-1.json.gz", fails[0])
        notgz = os.path.join(self.tmp, "notgz")
        os.mkdir(notgz)
        with open(os.path.join(notgz, "osm-way-1.json.gz"), "wb") as f:
            f.write(b"<html>captive portal</html>")
        self.assertEqual(len(build.check_contents(notgz)), 1)

    def test_guard_3_coverage(self):
        listed = [f"w{i}" for i in range(100)]
        self.assertEqual(build.check_coverage(listed, set(listed[:99]))[0], [])
        fails, cov = build.check_coverage(listed, set(listed[:98]))
        self.assertEqual(len(fails), 1)
        self.assertIn("guard 3", fails[0])
        self.assertIn("2 of 100", fails[0])
        self.assertEqual((cov["listed"], cov["found"]), (100, 98))

    def test_guard_3_fails_on_a_course_list_with_no_ids(self):
        fails, _ = build.check_coverage([], {"w1"})
        self.assertEqual(len(fails), 1)
        self.assertIn("guard 3", fails[0])
        self.assertIn("0 usable ids", fails[0])

    def test_guard_4_size(self):
        d = os.path.join(self.tmp, "sizes")
        os.mkdir(d)
        with open(os.path.join(d, "osm-way-1.json.gz"), "wb") as f:
            f.write(b"x" * 1_000_000)
        self.assertEqual(build.check_sizes(d), [])
        with open(os.path.join(d, "osm-way-2.json.gz"), "wb") as f:
            f.write(b"x" * 1_000_001)
        fails = build.check_sizes(d)
        self.assertEqual(len(fails), 1)
        self.assertIn("guard 4", fails[0])
        self.assertIn("osm-way-2.json.gz", fails[0])
        self.assertIn("1000001", fails[0])


class WiringTests(unittest.TestCase):
    """run_guards must call guards 2 and 4, not just define them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.v1 = os.path.join(self.tmp, "v1")
        os.mkdir(self.v1)
        write_gz(os.path.join(self.v1, "osm-way-1.json.gz"), {"elements": [HOLE]})
        listed = os.path.join(self.tmp, "courses.json")
        with open(listed, "w") as f:
            json.dump({"courses": [["w1"]]}, f)
        self.args = argparse.Namespace(previous_build=None, min_files=1, course_list=listed)
        self.result = {"count": 1, "found_ids": {"w1"}}

    def test_good_set_passes(self):
        build.run_guards(self.args, self.result, self.v1)

    def test_run_guards_fails_a_file_with_no_golf_hole(self):
        write_gz(os.path.join(self.v1, "osm-way-2.json.gz"), {"elements": [{"type": "way", "id": 2, "tags": {"golf": "green"}}]})
        with self.assertRaises(SystemExit) as ctx:
            build.run_guards(self.args, self.result, self.v1)
        self.assertIn("guard 2", str(ctx.exception))

    def test_run_guards_fails_a_file_over_the_size_limit(self):
        with open(os.path.join(self.v1, "osm-way-2.json.gz"), "wb") as f:
            f.write(b"x" * (build.MAX_FILE_BYTES + 1))
        with self.assertRaises(SystemExit) as ctx:
            build.run_guards(self.args, self.result, self.v1)
        self.assertIn("guard 4", str(ctx.exception))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_good_input_passes_all_guards_and_publishes(self):
        r = run_build(self.tmp, [KEATON], ["w27130579"])
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr)
        self.assertIn("all four publish guards passed", r.stdout)
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "out"))), ["build.json", "v1"])

    def test_a_failed_guard_exits_non_zero_names_it_and_leaves_the_output_absent(self):
        r = run_build(self.tmp, [KEATON], ["w27130579"], extra=["--min-files", "999999"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("guard 1", r.stderr)
        self.assertIn("1 files", r.stderr)
        self.assertIn("999999", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))

    def test_a_failed_guard_leaves_an_empty_output_folder_empty(self):
        os.mkdir(os.path.join(self.tmp, "out"))
        r = run_build(self.tmp, [KEATON], ["w27130579"], extra=["--min-files", "999999"])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "out")), [])

    def test_every_failed_guard_is_reported(self):
        listed = ["w27130579"] + [f"w{i}" for i in range(1, 10)]
        r = run_build(self.tmp, [KEATON], [], extra=["--min-files", "999999"], listed=listed)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("guard 1", r.stderr)
        self.assertIn("guard 3", r.stderr)
        self.assertIn("9 of 10", r.stderr)

    def test_the_previous_build_sets_the_5_percent_floor(self):
        prev = os.path.join(self.tmp, "build.json")
        with open(prev, "w") as f:
            json.dump({"files": 1000}, f)
        r = run_build(self.tmp, [KEATON], ["w27130579"], extra=["--previous-build", prev])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fewer than the previous build's 1000", r.stderr)
        with open(prev, "w") as f:
            json.dump({"files": 1}, f)
        r = run_build(self.tmp, [KEATON], ["w27130579"], output="ok", extra=["--previous-build", prev])
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr)

    def test_a_missing_previous_build_skips_the_drop_check(self):
        r = run_build(self.tmp, [KEATON], ["w27130579"],
                      extra=["--previous-build", os.path.join(self.tmp, "nope.json")])
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr)
        self.assertIn("the 5% drop check is skipped", r.stdout)

    def test_a_course_list_with_no_usable_ids_fails_guard_3(self):
        for name, rows in (("empty", []), ("wrong-prefix", [["n1"], ["x2"], ["osm:way:3"]])):
            r = run_build(self.tmp, [KEATON], [], output=name, listed=[i[0] for i in rows])
            self.assertNotEqual(r.returncode, 0, name)
            self.assertIn("0 usable ids", r.stderr, name)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, name)), name)

    def test_a_non_empty_output_is_refused_untouched(self):
        os.mkdir(os.path.join(self.tmp, "out"))
        with open(os.path.join(self.tmp, "out", "keep.txt"), "w") as f:
            f.write("x")
        r = run_build(self.tmp, [KEATON], ["w27130579"])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "out")), ["keep.txt"])


if __name__ == "__main__":
    unittest.main()

import hashlib
import os
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import build

DATA = bytes(range(256)) * 4000  # 1,024,000 bytes
CONFIG = {}
LOG = []  # (path, Range, If-Range) per request


class Server(BaseHTTPRequestHandler):
    """/file serves DATA. CONFIG: cuts = bytes to send before dropping the
    connection, one per request; ignore_range; etag per request number;
    fail = answer 500 always; redirect = /latest answers 307 to /dated/file."""

    def do_GET(self):
        LOG.append((self.path, self.headers.get("Range"), self.headers.get("If-Range")))
        n = len([r for r in LOG if r[0] != "/latest"]) - 1  # the redirect is not a data request
        if CONFIG.get("fail"):
            self.send_error(500)
            return
        if self.path == "/latest":
            self.send_response(307)
            self.send_header("Location", "/dated/file")
            self.end_headers()
            return
        etags = CONFIG.get("etags", ["\"v1\""])
        etag = etags[min(n, len(etags) - 1)]
        start = 0
        rng = self.headers.get("Range")
        if rng and not CONFIG.get("ignore_range") and self.headers.get("If-Range") == etag:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(DATA) - 1}/{len(DATA)}")
        else:
            self.send_response(200)
        body = DATA[start:]
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        cuts = CONFIG.get("cuts", [])
        if n < len(cuts):
            self.wfile.write(body[:cuts[n]])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.saved = (build.RETRY_WAITS, build.OPEN_RETRY_WAIT)
        build.RETRY_WAITS, build.OPEN_RETRY_WAIT = (0,), 0
        os.environ["no_proxy"] = "127.0.0.1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        build.RETRY_WAITS, build.OPEN_RETRY_WAIT = cls.saved

    def setUp(self):
        CONFIG.clear()
        del LOG[:]
        build.STATE["downloaded_bytes"] = 0
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = os.path.join(self._tmp.name, "file.pbf")

    def read(self):
        with open(self.dest, "rb") as f:
            return f.read()

    def test_a_connection_cut_part_way_resumes_and_gives_the_same_bytes(self):
        CONFIG["cuts"] = [300_000, 200_000]
        size = build.download(self.base + "/file", self.dest)
        self.assertEqual(size, len(DATA))
        self.assertEqual(hashlib.sha256(self.read()).hexdigest(), hashlib.sha256(DATA).hexdigest())
        self.assertEqual([r[1] for r in LOG], [None, "bytes=300000-", "bytes=500000-"])
        self.assertEqual([r[2] for r in LOG], [None, '"v1"', '"v1"'])
        # Bytes received: the two cut pieces plus the 524,000 that finished it.
        self.assertEqual(build.STATE["downloaded_bytes"], len(DATA))

    def test_a_server_that_ignores_range_sends_the_whole_file_written_from_byte_0(self):
        CONFIG.update(cuts=[300_000], ignore_range=True)
        build.download(self.base + "/file", self.dest)
        self.assertEqual(self.read(), DATA)
        self.assertEqual(build.STATE["downloaded_bytes"], 300_000 + len(DATA))

    def test_a_file_that_changed_between_attempts_is_fetched_whole(self):
        CONFIG.update(cuts=[300_000], etags=['"v1"', '"v2"'])
        build.download(self.base + "/file", self.dest)
        self.assertEqual(self.read(), DATA)
        self.assertEqual(LOG[1][2], '"v1"')  # asked to resume against v1, got the whole v2

    def test_a_server_that_keeps_failing_ends_in_an_error_after_8_attempts(self):
        CONFIG["fail"] = True
        with self.assertRaises(urllib.error.HTTPError):
            build.download(self.base + "/file", self.dest)
        self.assertEqual(len(LOG), 8)

    def test_a_server_that_keeps_cutting_ends_in_an_error_after_8_attempts(self):
        CONFIG["cuts"] = [1000] * 20
        with self.assertRaises(Exception):
            build.download(self.base + "/file", self.dest)
        self.assertEqual(len(LOG), 8)

    def test_a_resume_goes_to_the_dated_url_a_redirect_led_to(self):
        CONFIG["cuts"] = [400_000]
        build.download(self.base + "/latest", self.dest)
        self.assertEqual(self.read(), DATA)
        self.assertEqual([(r[0], r[1]) for r in LOG],
                         [("/latest", None), ("/dated/file", None), ("/dated/file", "bytes=400000-")])

    def test_a_206_that_starts_elsewhere_is_not_appended(self):
        class Wrong(Server):
            def do_GET(self):
                if self.headers.get("Range"):
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes 5-{len(DATA) - 1}/{len(DATA)}")
                    self.send_header("Content-Length", str(len(DATA) - 5))
                    self.end_headers()
                    try:
                        self.wfile.write(DATA[5:])
                    except OSError:
                        pass  # the client refuses the body
                    return
                super().do_GET()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Wrong)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        CONFIG["cuts"] = [300_000]
        build.download(f"http://127.0.0.1:{srv.server_address[1]}/file", self.dest)
        self.assertEqual(self.read(), DATA)


if __name__ == "__main__":
    unittest.main()

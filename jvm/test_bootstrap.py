#!/usr/bin/env python3
"""Bounded JVM archive acquisition tests; no compiler or dependency installs."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

import bootstrap


class Response(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(payload)
        self.headers = {"Content-Type": "application/gzip", "Content-Length": str(len(payload))}


class FetchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="jvm-fetch-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / "archive.tar.gz"
        self.payload = b"verified archive"
        self.item = {
            "url": "https://example.invalid/archive",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "bytes": len(self.payload),
        }
        self.logs = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.logs))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.sleep = self.enterContext(mock.patch.object(bootstrap.time, "sleep"))
        self.open = self.enterContext(mock.patch.object(bootstrap.urllib.request, "urlopen"))

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.glob(".download-*")), [])

    def test_verified_cache_is_offline_and_checks_size_alias(self):
        self.item["size"] = self.item.pop("bytes")
        self.target.write_bytes(self.payload)
        self.assertEqual(bootstrap.fetch(self.item, self.target, True), self.target)
        self.open.assert_not_called()

    def test_missing_changed_and_wrong_size_cache_are_rejected_offline(self):
        for data, size in [(None, len(self.payload)), (b"changed", len(self.payload)),
                           (self.payload, len(self.payload) + 1)]:
            with self.subTest(data=data, size=size):
                if data is not None:
                    self.target.write_bytes(data)
                self.item["bytes"] = size
                with self.assertRaisesRegex(ValueError, "missing or changed"):
                    bootstrap.fetch(self.item, self.target, True)
        self.open.assert_not_called()

    def test_bad_response_retried_without_replacing_existing_cache(self):
        self.target.write_bytes(b"old cache")

        def download(url, timeout):
            self.assertEqual(self.target.read_bytes(), b"old cache")
            self.assertEqual(timeout, 120)
            return Response(b"bad" if self.open.call_count == 1 else self.payload)

        self.open.side_effect = download
        bootstrap.fetch(self.item, self.target, False)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.open.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assert_no_temporary_files()

    def test_same_pin_mirror_used_after_two_primary_failures(self):
        self.item["mirrors"] = ["https://example.invalid/fossils/archive"]
        self.open.side_effect = [Response(b"bad"), Response(b"bad"), Response(self.payload)]
        bootstrap.fetch(self.item, self.target, False)
        self.assertEqual([call.args[0] for call in self.open.call_args_list],
                         [self.item["url"], self.item["url"], self.item["mirrors"][0]])
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assert_no_temporary_files()

    def test_bad_mirror_cannot_change_pin_or_existing_cache(self):
        self.target.write_bytes(b"old cache")
        self.item["mirrors"] = ["https://example.invalid/fossils/archive"]
        self.open.side_effect = lambda *args, **kwargs: Response(b"x" * len(self.payload))
        with self.assertRaisesRegex(ValueError, "no verified bootstrap input"):
            bootstrap.fetch(self.item, self.target, False)
        self.assertEqual(self.open.call_count, 4)
        self.assertEqual(self.target.read_bytes(), b"old cache")
        self.assert_no_temporary_files()

    def test_oversized_response_is_bounded(self):
        observed = []

        class LargeResponse(Response):
            def close(stream):
                observed.append(stream.tell())
                super().close()

        self.open.side_effect = lambda *args, **kwargs: LargeResponse(b"x" * 100000)
        with self.assertRaisesRegex(ValueError, "no verified bootstrap input"):
            bootstrap.fetch(self.item, self.target, False)
        self.assertEqual(observed, [len(self.payload) + 1] * 2)
        self.assertFalse(self.target.exists())
        self.assert_no_temporary_files()

    def test_truncated_response_reports_metadata_size_and_hash(self):
        self.open.side_effect = lambda *args, **kwargs: Response(b"short")
        with self.assertRaisesRegex(ValueError, "bootstrap input checksum mismatch"):
            bootstrap.fetch(self.item, self.target, False)
        message = self.logs.getvalue()
        for expected in ["status=200", "content-type='application/gzip'", "content-length='5'",
                         "receivedBytes=5", "receivedSha256=" + hashlib.sha256(b"short").hexdigest(),
                         "expectedSha256=" + self.item["sha256"], "attempt=2/2"]:
            self.assertIn(expected, message)
        self.assertFalse(self.target.exists())
        self.assert_no_temporary_files()

    def test_http_failure_retries_and_closes_response(self):
        body = io.BytesIO(b"unavailable")
        error = urllib.error.HTTPError(self.item["url"], 503, "Unavailable", {}, body)
        self.open.side_effect = [error, Response(self.payload)]
        bootstrap.fetch(self.item, self.target, False)
        self.assertTrue(body.closed)
        self.assertIn("status=503", self.logs.getvalue())
        self.assert_no_temporary_files()

    def test_interrupted_download_restarts_without_partial_bytes(self):
        class InterruptedResponse(Response):
            def read(stream, count):
                if stream.tell():
                    raise TimeoutError("interrupted")
                return super().read(3)

        self.open.side_effect = [InterruptedResponse(self.payload), Response(self.payload)]
        bootstrap.fetch(self.item, self.target, False)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertIn("receivedBytes=3", self.logs.getvalue())
        self.assert_no_temporary_files()

    def test_connection_errors_retry_without_publishing_a_partial_file(self):
        for error in [ConnectionResetError("connection reset"), urllib.error.URLError("unreachable")]:
            with self.subTest(error=type(error).__name__):
                self.open.reset_mock()
                self.open.side_effect = [error, Response(self.payload)]
                bootstrap.fetch(self.item, self.target, False)
                self.assertEqual(self.open.call_count, 2)
                self.assertEqual(self.target.read_bytes(), self.payload)
                self.target.unlink()
                self.assert_no_temporary_files()

    def test_invalid_size_is_rejected_before_network(self):
        self.item["bytes"] = 0
        with self.assertRaisesRegex(ValueError, "positive pinned size"):
            bootstrap.fetch(self.item, self.target, False)
        self.open.assert_not_called()


@unittest.skipUnless(os.environ.get("WISP_TEST_JVM_DOWNLOADS") == "1", "hosted network acceptance only")
class OfficialZlibFetchTests(unittest.TestCase):
    def test_locked_acquisition_and_official_fallback(self):
        item = json.loads((bootstrap.ROOT / "toolchain.lock.json").read_text())["zlib"]
        with tempfile.TemporaryDirectory(prefix="jvm-zlib-live-") as directory:
            for label, candidate in [
                ("normal", item),
                ("official-fallback", {**item, "url": item["mirrors"][0], "mirrors": []}),
            ]:
                with self.subTest(source=label):
                    target = Path(directory) / (label + ".tar.gz")
                    bootstrap.fetch(candidate, target, False)
                    self.assertEqual(target.stat().st_size, item["bytes"])
                    self.assertEqual(bootstrap.digest(target), item["sha256"])
                    target.unlink()


if __name__ == "__main__":
    unittest.main()

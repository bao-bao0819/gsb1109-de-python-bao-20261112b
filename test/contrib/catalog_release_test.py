# Copyright (c) 2026 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

import hashlib
import json
import os
import shutil
import tempfile
import unittest

import luigi
from luigi.contrib.catalog_release import (
    AttachmentDownloadTask,
    AttachmentVerificationError,
    CatalogEntry,
    CatalogManifest,
    CatalogPage,
    CatalogPaginationError,
    CatalogReleaseTask,
    CatalogVersionMismatchError,
    blob_target_path,
    claim_target_path,
    discover_manifest,
    hash_file,
    normalize_checksum,
)


def checksum_of(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


class ScriptedCatalogClient:
    """In-memory catalog: page scripts per revision plus attachment payloads."""

    def __init__(self, pages, payloads, fail_pages=None):
        self.pages = list(pages)
        self.payloads = dict(payloads)
        self.fail_pages = set(fail_pages or ())
        self.fetch_calls = []
        self.attachment_calls = []

    def fetch_page(self, page_index):
        self.fetch_calls.append(page_index)
        if page_index in self.fail_pages:
            raise RuntimeError("boom on page %d" % page_index)
        if page_index >= len(self.pages):
            raise RuntimeError("page %d requested beyond scripted catalog" % page_index)
        return self.pages[page_index]

    def fetch_attachment(self, entry, destination_path):
        self.attachment_calls.append(entry.name)
        content = self.payloads[entry.location]
        with open(destination_path, "wb") as handle:
            handle.write(content)


def make_pages(version, chunks):
    pages = []
    for index, chunk in enumerate(chunks):
        entries = tuple(CatalogEntry(name, "loc://%s" % name, checksum_of(content)) for name, content in chunk)
        pages.append(CatalogPage(entries=entries, has_more=index < len(chunks) - 1, version=version))
    return pages


class DownloadTask(AttachmentDownloadTask):
    client = None

    def create_client(self):
        return self.client


class ReleaseTask(CatalogReleaseTask):
    client = None
    attachment_task_class = DownloadTask

    def create_client(self):
        return self.client


def build_release(client, store_root, release_id="r1", catalog_version=None, **kwargs):
    DownloadTask.client = client
    ReleaseTask.client = client
    failures = []

    def capture_failure(task, exception):
        failures.append(exception)

    luigi.Task.event_handler(luigi.Event.FAILURE)(capture_failure)
    try:
        success = luigi.build(
            [ReleaseTask(release_id=release_id, store_root=store_root, catalog_version=catalog_version, **kwargs)],
            local_scheduler=True,
            log_level="WARNING",
        )
    finally:
        luigi.Task.remove_event_handler(luigi.Event.FAILURE, capture_failure)
    return success, failures


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class ChecksumHelpersTest(unittest.TestCase):
    def test_normalize_checksum(self):
        digest = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(normalize_checksum(digest), "sha256:" + digest)
        self.assertEqual(normalize_checksum("MD5:ABC"), "md5:abc")

    def test_hash_file(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        try:
            tmp.write(b"payload")
            tmp.close()
            self.assertEqual(hash_file(tmp.name), checksum_of(b"payload"))
        finally:
            os.unlink(tmp.name)


class DiscoverManifestTest(unittest.TestCase):
    def test_paginates_all_pages_and_pins_version(self):
        pages = make_pages("v1", [[("a", b"1")], [("b", b"2"), ("c", b"3")]])
        client = ScriptedCatalogClient(pages, {})
        manifest = discover_manifest(client, "r1")
        self.assertEqual(manifest.catalog_version, "v1")
        self.assertEqual(manifest.entry_names(), frozenset({"a", "b", "c"}))
        self.assertEqual(manifest.page_count, 2)
        self.assertEqual(client.fetch_calls, [0, 1])

    def test_empty_trailing_page_is_accepted_as_end(self):
        first = make_pages("v1", [[("a", b"1")]])[0]
        pages = [
            CatalogPage(entries=first.entries, has_more=True, version="v1"),
            CatalogPage(entries=(), has_more=False, version="v1"),
        ]
        client = ScriptedCatalogClient(pages, {})
        manifest = discover_manifest(client, "r1")
        self.assertEqual(manifest.entry_names(), frozenset({"a"}))
        self.assertEqual(manifest.page_count, 2)

    def test_empty_catalog_is_valid(self):
        pages = [CatalogPage(entries=(), has_more=False, version="v1")]
        manifest = discover_manifest(ScriptedCatalogClient(pages, {}), "r1")
        self.assertEqual(manifest.entries, ())
        self.assertEqual(manifest.catalog_version, "v1")

    def test_pagination_failure_is_distinct_from_empty_page(self):
        first = make_pages("v1", [[("a", b"1")]])[0]
        pages = [CatalogPage(entries=first.entries, has_more=True, version="v1")]
        client = ScriptedCatalogClient(pages, {}, fail_pages={1})
        with self.assertRaises(CatalogPaginationError):
            discover_manifest(client, "r1")

    def test_expected_version_mismatch_aborts(self):
        pages = make_pages("v2", [[("a", b"1")]])
        with self.assertRaises(CatalogVersionMismatchError):
            discover_manifest(ScriptedCatalogClient(pages, {}), "r1", expected_version="v1")

    def test_version_changing_mid_pagination_aborts(self):
        first = make_pages("v1", [[("a", b"1")]])[0]
        second = make_pages("v2", [[("b", b"2")]])[0]
        pages = [
            CatalogPage(entries=first.entries, has_more=True, version="v1"),
            CatalogPage(entries=second.entries, has_more=False, version="v2"),
        ]
        with self.assertRaises(CatalogVersionMismatchError):
            discover_manifest(ScriptedCatalogClient(pages, {}), "r1")

    def test_non_terminating_pagination_aborts(self):
        pages = [CatalogPage(entries=(), has_more=True, version="v1")]
        with self.assertRaises(CatalogPaginationError):
            discover_manifest(ScriptedCatalogClient(pages, {}), "r1", max_pages=3)

    def test_duplicate_names_rejected(self):
        entry = CatalogEntry("a", "loc://a", checksum_of(b"1"))
        with self.assertRaises(Exception):
            CatalogManifest("r1", "v1", (entry, entry), 1, "now")


class ReleaseFlowTest(unittest.TestCase):
    def setUp(self):
        self.store_root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.store_root, ignore_errors=True)

    def _client(self, version="v1", chunks=(("a", b"alpha"), ("b", b"bravo"))):
        payloads = {"loc://%s" % name: content for name, content in chunks}
        return ScriptedCatalogClient(make_pages(version, [list(chunks)]), payloads)

    def _receipt(self, release_id="r1"):
        return read_json(os.path.join(self.store_root, "releases", release_id, "receipt.json"))

    def _assert_failed_with(self, result, error_type):
        success, failures = result
        self.assertFalse(success)
        self.assertTrue(any(isinstance(exc, error_type) for exc in failures), failures)

    def _assert_succeeded(self, result):
        success, failures = result
        self.assertTrue(success, failures)

    def test_full_release_every_attachment_traces_to_one_version(self):
        client = self._client()
        self._assert_succeeded(build_release(client, self.store_root))
        receipt = self._receipt()
        self.assertEqual(receipt["catalog_version"], "v1")
        self.assertEqual(len(receipt["attachments"]), 2)
        for attachment in receipt["attachments"]:
            self.assertEqual(attachment["catalog_version"], "v1")
            self.assertTrue(os.path.isfile(attachment["blob_path"]))
            self.assertTrue(os.path.isfile(attachment["claim_path"]))
            claim = read_json(attachment["claim_path"])
            self.assertEqual(claim["catalog_version"], "v1")
            self.assertEqual(claim["checksum"], attachment["checksum"])

    def test_rerun_is_complete_and_does_not_redownload(self):
        client = self._client()
        self._assert_succeeded(build_release(client, self.store_root))
        self.assertEqual(sorted(client.attachment_calls), ["a", "b"])
        self._assert_succeeded(build_release(client, self.store_root))
        self.assertEqual(sorted(client.attachment_calls), ["a", "b"])

    def test_resume_after_catalog_revision_is_rejected(self):
        client = self._client(version="v1")
        self._assert_succeeded(build_release(client, self.store_root))
        # Simulate re-entering an unfinished release: pinned manifest remains, receipt does not.
        shutil.rmtree(os.path.join(self.store_root, "releases"))
        revised = self._client(version="v2")
        self._assert_failed_with(build_release(revised, self.store_root), CatalogVersionMismatchError)

    def test_resume_same_version_same_set_succeeds(self):
        client = self._client(version="v1")
        self._assert_succeeded(build_release(client, self.store_root))
        shutil.rmtree(os.path.join(self.store_root, "releases"))
        again = self._client(version="v1")
        self._assert_succeeded(build_release(again, self.store_root))
        self.assertEqual(sorted(again.attachment_calls), [])

    def test_removed_attachment_cannot_enter_release(self):
        v1 = self._client(version="v1", chunks=(("a", b"alpha"), ("b", b"bravo")))
        self._assert_succeeded(build_release(v1, self.store_root))
        shutil.rmtree(os.path.join(self.store_root, "releases"))
        v2 = self._client(version="v2", chunks=(("a", b"alpha"),))
        self._assert_failed_with(build_release(v2, self.store_root), CatalogVersionMismatchError)
        self.assertFalse(os.path.exists(os.path.join(self.store_root, "releases", "r1", "receipt.json")))

    def test_blob_without_claim_evidence_is_not_reused(self):
        client = self._client()
        self._assert_succeeded(build_release(client, self.store_root))
        shutil.rmtree(os.path.join(self.store_root, "claims"))
        client.attachment_calls.clear()
        shutil.rmtree(os.path.join(self.store_root, "releases"))
        self._assert_succeeded(build_release(client, self.store_root))
        self.assertEqual(sorted(client.attachment_calls), ["a", "b"])

    def test_corrupt_blob_is_redownloaded(self):
        client = self._client()
        self._assert_succeeded(build_release(client, self.store_root))
        alpha_checksum = checksum_of(b"alpha")
        blob = blob_target_path(self.store_root, alpha_checksum)
        with open(blob, "wb") as handle:
            handle.write(b"tampered")
        client.attachment_calls.clear()
        shutil.rmtree(os.path.join(self.store_root, "releases"))
        os.remove(claim_target_path(self.store_root, alpha_checksum, "r1"))
        self._assert_succeeded(build_release(client, self.store_root))
        self.assertIn("a", client.attachment_calls)
        with open(blob, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha")

    def test_checksum_mismatch_on_download_fails_release(self):
        client = self._client()
        original = client.fetch_attachment

        def tampered(entry, destination_path):
            original(entry, destination_path)
            if entry.name == "a":
                with open(destination_path, "wb") as handle:
                    handle.write(b"wrong bytes")

        client.fetch_attachment = tampered
        self._assert_failed_with(build_release(client, self.store_root), AttachmentVerificationError)

    def test_reuse_from_older_release_with_evidence(self):
        old_client = self._client()
        self._assert_succeeded(build_release(old_client, self.store_root, release_id="old"))
        new_client = self._client(version="v2")
        self._assert_succeeded(build_release(new_client, self.store_root, release_id="new"))
        self.assertEqual(new_client.attachment_calls, [])
        receipt = self._receipt(release_id="new")
        self.assertEqual({item["catalog_version"] for item in receipt["attachments"]}, {"v2"})
        claims = set()
        for attachment in receipt["attachments"]:
            claims.update(os.listdir(os.path.dirname(attachment["claim_path"])))
        self.assertIn("old.json", claims)
        self.assertIn("new.json", claims)

    def test_malformed_claim_marker_is_not_evidence(self):
        client = self._client()
        self._assert_succeeded(build_release(client, self.store_root))
        marker = claim_target_path(self.store_root, checksum_of(b"alpha"), "r1")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("{not valid json")
        client.attachment_calls.clear()
        shutil.rmtree(os.path.join(self.store_root, "releases"))
        self._assert_succeeded(build_release(client, self.store_root))
        self.assertIn("a", client.attachment_calls)
        self.assertNotIn("b", client.attachment_calls)
        claim = read_json(marker)
        self.assertEqual(claim["catalog_version"], "v1")

    def test_same_version_label_with_changed_checksum_is_rejected(self):
        v1 = self._client(version="v1", chunks=(("a", b"alpha"),))
        self._assert_succeeded(build_release(v1, self.store_root))
        shutil.rmtree(os.path.join(self.store_root, "releases"))
        drifted = self._client(version="v1", chunks=(("a", b"different-content"),))
        self._assert_failed_with(build_release(drifted, self.store_root), CatalogVersionMismatchError)

    def test_pagination_failure_aborts_release(self):
        pages = make_pages("v1", [(("a", b"alpha"),)])
        pages[0] = CatalogPage(entries=pages[0].entries, has_more=True, version="v1")
        client = ScriptedCatalogClient(pages, {"loc://a": b"alpha"}, fail_pages={1})
        self._assert_failed_with(build_release(client, self.store_root), CatalogPaginationError)

    def test_empty_catalog_produces_empty_receipt(self):
        pages = [CatalogPage(entries=(), has_more=False, version="v1")]
        client = ScriptedCatalogClient(pages, {})
        self._assert_succeeded(build_release(client, self.store_root))
        receipt = self._receipt()
        self.assertEqual(receipt["attachment_count"], 0)
        self.assertEqual(receipt["attachments"], [])

    def test_manifest_and_receipt_persist_pinned_version(self):
        client = self._client()
        self._assert_succeeded(build_release(client, self.store_root))
        manifest = read_json(os.path.join(self.store_root, "manifests", "r1.json"))
        self.assertEqual(manifest["catalog_version"], "v1")
        self.assertEqual(manifest["entries"][0]["checksum"], checksum_of(b"alpha"))
        receipt = self._receipt()
        self.assertEqual(receipt["manifest_path"], os.path.join(self.store_root, "manifests", "r1.json"))


if __name__ == "__main__":
    unittest.main()

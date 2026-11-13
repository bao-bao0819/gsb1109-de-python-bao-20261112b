# -*- coding: utf-8 -*-
#
# Copyright 2026 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Version-pinned catalog releases with dynamic attachment downloads.

A catalog (e.g. a paginated web directory) only reveals its attachments page
by page. A :class:`CatalogReleaseTask` paginates the full catalog first, pins
the discovered *catalog version* for the whole release and only then yields
:class:`AttachmentDownloadTask` instances dynamically. This guarantees that a
single release never mixes attachments coming from different revisions of the
catalog:

* every attachment accepted by a release is listed in one manifest carrying a
  single ``catalog_version`` (see :class:`CatalogManifest`);
* resuming the task after the worker yielded re-paginates the catalog and
  refuses to proceed when the catalog has changed in the meantime
  (:class:`CatalogVersionMismatchError`);
* blobs already downloaded for an older release are reused only when their
  content checksum can be verified *and* a claim marker from that earlier
  release exists as evidence. A bare file on disk without evidence can never
  masquerade as an attachment of the current version;
* an empty trailing page is a normal end of pagination, whereas a failing
  page request raises :class:`CatalogPaginationError`;
* attachments removed from the catalog are never part of the manifest and
  therefore can never end up in the release receipt.

Example::

    class MyCatalogClient(CatalogClient):
        def fetch_page(self, page_index):
            ...  # -> CatalogPage

        def fetch_attachment(self, entry, destination_path):
            ...  # streams the attachment bytes to destination_path

    class MyDownloadTask(AttachmentDownloadTask):
        def create_client(self):
            return MyCatalogClient()

    class MyRelease(CatalogReleaseTask):
        attachment_task_class = MyDownloadTask

        def create_client(self):
            return MyCatalogClient()

    luigi.build([MyRelease(release_id="2026-11-13", store_root="/data/store")],
                local_scheduler=True)
"""

from __future__ import annotations

import abc
import dataclasses
import datetime
import hashlib
import json
import logging
import os
from typing import Iterable, Optional

import luigi
from luigi.local_target import LocalTarget

logger = logging.getLogger("luigi-interface")

DEFAULT_CHECKSUM_ALGORITHM = "sha256"


class CatalogError(Exception):
    """Base class for all errors raised by the catalog release machinery."""


class CatalogPaginationError(CatalogError):
    """Raised when a catalog page cannot be fetched or pagination never ends.

    This is deliberately distinct from an empty trailing page, which is the
    normal signal that the catalog has been fully listed.
    """


class CatalogVersionMismatchError(CatalogError):
    """Raised when observed catalog data does not belong to the pinned version."""


class AttachmentVerificationError(CatalogError):
    """Raised when downloaded or reused bytes fail their checksum verification."""


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe_name(name: str) -> str:
    """Turn an arbitrary identifier into a single, filesystem-safe path segment."""
    sanitized = "".join(ch if (ch.isalnum() or ch in ("-", "_", ".")) else "_" for ch in name)
    return sanitized.strip("._") or "unnamed"


def blob_target_path(store_root: str, checksum: str) -> str:
    """Absolute path of the content-addressed blob for ``checksum``."""
    return os.path.join(store_root, "blobs", _safe_name(normalize_checksum(checksum)))


def claim_target_path(store_root: str, checksum: str, release_id: str) -> str:
    """Absolute path of the claim marker recording provenance for one release."""
    return os.path.join(store_root, "claims", _safe_name(normalize_checksum(checksum)), "%s.json" % _safe_name(release_id))


def normalize_checksum(checksum: str, algorithm: str = DEFAULT_CHECKSUM_ALGORITHM) -> str:
    """Return a canonical ``algorithm:hexdigest`` checksum identifier."""
    checksum = checksum.strip().lower()
    if ":" in checksum:
        prefix, _, digest = checksum.partition(":")
        return "%s:%s" % (prefix.strip(), digest.strip())
    return "%s:%s" % (algorithm, checksum)


def hash_file(path: str, algorithm: str = DEFAULT_CHECKSUM_ALGORITHM) -> str:
    """Compute the canonical checksum identifier of a local file."""
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "%s:%s" % (algorithm, digest.hexdigest())


@dataclasses.dataclass(frozen=True)
class CatalogEntry:
    """One attachment listed by the catalog.

    ``checksum`` is expected in canonical ``algorithm:hexdigest`` form (a bare
    hexdigest is assumed to use :data:`DEFAULT_CHECKSUM_ALGORITHM`).
    """

    name: str
    location: str
    checksum: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CatalogEntry requires a non-empty name")
        if not self.location:
            raise ValueError("CatalogEntry requires a non-empty location")
        if not self.checksum:
            raise ValueError("CatalogEntry requires a checksum")
        object.__setattr__(self, "checksum", normalize_checksum(self.checksum))

    @property
    def checksum_algorithm(self) -> str:
        return self.checksum.partition(":")[0]

    @property
    def checksum_digest(self) -> str:
        return self.checksum.partition(":")[2]

    def to_dict(self) -> dict:
        return {"name": self.name, "location": self.location, "checksum": self.checksum}

    @classmethod
    def from_dict(cls, data: dict) -> "CatalogEntry":
        return cls(name=data["name"], location=data["location"], checksum=data["checksum"])


@dataclasses.dataclass(frozen=True)
class CatalogPage:
    """Result of fetching one catalog page.

    ``has_more`` indicates whether another page exists. An empty ``entries``
    tuple on the final page is a normal, non-erroneous result.
    """

    entries: tuple
    has_more: bool
    version: str


class CatalogClient(abc.ABC):
    """Abstract transport for reading a paginated catalog and its attachments."""

    @abc.abstractmethod
    def fetch_page(self, page_index: int) -> CatalogPage:
        """Return the page at ``page_index`` (zero based).

        Implementations should raise an exception when the page cannot be
        retrieved; the discovery machinery converts that into a
        :class:`CatalogPaginationError`.
        """

    @abc.abstractmethod
    def fetch_attachment(self, entry: CatalogEntry, destination_path: str) -> None:
        """Stream the attachment described by ``entry`` to ``destination_path``."""


@dataclasses.dataclass(frozen=True)
class CatalogManifest:
    """The complete, immutable listing of a catalog at one ``catalog_version``."""

    release_id: str
    catalog_version: str
    entries: tuple
    page_count: int
    discovered_at: str

    def __post_init__(self) -> None:
        names = [entry.name for entry in self.entries]
        if len(names) != len(set(names)):
            raise CatalogError("catalog version %s lists duplicate attachment names" % self.catalog_version)
        object.__setattr__(self, "entries", tuple(self.entries))

    def entry_names(self) -> frozenset:
        return frozenset(entry.name for entry in self.entries)

    def as_entry_set(self) -> frozenset:
        """Full identity of the listing (name, location and checksum per entry)."""
        return frozenset(self.entries)

    def find(self, name: str) -> CatalogEntry:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {
            "release_id": self.release_id,
            "catalog_version": self.catalog_version,
            "page_count": self.page_count,
            "discovered_at": self.discovered_at,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CatalogManifest":
        return cls(
            release_id=data["release_id"],
            catalog_version=data["catalog_version"],
            entries=tuple(CatalogEntry.from_dict(item) for item in data["entries"]),
            page_count=data["page_count"],
            discovered_at=data["discovered_at"],
        )


def discover_manifest(
    client: CatalogClient,
    release_id: str,
    expected_version: Optional[str] = None,
    max_pages: int = 1000,
) -> CatalogManifest:
    """Paginate the whole catalog and build a version-pinned manifest.

    An empty trailing page (``entries == ()`` together with ``has_more`` being
    false) is the ordinary termination condition. A page request that raises is
    converted into :class:`CatalogPaginationError`, so callers can always tell
    "the catalog genuinely ends here" apart from "listing failed".
    """
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")

    entries: list[CatalogEntry] = []
    seen_versions: set[str] = set()
    page_index = 0
    while True:
        try:
            page = client.fetch_page(page_index)
        except CatalogError:
            raise
        except Exception as exc:
            raise CatalogPaginationError("failed to fetch catalog page %d for release %s: %s" % (page_index, release_id, exc)) from exc

        if not page.version:
            raise CatalogPaginationError("catalog page %d did not report a version" % page_index)
        if expected_version is not None and page.version != expected_version:
            raise CatalogVersionMismatchError(
                "release %s is pinned to catalog version %s but page %d reports %s" % (release_id, expected_version, page_index, page.version)
            )
        seen_versions.add(page.version)
        if len(seen_versions) > 1:
            raise CatalogVersionMismatchError("catalog changed while paginating release %s: observed versions %s" % (release_id, sorted(seen_versions)))

        entries.extend(page.entries)
        page_index += 1
        if not page.has_more:
            break
        if page_index >= max_pages:
            raise CatalogPaginationError("catalog pagination for release %s did not terminate after %d pages" % (release_id, max_pages))

    manifest = CatalogManifest(
        release_id=release_id,
        catalog_version=next(iter(seen_versions)),
        entries=tuple(entries),
        page_count=page_index,
        discovered_at=_utc_now_iso(),
    )
    logger.info(
        "Discovered catalog version %s for release %s with %d attachments across %d pages",
        manifest.catalog_version,
        release_id,
        len(manifest.entries),
        manifest.page_count,
    )
    return manifest


class _JSONTarget(LocalTarget):
    """LocalTarget helper that atomically writes JSON-serializable payloads.

    Unlike :meth:`LocalTarget.temporary_path`, writes are idempotent: an
    existing target is replaced, so re-entering a partially finished release
    does not fail because its markers are already present.
    """

    def write_json(self, payload: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = "%s.luigi-tmp-%s" % (self.path, os.getpid())
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def read_json(self) -> dict:
        with self.open("r") as handle:
            return json.load(handle)


class AttachmentDownloadTask(luigi.Task):
    """Downloads one attachment and records a versioned claim marker.

    The on-disk layout under ``store_root`` separates immutable, content
    addressed blobs from per-release evidence:

    * ``blobs/<checksum>`` holds the bytes and is shared across releases;
    * ``claims/<checksum>/<release_id>.json`` is the evidence that a specific
      release accepted those bytes for a specific catalog version.

    A blob without any claim marker is treated as having no provenance and is
    never reused: the attachment is downloaded again.
    """

    release_id = luigi.Parameter()
    catalog_version = luigi.Parameter()
    attachment_name = luigi.Parameter()
    expected_checksum = luigi.Parameter()
    location = luigi.Parameter()
    manifest_path = luigi.Parameter()
    store_root = luigi.Parameter()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._expected = normalize_checksum(self.expected_checksum)

    # ------------------------------------------------------------------ hooks
    def create_client(self) -> CatalogClient:
        """Return the client used to download this attachment.

        Subclasses implement this; the default raises because the base task has
        no transport of its own.
        """
        raise NotImplementedError("Subclasses of AttachmentDownloadTask must implement create_client()")

    # ------------------------------------------------------------- locations
    @property
    def blob_path(self) -> str:
        return blob_target_path(self.store_root, self._expected)

    @property
    def claim_path(self) -> str:
        return claim_target_path(self.store_root, self._expected, self.release_id)

    def _existing_claim_paths(self) -> list:
        claim_dir = os.path.dirname(self.claim_path)
        if not os.path.isdir(claim_dir):
            return []
        return [os.path.join(claim_dir, member) for member in sorted(os.listdir(claim_dir)) if member.endswith(".json")]

    def _has_valid_evidence(self) -> bool:
        """A blob is reusable only with a parseable marker proving provenance.

        The marker must be valid JSON, address the exact expected checksum and
        record the catalog version that accepted the bytes. A bare file, an
        empty marker or an unreadable marker is not evidence.
        """
        for path in self._existing_claim_paths():
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    claim = json.load(handle)
            except (OSError, ValueError):
                continue
            if claim.get("checksum") != self._expected:
                continue
            if not claim.get("catalog_version"):
                continue
            return True
        return False

    def output(self):
        return LocalTarget(self.claim_path)

    def complete(self):
        if not super().complete():
            return False
        try:
            claim = _JSONTarget(self.claim_path).read_json()
        except (OSError, ValueError):
            return False
        if claim.get("catalog_version") != self.catalog_version:
            return False
        if claim.get("checksum") != self._expected:
            return False
        if not os.path.isfile(self.blob_path):
            return False
        try:
            return hash_file(self.blob_path, self._expected.partition(":")[0]) == self._expected
        except OSError:
            return False

    # ------------------------------------------------------------------ run
    def run(self):
        os.makedirs(os.path.dirname(self.claim_path), exist_ok=True)
        if self._can_reuse_blob():
            logger.info("Reusing verified blob %s for attachment %s", self.blob_path, self.attachment_name)
        else:
            self._download_blob()

        claim = {
            "release_id": self.release_id,
            "catalog_version": self.catalog_version,
            "attachment_name": self.attachment_name,
            "location": self.location,
            "checksum": self._expected,
            "blob_path": self.blob_path,
            "manifest_path": self.manifest_path,
            "recorded_at": _utc_now_iso(),
        }
        _JSONTarget(self.claim_path).write_json(claim)

    def _can_reuse_blob(self) -> bool:
        """Reuse only checksum-verified bytes that carry evidence from a release."""
        if not self._has_valid_evidence():
            return False
        if not os.path.isfile(self.blob_path):
            return False
        try:
            actual = hash_file(self.blob_path, self._expected.partition(":")[0])
        except OSError:
            return False
        if actual != self._expected:
            logger.warning("Discarding unverified blob %s for attachment %s", self.blob_path, self.attachment_name)
            return False
        return True

    def _download_blob(self):
        client = self.create_client()
        os.makedirs(os.path.dirname(self.blob_path), exist_ok=True)
        tmp_path = "%s.tmp-%s" % (self.blob_path, os.getpid())
        try:
            client.fetch_attachment(CatalogEntry(self.attachment_name, self.location, self._expected), tmp_path)
            actual = hash_file(tmp_path, self._expected.partition(":")[0])
            if actual != self._expected:
                raise AttachmentVerificationError("checksum mismatch for attachment %s: expected %s, got %s" % (self.attachment_name, self._expected, actual))
            os.replace(tmp_path, self.blob_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


class CatalogReleaseTask(luigi.Task):
    """Pins one catalog version for a release and downloads exactly its attachments.

    Subclasses implement :meth:`create_client` and set
    :attr:`attachment_task_class` to a concrete :class:`AttachmentDownloadTask`
    subclass. The task writes two artifacts under ``store_root``:

    * ``manifests/<release_id>.json`` -- the pinned listing;
    * ``releases/<release_id>/receipt.json`` -- the final receipt, listing one
      catalog version for every accepted attachment.
    """

    release_id = luigi.Parameter()
    store_root = luigi.Parameter()
    catalog_version = luigi.OptionalParameter(default=None)
    max_pages = luigi.IntParameter(default=1000)

    attachment_task_class = AttachmentDownloadTask

    # ------------------------------------------------------------------ hooks
    def create_client(self) -> CatalogClient:
        raise NotImplementedError("Subclasses of CatalogReleaseTask must implement create_client()")

    # ------------------------------------------------------------- locations
    @property
    def manifest_target(self) -> _JSONTarget:
        return _JSONTarget(os.path.join(self.store_root, "manifests", "%s.json" % _safe_name(self.release_id)))

    @property
    def receipt_target(self) -> _JSONTarget:
        return _JSONTarget(os.path.join(self.store_root, "releases", _safe_name(self.release_id), "receipt.json"))

    def output(self):
        return self.receipt_target

    # ------------------------------------------------------------------ run
    def run(self):
        client = self.create_client()
        manifest = discover_manifest(
            client,
            release_id=self.release_id,
            expected_version=self.catalog_version,
            max_pages=self.max_pages,
        )
        self._pin_manifest(manifest)

        download_tasks = [self._build_download_task(manifest, entry) for entry in manifest.entries]
        if download_tasks:
            yield download_tasks

        self._write_receipt(manifest, download_tasks)

    def _pin_manifest(self, manifest: CatalogManifest) -> None:
        """Persist the manifest, refusing to re-pin a different catalog revision.

        An existing, identical manifest is left untouched; any difference in
        version or in the attachment set means the catalog was revised and
        aborts the release.
        """
        target = self.manifest_target
        if target.exists():
            try:
                previous = CatalogManifest.from_dict(target.read_json())
            except (OSError, ValueError) as exc:
                raise CatalogError("stored manifest for release %s is unreadable: %s" % (self.release_id, exc))
            if previous.catalog_version != manifest.catalog_version:
                raise CatalogVersionMismatchError(
                    "release %s already pinned catalog version %s but the catalog now reports %s"
                    % (self.release_id, previous.catalog_version, manifest.catalog_version)
                )
            if previous.as_entry_set() != manifest.as_entry_set():
                raise CatalogVersionMismatchError("catalog version %s changed its attachment set for release %s" % (manifest.catalog_version, self.release_id))
            return
        target.write_json(manifest.to_dict())

    def _build_download_task(self, manifest: CatalogManifest, entry: CatalogEntry) -> AttachmentDownloadTask:
        return self.attachment_task_class(
            release_id=self.release_id,
            catalog_version=manifest.catalog_version,
            attachment_name=entry.name,
            expected_checksum=entry.checksum,
            location=entry.location,
            manifest_path=self.manifest_target.path,
            store_root=self.store_root,
        )

    def _write_receipt(self, manifest: CatalogManifest, download_tasks: Iterable[AttachmentDownloadTask]) -> None:
        attachments = []
        for task in download_tasks:
            entry = manifest.find(task.attachment_name)
            attachments.append(
                {
                    "name": entry.name,
                    "location": entry.location,
                    "checksum": entry.checksum,
                    "catalog_version": manifest.catalog_version,
                    "blob_path": task.blob_path,
                    "claim_path": task.claim_path,
                    "manifest_path": self.manifest_target.path,
                }
            )
        receipt = {
            "release_id": self.release_id,
            "catalog_version": manifest.catalog_version,
            "manifest_path": self.manifest_target.path,
            "generated_at": _utc_now_iso(),
            "attachment_count": len(attachments),
            "attachments": attachments,
        }
        versions = {item["catalog_version"] for item in attachments}
        if versions and versions != {manifest.catalog_version}:
            raise CatalogVersionMismatchError("receipt for release %s mixes catalog versions %s" % (self.release_id, sorted(versions)))
        self.receipt_target.write_json(receipt)

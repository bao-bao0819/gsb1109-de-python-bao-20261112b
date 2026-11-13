# -*- coding: utf-8 -*-
"""
Completeness checks for inventory collections made of several external files.

A collection is delivered as a group of external files plus a seal document
(封账单) describing exactly which version of every file belongs to the
collection.  The seal may be corrected after the files have arrived, so the
files and the seal must always be evaluated as one generation.  A checker that
merely tests whether every file exists can observe a mixture of an old and a
new generation and incorrectly report success.

The seal is a UTF-8 JSON document::

    {
      "seal_version": "inventory-2026-11-13T22:00:00Z",
      "artifacts": {
        "stock": {
          "version": "stock-20261113-a",
          "size": 128,
          "sha256": "..."
        },
        "locations": {
          "version": "locations-20261113-a",
          "size": 0,
          "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        },
        "lots": {
          "version": "lots-20261113-a",
          "size": 256,
          "sha256": "..."
        }
      }
    }

``seal_version`` identifies the generation of the seal.  Every artifact entry
declares the exact byte size and SHA-256 digest sealed for that generation.
An empty artifact is valid zero stock only when that generation's seal declares
it with ``size`` 0 and the SHA-256 digest of the empty byte string.  A seal that
does not cover an artifact cannot confirm it, including when the artifact is
empty.

:py:func:`inspect_inventory` returns one of three states:

``NOT_READY``
    还没齐.  The seal or at least one artifact is missing or unreadable, or the
    collection kept changing while it was being checked.  Nothing is reported
    as complete.

``READY``
    可以用.  All artifacts match the exact generation described by one stable
    seal, and every artifact was read under a stable seal snapshot.

``MISMATCH``
    封账单和交付件对不上.  Every artifact is present, but at least one size,
    digest, artifact name, or seal field disagrees with the seal.

The check re-reads the seal and every artifact before accepting a conclusion.
If a replacement happens during verification, the whole check is retried; if
the collection remains unstable, the result is ``NOT_READY`` rather than a
false success.

Luigi usage::

    import luigi
    from luigi.contrib.inventory import SealedInventoryTask

    class DailyInventory(SealedInventoryTask):
        date = luigi.DateParameter()

        def seal(self):
            return luigi.LocalTarget(f"/data/inventory/{self.date}/seal.json")

        def artifacts(self):
            base = f"/data/inventory/{self.date}"
            return {
                "stock": luigi.LocalTarget(f"{base}/stock.csv"),
                "locations": luigi.LocalTarget(f"{base}/locations.csv"),
                "lots": luigi.LocalTarget(f"{base}/lots.csv"),
            }

Only the ``READY`` state makes :py:meth:`SealedInventoryTask.complete` return
``True``.  ``NOT_READY`` leaves the task pending.  By default ``MISMATCH``
raises :class:`InventorySealMismatch`, so Luigi reports the task as broken
rather than pending; set ``mismatch_is_fatal = False`` to keep a contradictory
delivery pending while its seal is being corrected.  In a long-running worker,
enable ``retry-external-tasks`` so an incomplete external task is re-checked
when the seal arrives or changes.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

import luigi
from luigi.target import Target

logger = logging.getLogger("luigi-interface")

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class SealFormatError(ValueError):
    """Raised when a seal document is not a valid inventory seal."""


class InventoryState(enum.Enum):
    """The three observable states of an inventory collection."""

    NOT_READY = "not_ready"
    READY = "ready"
    MISMATCH = "mismatch"

    @property
    def label(self) -> str:
        """Return the human-readable label of this state."""
        return {
            InventoryState.NOT_READY: "not ready",
            InventoryState.READY: "ready",
            InventoryState.MISMATCH: "seal mismatch",
        }[self]


class ProblemCode(str, enum.Enum):
    """Machine-readable reason codes attached to an inventory status."""

    SEAL_MISSING = "seal_missing"
    SEAL_UNREADABLE = "seal_unreadable"
    SEAL_EMPTY = "seal_empty"
    SEAL_INVALID = "seal_invalid"
    SEAL_ARTIFACT_MISSING = "seal_artifact_missing"
    ARTIFACT_UNSEALED = "artifact_unsealed"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_UNREADABLE = "artifact_unreadable"
    ARTIFACT_SIZE = "artifact_size"
    ARTIFACT_DIGEST = "artifact_digest"
    ARTIFACT_EMPTY_NOT_SEALED = "artifact_empty_not_sealed"
    UNSTABLE = "unstable"


@dataclass(frozen=True)
class InventoryProblem:
    """A single reason why a collection is not ready or does not match."""

    code: ProblemCode
    message: str
    artifact: Optional[str] = None


@dataclass(frozen=True)
class ArtifactSeal:
    """The sealed version of one external artifact."""

    name: str
    size: int
    sha256: str
    version: Optional[str] = None


@dataclass(frozen=True)
class SealManifest:
    """A parsed and validated seal document."""

    version: str
    artifacts: Mapping[str, ArtifactSeal]

    @property
    def is_zero_stock(self) -> bool:
        """Return ``True`` when the seal declares every artifact as empty."""
        return all(artifact.size == 0 for artifact in self.artifacts.values())


@dataclass(frozen=True)
class InventoryStatus:
    """The result of one inventory verification run."""

    state: InventoryState
    seal_version: Optional[str] = None
    problems: Tuple[InventoryProblem, ...] = ()
    attempts: int = 1
    manifest: Optional[SealManifest] = None

    @property
    def is_ready(self) -> bool:
        """Return ``True`` only for the ``READY`` state."""
        return self.state is InventoryState.READY

    @property
    def is_not_ready(self) -> bool:
        """Return ``True`` only for the ``NOT_READY`` state."""
        return self.state is InventoryState.NOT_READY

    @property
    def is_mismatch(self) -> bool:
        """Return ``True`` only for the ``MISMATCH`` state."""
        return self.state is InventoryState.MISMATCH

    @property
    def problem_codes(self) -> Tuple[ProblemCode, ...]:
        """Return the distinct problem codes in discovery order."""
        codes = []
        for problem in self.problems:
            if problem.code not in codes:
                codes.append(problem.code)
        return tuple(codes)

    def describe(self) -> str:
        """Return a compact human-readable summary suitable for logs or errors."""
        parts = [f"{self.state.label} (seal_version={self.seal_version or 'unknown'})"]
        parts.extend(problem.message for problem in self.problems)
        return "; ".join(parts)


class InventorySealMismatch(Exception):
    """Raised when a sealed inventory is present but contradicts its seal."""

    def __init__(self, status: InventoryStatus):
        self.status = status
        super().__init__(status.describe())


Reader = Callable[[Target], Optional[bytes]]


def read_target(target: Target) -> Optional[bytes]:
    """
    Read a target as bytes.

    ``None`` means the target does not exist.  The default formats of
    :class:`~luigi.local_target.LocalTarget` and
    :class:`~luigi.mock.MockTarget` produce text; text is encoded as UTF-8 so
    that a seal written through a normal Luigi target can be hashed
    deterministically.  Use a binary/Nop format for non-UTF-8 artifacts.
    """
    if not target.exists():
        return None
    with target.open("r") as handle:
        payload = handle.read()
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return bytes(payload)


def _duplicate_checking_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SealFormatError(f"duplicate JSON key {key!r} in seal")
        result[key] = value
    return result


def parse_seal(payload: bytes) -> SealManifest:
    """
    Parse and validate a seal document.

    :raises SealFormatError: if the document is not valid UTF-8 JSON, has
        duplicate keys, has an unknown structure, or contains an invalid
        artifact declaration.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SealFormatError(f"seal is not valid UTF-8: {exc}") from exc

    try:
        document = json.loads(text, object_pairs_hook=_duplicate_checking_object_pairs)
    except json.JSONDecodeError as exc:
        raise SealFormatError(f"seal is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise SealFormatError("seal must be a JSON object")

    seal_version = document.get("seal_version")
    if not isinstance(seal_version, str) or not seal_version.strip():
        raise SealFormatError("seal must contain a non-empty string 'seal_version'")

    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, dict) or not raw_artifacts:
        raise SealFormatError("seal must contain a non-empty 'artifacts' object")

    artifacts = {}
    for name, raw_entry in raw_artifacts.items():
        if not isinstance(name, str) or not name:
            raise SealFormatError(f"invalid artifact name {name!r}")
        if not isinstance(raw_entry, dict):
            raise SealFormatError(f"artifact {name!r} must be a JSON object")

        size = raw_entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SealFormatError(f"artifact {name!r} must have a non-negative integer 'size'")

        sha256 = raw_entry.get("sha256")
        if not isinstance(sha256, str) or not _SHA256_PATTERN.match(sha256):
            raise SealFormatError(f"artifact {name!r} must have a lowercase hex 'sha256' digest")

        version = raw_entry.get("version")
        if version is not None and (not isinstance(version, str) or not version.strip()):
            raise SealFormatError(f"artifact {name!r} 'version' must be a non-empty string when present")

        if size == 0 and sha256 != _EMPTY_SHA256:
            raise SealFormatError(f"artifact {name!r} is declared empty but its digest is not the empty digest")
        if size > 0 and sha256 == _EMPTY_SHA256:
            raise SealFormatError(f"artifact {name!r} has a non-empty size but the empty digest")

        artifacts[name] = ArtifactSeal(name=name, size=size, sha256=sha256, version=version)

    return SealManifest(version=seal_version, artifacts=artifacts)


def build_seal(
    seal_version: str,
    artifacts: Mapping[str, Target],
    *,
    versions: Optional[Mapping[str, str]] = None,
    reader: Reader = read_target,
) -> bytes:
    """
    Build a seal document for existing artifacts.

    This helper is intended for the external process that produces the files
    and publishes the seal.  Artifacts are read through ``reader``; missing or
    unreadable artifacts raise :class:`FileNotFoundError` or the underlying
    error rather than producing a seal that cannot be satisfied.

    :param versions: optional mapping of artifact name to its version label.
        When supplied it must cover exactly the artifacts being sealed; the
        labels are recorded in the seal alongside the size and digest.

    The output is UTF-8 JSON with sorted keys and a trailing newline so that the
    same artifact generation always produces identical seal bytes.
    """
    if not isinstance(seal_version, str) or not seal_version.strip():
        raise ValueError("seal_version must be a non-empty string")
    if not artifacts:
        raise ValueError("at least one artifact is required")
    if versions is not None:
        unknown = set(versions) - set(artifacts)
        if unknown:
            raise ValueError("versions supplied for unknown artifacts: %s" % ", ".join(sorted(unknown)))
        for name, version in versions.items():
            if not isinstance(version, str) or not version.strip():
                raise ValueError(f"artifact {name!r} version must be a non-empty string")

    entries = {}
    for name in sorted(artifacts):
        observation, _ = _observe(artifacts[name], reader)
        if not observation.readable:
            reason = observation.error or "does not exist"
            raise FileNotFoundError(f"artifact {name!r} {reason}")
        entry = {
            "size": observation.size,
            "sha256": observation.sha256,
        }
        if versions is not None:
            entry["version"] = versions[name]
        entries[name] = entry

    document = {"seal_version": seal_version, "artifacts": entries}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class _Observation:
    size: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[str] = None

    @property
    def missing(self) -> bool:
        return self.size is None and self.error is None

    @property
    def readable(self) -> bool:
        return self.size is not None


def _observe(target: Target, reader: Reader) -> Tuple[_Observation, Optional[bytes]]:
    try:
        payload = reader(target)
    except Exception as exc:
        # Includes a target disappearing between exists() and open(), a
        # permission failure, and undecodable text. All mean "cannot confirm".
        return _Observation(error=str(exc) or type(exc).__name__), None
    if payload is None:
        return _Observation(), None
    return _Observation(size=len(payload), sha256=hashlib.sha256(payload).hexdigest()), payload


def _problem(code: ProblemCode, message: str, artifact: Optional[str] = None) -> InventoryProblem:
    return InventoryProblem(code=code, message=message, artifact=artifact)


def _check_names(manifest: SealManifest, artifact_targets: Mapping[str, Target]) -> list[InventoryProblem]:
    problems = []
    sealed_names = set(manifest.artifacts)
    delivered_names = set(artifact_targets)
    for name in sorted(sealed_names - delivered_names):
        problems.append(_problem(ProblemCode.SEAL_ARTIFACT_MISSING, f"seal declares artifact {name!r} but the task does not provide it", name))
    for name in sorted(delivered_names - sealed_names):
        problems.append(_problem(ProblemCode.ARTIFACT_UNSEALED, f"artifact {name!r} is not covered by the seal", name))
    return problems


def _check_content(seal: ArtifactSeal, observation: _Observation) -> list[InventoryProblem]:
    if not observation.readable:
        if observation.error is not None:
            return [_problem(ProblemCode.ARTIFACT_UNREADABLE, f"artifact {seal.name!r} could not be read: {observation.error}", seal.name)]
        return [_problem(ProblemCode.ARTIFACT_MISSING, f"artifact {seal.name!r} is missing", seal.name)]

    size = observation.size
    problems = []
    if size == 0 and seal.size != 0:
        problems.append(
            _problem(
                ProblemCode.ARTIFACT_EMPTY_NOT_SEALED,
                f"artifact {seal.name!r} is empty but seal version {seal.version or '?'} expects {seal.size} bytes",
                seal.name,
            )
        )
    elif size != seal.size:
        problems.append(
            _problem(
                ProblemCode.ARTIFACT_SIZE,
                f"artifact {seal.name!r} has {size} bytes but seal version {seal.version or '?'} expects {seal.size}",
                seal.name,
            )
        )

    if observation.sha256 != seal.sha256:
        problems.append(
            _problem(
                ProblemCode.ARTIFACT_DIGEST,
                f"artifact {seal.name!r} has sha256 {observation.sha256} but seal version {seal.version or '?'} expects {seal.sha256}",
                seal.name,
            )
        )
    return problems


def _unstable_problem(message: str) -> InventoryProblem:
    return _problem(ProblemCode.UNSTABLE, message)


def inspect_inventory(
    seal: Target,
    artifacts: Mapping[str, Target],
    *,
    attempts: int = 3,
    reader: Reader = read_target,
) -> InventoryStatus:
    """
    Inspect a sealed inventory without ever reporting a mixed generation.

    The seal is read first, every artifact is read and checked against it, and
    then the seal and all successfully read artifacts are re-read.  Any change
    during the check causes the whole check to restart.  A conclusion is
    accepted only when the seal snapshot and all artifact snapshots are stable.

    :param seal: target containing the seal JSON document
    :param artifacts: mapping of sealed artifact name to its target
    :param attempts: maximum number of complete verification passes; when the
        seal or an artifact is missing/unreadable, or the collection is still
        changing after the last pass, the result is ``NOT_READY``
    :param reader: function used to read a target, mainly useful for testing;
        ``None`` means missing
    :raises ValueError: if ``attempts`` is less than one
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    artifact_targets = dict(artifacts)
    last_problems: Tuple[InventoryProblem, ...] = ()
    last_seal_version: Optional[str] = None
    last_state = InventoryState.NOT_READY

    for attempt in range(1, attempts + 1):
        seal_first, seal_first_bytes = _observe(seal, reader)
        if not seal_first.readable:
            return InventoryStatus(
                state=InventoryState.NOT_READY,
                problems=(
                    _problem(
                        ProblemCode.SEAL_UNREADABLE if seal_first.error is not None else ProblemCode.SEAL_MISSING,
                        "seal could not be read: %s" % seal_first.error if seal_first.error is not None else "seal is missing",
                    ),
                ),
                attempts=attempt,
            )

        if seal_first.size == 0:
            return InventoryStatus(
                state=InventoryState.NOT_READY,
                problems=(_problem(ProblemCode.SEAL_EMPTY, "seal is empty and cannot confirm any artifact"),),
                attempts=attempt,
            )

        observations = {name: _observe(target, reader)[0] for name, target in artifact_targets.items()}
        seal_second, _ = _observe(seal, reader)

        if seal_first != seal_second:
            last_problems = (_unstable_problem("seal changed while artifacts were being read"),)
            last_state = InventoryState.NOT_READY
            continue

        unstable = []
        for name, first in observations.items():
            if not first.readable:
                continue
            if _observe(artifact_targets[name], reader)[0] != first:
                unstable.append(name)
        if unstable:
            last_problems = (_unstable_problem("artifacts changed during verification: %s" % ", ".join(sorted(unstable))),)
            last_state = InventoryState.NOT_READY
            continue

        seal_third, _ = _observe(seal, reader)
        if seal_third != seal_first:
            last_problems = (_unstable_problem("seal changed before verification completed"),)
            last_state = InventoryState.NOT_READY
            continue

        try:
            manifest = parse_seal(seal_first_bytes or b"")
        except SealFormatError as exc:
            return InventoryStatus(
                state=InventoryState.MISMATCH,
                problems=(_problem(ProblemCode.SEAL_INVALID, str(exc)),),
                attempts=attempt,
            )

        last_seal_version = manifest.version
        name_problems = _check_names(manifest, artifact_targets)
        content_problems = []
        for name, seal_entry in manifest.artifacts.items():
            if name in observations:
                content_problems.extend(_check_content(seal_entry, observations[name]))

        problems = tuple(name_problems + content_problems)
        missing = any(problem.code in (ProblemCode.ARTIFACT_MISSING, ProblemCode.ARTIFACT_UNREADABLE) for problem in problems)
        if missing:
            # Waiting does not change a missing file within a single check;
            # return immediately so frequent completeness polls stay cheap.
            return InventoryStatus(state=InventoryState.NOT_READY, seal_version=manifest.version, problems=problems, attempts=attempt, manifest=manifest)
        state = InventoryState.MISMATCH if problems else InventoryState.READY
        return InventoryStatus(state=state, seal_version=manifest.version, problems=problems, attempts=attempt, manifest=manifest)

    return InventoryStatus(
        state=last_state,
        seal_version=last_seal_version,
        problems=last_problems or (_unstable_problem("inventory did not stabilize during verification"),),
        attempts=attempts,
    )


class SealedInventoryTask(luigi.ExternalTask):
    """
    External task that is complete only when its collection matches its seal.

    Subclasses implement :py:meth:`seal` and :py:meth:`artifacts`.  The task
    never runs; it is a gate over data produced outside Luigi.

    ``mismatch_is_fatal`` controls how a ``MISMATCH`` behaves in Luigi.
    ``True`` (the default) raises :py:class:`InventorySealMismatch` from
    :py:meth:`complete`, making Luigi report a broken task, which is distinct
    from the pending state of a collection that is not ready yet.  Set it to
    ``False`` when a contradictory delivery is expected to be corrected
    shortly; the task then stays pending and :py:meth:`inventory_status` still
    reports the mismatch.
    """

    verification_attempts = 3
    mismatch_is_fatal = True

    def seal(self) -> Target:
        """Return the target containing the seal document."""
        raise NotImplementedError

    def artifacts(self) -> Mapping[str, Target]:
        """Return the mapping of sealed artifact name to external target."""
        raise NotImplementedError

    def inventory_status(self) -> InventoryStatus:
        """Inspect the collection and return its three-state status."""
        return inspect_inventory(self.seal(), self.artifacts(), attempts=self.verification_attempts)

    def complete(self) -> bool:
        """Return ``True`` only for a stable, fully matching ``READY`` collection."""
        status = self.inventory_status()
        self.last_inventory_status = status
        if status.state is InventoryState.READY:
            return True
        if status.state is InventoryState.MISMATCH:
            self.on_mismatch(status)
            if self.mismatch_is_fatal:
                raise InventorySealMismatch(status)
        else:
            self.on_not_ready(status)
        return False

    def on_mismatch(self, status: InventoryStatus) -> None:
        """Hook for logging or alerting when the seal and delivery disagree."""
        logger.error("Sealed inventory %s does not match its seal: %s", self.task_id, status.describe())

    def on_not_ready(self, status: InventoryStatus) -> None:
        """Hook for logging or alerting when the collection is still incomplete."""
        logger.info("Sealed inventory %s is not ready: %s", self.task_id, status.describe())

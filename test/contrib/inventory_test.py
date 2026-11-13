# -*- coding: utf-8 -*-

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from luigi.contrib.inventory import (
    InventorySealMismatch,
    InventoryState,
    ProblemCode,
    SealFormatError,
    SealedInventoryTask,
    build_seal,
    inspect_inventory,
    parse_seal,
    read_target,
)
from luigi.local_target import LocalTarget


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _seal_bytes(seal_version, artifacts):
    document = {"seal_version": seal_version, "artifacts": {name: {"size": len(data), "sha256": _sha(data)} for name, data in artifacts.items()}}
    return json.dumps(document, sort_keys=True).encode("utf-8")


class _InventoryFixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="luigi-inventory-")
        self.addCleanup(shutil.rmtree, self.tempdir, True)

    def path(self, name):
        return os.path.join(self.tempdir, name)

    def write(self, name, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(self.path(name), "wb") as handle:
            handle.write(data)
        return LocalTarget(self.path(name))

    def target(self, name):
        return LocalTarget(self.path(name))

    def write_seal(self, seal_version, artifacts, name="seal.json"):
        self.write(name, _seal_bytes(seal_version, artifacts))
        return self.target(name)

    def inventory(self, artifacts, **kwargs):
        return inspect_inventory(self.target("seal.json"), {name: self.target(name) for name in artifacts}, **kwargs)


class InventoryStateTest(_InventoryFixture):
    def test_missing_seal_is_not_ready(self):
        status = self.inventory({})

        self.assertIs(status.state, InventoryState.NOT_READY)
        self.assertTrue(status.is_not_ready)
        self.assertFalse(status.is_ready)
        self.assertIn(ProblemCode.SEAL_MISSING, status.problem_codes)

    def test_empty_seal_is_not_ready(self):
        self.write("seal.json", b"")
        self.write("stock", "A,1\n")

        status = inspect_inventory(self.target("seal.json"), {"stock": self.target("stock")})

        self.assertIs(status.state, InventoryState.NOT_READY)
        self.assertIn(ProblemCode.SEAL_EMPTY, status.problem_codes)

    def test_partial_delivery_is_not_ready(self):
        self.write("stock", "A,1\n")
        self.write("locations", "L,1\n")
        self.write_seal("v1", {"stock": b"A,1\n", "locations": b"L,1\n", "lots": b"X,7\n"})

        status = self.inventory({"stock", "locations", "lots"})

        self.assertIs(status.state, InventoryState.NOT_READY)
        self.assertEqual(status.problem_codes, (ProblemCode.ARTIFACT_MISSING,))
        self.assertEqual(status.problems[0].artifact, "lots")

    def test_complete_match_is_ready(self):
        self.write("stock", "A,1\n")
        self.write("locations", "L,1\n")
        self.write("lots", "X,7\n")
        self.write_seal("v1", {"stock": b"A,1\n", "locations": b"L,1\n", "lots": b"X,7\n"})

        status = self.inventory({"stock", "locations", "lots"})

        self.assertIs(status.state, InventoryState.READY)
        self.assertTrue(status.is_ready)
        self.assertFalse(status.is_mismatch)
        self.assertEqual(status.seal_version, "v1")
        self.assertEqual(status.problems, ())
        self.assertEqual(status.attempts, 1)
        self.assertTrue(read_target(self.target("seal.json")))

    def test_sealed_empty_artifact_is_ready_zero_stock(self):
        self.write("stock", b"")
        self.write("locations", "")
        self.write_seal("v1", {"stock": b"", "locations": b""})

        status = self.inventory({"stock", "locations"})

        self.assertIs(status.state, InventoryState.READY)
        self.assertTrue(status.manifest.is_zero_stock)

    def test_empty_artifact_not_sealed_as_empty_is_mismatch(self):
        self.write("stock", b"")
        self.write_seal("v1", {"stock": b"A,1\n"})

        status = self.inventory({"stock"})

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertTrue(status.is_mismatch)
        self.assertIn(ProblemCode.ARTIFACT_EMPTY_NOT_SEALED, status.problem_codes)

    def test_unsealed_empty_artifact_is_mismatch(self):
        self.write("stock", b"")
        self.write("locations", b"")
        self.write_seal("v1", {"stock": b""})

        status = self.inventory({"stock", "locations"})

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertIn(ProblemCode.ARTIFACT_UNSEALED, status.problem_codes)

    def test_size_disagreement_is_mismatch(self):
        self.write("stock", "A,12\n")
        self.write_seal("v1", {"stock": b"A,1\n"})

        status = self.inventory({"stock"})

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertIn(ProblemCode.ARTIFACT_SIZE, status.problem_codes)
        self.assertIn(ProblemCode.ARTIFACT_DIGEST, status.problem_codes)

    def test_same_size_different_generation_is_mismatch(self):
        self.write("stock", "A,2\n")
        self.write_seal("v1", {"stock": b"A,1\n"})

        status = self.inventory({"stock"})

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertEqual(status.problem_codes, (ProblemCode.ARTIFACT_DIGEST,))

    def test_seal_names_artifact_task_does_not_provide_is_mismatch(self):
        self.write("stock", "A,1\n")
        self.write_seal("v1", {"stock": b"A,1\n", "lots": b"X,1\n"})

        status = self.inventory({"stock"})

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertIn(ProblemCode.SEAL_ARTIFACT_MISSING, status.problem_codes)

    def test_corrected_seal_recovers_without_stale_state(self):
        self.write("stock", "A,2\n")
        self.write_seal("v1", {"stock": b"A,1\n"})

        before = self.inventory({"stock"})
        self.assertIs(before.state, InventoryState.MISMATCH)

        self.write_seal("v2", {"stock": b"A,2\n"})
        after = self.inventory({"stock"})

        self.assertIs(after.state, InventoryState.READY)
        self.assertEqual(after.seal_version, "v2")

    def test_attempts_must_be_positive(self):
        with self.assertRaises(ValueError):
            inspect_inventory(self.target("seal.json"), {}, attempts=0)


class SealParsingTest(_InventoryFixture):
    def test_parse_rejects_duplicate_keys(self):
        payload = (
            '{"seal_version": "v1", "artifacts": {"stock": {"size": 0, "sha256": "%s"}, "stock": {"size": 0, "sha256": "%s"}}}' % (_sha(b""), _sha(b""))
        ).encode()

        with self.assertRaises(SealFormatError):
            parse_seal(payload)

    def test_parse_rejects_invalid_digest(self):
        payload = b'{"seal_version": "v1", "artifacts": {"stock": {"size": 1, "sha256": "nope"}}}'

        with self.assertRaises(SealFormatError):
            parse_seal(payload)

    def test_parse_rejects_empty_digest_for_non_empty_size(self):
        payload = ('{"seal_version": "v1", "artifacts": {"stock": {"size": 3, "sha256": "%s"}}}' % _sha(b"")).encode()

        with self.assertRaises(SealFormatError):
            parse_seal(payload)

    def test_invalid_seal_is_mismatch(self):
        self.write("seal.json", "{not json")
        self.write("stock", "A,1\n")

        status = inspect_inventory(self.target("seal.json"), {"stock": self.target("stock")})

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertIn(ProblemCode.SEAL_INVALID, status.problem_codes)

    def test_build_seal_is_deterministic_and_parses(self):
        stock = self.write("stock", "A,1\n")
        empty = self.write("locations", b"")

        first = build_seal("v1", {"stock": stock, "locations": empty})
        second = build_seal("v1", {"locations": empty, "stock": stock})
        manifest = parse_seal(first)

        self.assertEqual(first, second)
        self.assertEqual(manifest.version, "v1")
        self.assertEqual(manifest.artifacts["stock"].size, 4)
        self.assertEqual(manifest.artifacts["locations"].size, 0)
        self.assertEqual(manifest.artifacts["locations"].sha256, _sha(b""))

    def test_build_seal_rejects_missing_artifact(self):
        with self.assertRaises(FileNotFoundError):
            build_seal("v1", {"stock": self.target("missing")})


class _FixedReader:
    def __init__(self, contents):
        self.contents = contents

    def __call__(self, target):
        return self.contents[target.path]


class InventoryRaceTest(_InventoryFixture):
    def _paths(self, *names):
        return [self.path(name) for name in names]

    def test_old_then_new_artifact_read_never_reports_ready(self):
        seal_path, stock_path = self._paths("seal.json", "stock")
        old, new = b"A,1\n", b"A,2\n"
        seal = _seal_bytes("v1", {"stock": old})
        reads = {"stock": 0}

        def reader(target):
            if target.path == seal_path:
                return seal
            reads["stock"] += 1
            return old if reads["stock"] % 2 else new

        status = inspect_inventory(LocalTarget(seal_path), {"stock": LocalTarget(stock_path)}, attempts=3, reader=reader)

        self.assertIs(status.state, InventoryState.NOT_READY)
        self.assertIn(ProblemCode.UNSTABLE, status.problem_codes)
        self.assertEqual(status.attempts, 3)
        self.assertFalse(status.is_ready)

    def test_stable_new_file_against_old_seal_is_mismatch(self):
        seal_path, stock_path = self._paths("seal.json", "stock")
        old, new = b"A,1\n", b"A,2\n"
        seal = _seal_bytes("v1", {"stock": old})
        reader = _FixedReader({seal_path: seal, stock_path: new})

        status = inspect_inventory(LocalTarget(seal_path), {"stock": LocalTarget(stock_path)}, reader=reader)

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertEqual(status.problem_codes, (ProblemCode.ARTIFACT_DIGEST,))

    def test_seal_replaced_during_verification_retries_on_new_generation(self):
        seal_path, stock_path = self._paths("seal.json", "stock")
        old_stock, new_stock = b"A,1\n", b"A,2\n"
        old_seal = _seal_bytes("v1", {"stock": old_stock})
        new_seal = _seal_bytes("v2", {"stock": new_stock})
        seal_reads = {"count": 0}
        stock_reads = {"count": 0}

        def reader(target):
            if target.path == seal_path:
                seal_reads["count"] += 1
                return old_seal if seal_reads["count"] == 1 else new_seal
            stock_reads["count"] += 1
            return old_stock if stock_reads["count"] == 1 else new_stock

        status = inspect_inventory(LocalTarget(seal_path), {"stock": LocalTarget(stock_path)}, attempts=3, reader=reader)

        self.assertIs(status.state, InventoryState.READY)
        self.assertEqual(status.seal_version, "v2")
        self.assertEqual(status.attempts, 2)

    def test_three_file_mixed_generation_is_mismatch(self):
        seal_path, stock_path, locations_path, lots_path = self._paths("seal.json", "stock", "locations", "lots")
        stock_old, stock_new = b"S1\n", b"S2\n"
        locations_old, locations_new = b"L1\n", b"L2\n"
        lots_old, lots_new = b"X1\n", b"X2\n"
        new_seal = _seal_bytes("v2", {"stock": stock_new, "locations": locations_new, "lots": lots_new})
        reader = _FixedReader(
            {
                seal_path: new_seal,
                stock_path: stock_old,
                locations_path: locations_new,
                lots_path: lots_new,
            }
        )

        status = inspect_inventory(
            LocalTarget(seal_path),
            {"stock": LocalTarget(stock_path), "locations": LocalTarget(locations_path), "lots": LocalTarget(lots_path)},
            reader=reader,
        )

        self.assertIs(status.state, InventoryState.MISMATCH)
        self.assertEqual(status.problem_codes, (ProblemCode.ARTIFACT_DIGEST,))
        self.assertEqual([problem.artifact for problem in status.problems], ["stock"])

    def test_three_file_seal_flip_mid_check_settles_on_consistent_generation(self):
        seal_path, stock_path, locations_path, lots_path = self._paths("seal.json", "stock", "locations", "lots")
        stock_old, stock_new = b"S1\n", b"S2\n"
        locations_old, locations_new = b"L1\n", b"L2\n"
        lots_old, lots_new = b"X1\n", b"X2\n"
        seal_old = _seal_bytes("v1", {"stock": stock_old, "locations": locations_old, "lots": lots_old})
        seal_new = _seal_bytes("v2", {"stock": stock_new, "locations": locations_new, "lots": lots_new})
        seal_reads = {"count": 0}

        def reader(target):
            if target.path == seal_path:
                seal_reads["count"] += 1
                return seal_old if seal_reads["count"] <= 2 else seal_new
            if target.path == stock_path:
                return stock_new
            if target.path == locations_path:
                return locations_new
            return lots_new

        status = inspect_inventory(
            LocalTarget(seal_path),
            {"stock": LocalTarget(stock_path), "locations": LocalTarget(locations_path), "lots": LocalTarget(lots_path)},
            attempts=3,
            reader=reader,
        )

        self.assertIs(status.state, InventoryState.READY)
        self.assertEqual(status.seal_version, "v2")
        self.assertEqual(status.attempts, 2)

    def test_two_generations_alternating_never_complete(self):
        seal_path, stock_path, locations_path = self._paths("seal.json", "stock", "locations")
        old_stock, new_stock = b"A,1\n", b"A,2\n"
        old_locations, new_locations = b"L,1\n", b"L,2\n"
        old_seal = _seal_bytes("v1", {"stock": old_stock, "locations": old_locations})
        new_seal = _seal_bytes("v2", {"stock": new_stock, "locations": new_locations})
        calls = {"seal": 0, "stock": 0, "locations": 0}

        def reader(target):
            if target.path == seal_path:
                calls["seal"] += 1
                return old_seal if calls["seal"] % 2 else new_seal
            if target.path == stock_path:
                calls["stock"] += 1
                return old_stock if calls["stock"] % 2 else new_stock
            calls["locations"] += 1
            return new_locations if calls["locations"] % 2 else old_locations

        status = inspect_inventory(
            LocalTarget(seal_path),
            {"stock": LocalTarget(stock_path), "locations": LocalTarget(locations_path)},
            attempts=4,
            reader=reader,
        )

        self.assertIs(status.state, InventoryState.NOT_READY)
        self.assertIn(ProblemCode.UNSTABLE, status.problem_codes)

    def test_artifact_missing_at_observation_is_not_ready_without_retry(self):
        seal_path, stock_path = self._paths("seal.json", "stock")
        seal = _seal_bytes("v1", {"stock": b"A,1\n"})
        reads = {"count": 0}

        def reader(target):
            if target.path == seal_path:
                return seal
            reads["count"] += 1
            return None

        status = inspect_inventory(LocalTarget(seal_path), {"stock": LocalTarget(stock_path)}, attempts=3, reader=reader)

        self.assertIs(status.state, InventoryState.NOT_READY)
        self.assertIn(ProblemCode.ARTIFACT_MISSING, status.problem_codes)
        self.assertEqual(status.attempts, 1)


class SealedInventoryTaskTest(_InventoryFixture):
    def _task(self, mismatch_is_fatal):
        seal = self.target("seal.json")
        artifacts = {"stock": self.target("stock")}

        class Task(SealedInventoryTask):
            def seal(self):
                return seal

            def artifacts(self):
                return artifacts

        Task.mismatch_is_fatal = mismatch_is_fatal
        return Task()

    def test_ready_completes(self):
        self.write("stock", "A,1\n")
        self.write_seal("v1", {"stock": b"A,1\n"})

        task = self._task(mismatch_is_fatal=False)

        self.assertTrue(task.complete())
        self.assertIs(task.last_inventory_status.state, InventoryState.READY)

    def test_not_ready_is_incomplete(self):
        self.write_seal("v1", {"stock": b"A,1\n"})

        task = self._task(mismatch_is_fatal=False)

        self.assertFalse(task.complete())
        self.assertIs(task.last_inventory_status.state, InventoryState.NOT_READY)

    def test_mismatch_is_never_reported_complete(self):
        self.write("stock", "A,2\n")
        self.write_seal("v1", {"stock": b"A,1\n"})

        task = self._task(mismatch_is_fatal=False)

        self.assertFalse(task.complete())
        self.assertIs(task.last_inventory_status.state, InventoryState.MISMATCH)

    def test_mismatch_is_fatal_by_default(self):
        self.write("stock", "A,2\n")
        self.write_seal("v1", {"stock": b"A,1\n"})
        seal = self.target("seal.json")
        artifacts = {"stock": self.target("stock")}

        class Task(SealedInventoryTask):
            def seal(self):
                return seal

            def artifacts(self):
                return artifacts

        with self.assertRaises(InventorySealMismatch) as ctx:
            Task().complete()
        self.assertIs(ctx.exception.status.state, InventoryState.MISMATCH)

    def test_mismatch_opt_out_stays_pending(self):
        self.write("stock", "A,2\n")
        self.write_seal("v1", {"stock": b"A,1\n"})
        seal = self.target("seal.json")
        artifacts = {"stock": self.target("stock")}

        class Task(SealedInventoryTask):
            mismatch_is_fatal = False

            def seal(self):
                return seal

            def artifacts(self):
                return artifacts

        task = Task()

        self.assertFalse(task.complete())
        self.assertIs(task.last_inventory_status.state, InventoryState.MISMATCH)

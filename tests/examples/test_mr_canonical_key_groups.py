# Copyright 2020 Yelp and Contributors
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
from io import BytesIO

from mrjob.examples.mr_canonical_key_groups import (
    MRCanonicalKeyGroupsJob)
from mrjob.canonical import Quarantine

from tests.sandbox import BasicTestCase


def freeze(value):
    """Recursively make a JSON value hashable so it can index a dict."""
    if isinstance(value, dict):
        return tuple(sorted(
            (member, freeze(item)) for member, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def run_frozen_job(args, raw_input):
    job = MRCanonicalKeyGroupsJob(args)
    job.sandbox(stdin=BytesIO(raw_input))

    with job.make_runner() as runner:
        runner.run()
        return {
            freeze(key): value
            for key, value in job.parse_output(runner.cat_output())
        }, runner


# two mappers (here, two spellings) describe the same grouping key
INPUT = b'\n'.join([
    b'{"partition":"p1","id":"r1","key":{"a":1,"b":2},"v":10}',
    b'{"partition":"p1","id":"r2","key":{"b":2.0,"a":1},"v":5}',
    b'{"partition":"p1","id":"r3","key":{"a":1},"v":1}',
    b'{"partition":"p1","id":"r4","key":[1,2],"v":1}',
    b'{"partition":"p1","id":"r5","key":[2,1],"v":1}',
    b'{"partition":"p1","id":"r6","key":true,"v":1}',
    b'{"partition":"p1","id":"r7","key":1,"v":1}',
    b'{"partition":"p1","id":"r8","key":{"a":1,"a":2},"v":1}',
    b'{"partition":"p1","id":"r9",'
    b'"key":0.1234567890123456789012345678901,"v":1}',
    b'{"partition":"p2","id":"r10","key":{"a":1,"b":2},"v":7}',
]) + b'\n'


class MRCanonicalKeyGroupsJobTestCase(BasicTestCase):

    def test_canonical_batches_merge_equal_keys(self):
        output, _ = run_frozen_job(
            ['--canonical-batches', 'p1'], INPUT)

        # 1 vs 1.0 and reordered members collapse into one group
        self.assertEqual(output[freeze({'a': 1, 'b': 2})], 15)
        # a genuinely missing field stays its own group
        self.assertEqual(output[freeze({'a': 1})], 1)
        # array order is significant
        self.assertEqual(output[(1, 2)], 1)
        self.assertEqual(output[(2, 1)], 1)
        # booleans are a business category, not the number 1
        self.assertEqual(output[True], 1)
        self.assertEqual(output[1], 1)
        # the legacy batch never mixes with canonical groups
        self.assertEqual(
            output[freeze(('__legacy__', 'p2', {'a': 1, 'b': 2}))], 7)

    def test_quarantine_names_partitions_to_reprocess(self):
        job = MRCanonicalKeyGroupsJob(['--canonical-batches', 'p1'])
        job.sandbox(stdin=BytesIO(INPUT))

        with job.make_runner() as runner:
            runner.run()

            notices = [
                (key, value)
                for key, value in
                job.parse_output(runner.cat_output())
                if key == '__quarantine__'
            ]

            self.assertEqual(len(notices), 2)
            reasons = sorted(value['reason'] for _, value in notices)
            self.assertEqual(reasons, [
                Quarantine.DUPLICATE_MEMBER,
                Quarantine.SOURCE_PRECISION])

            # every quarantined record points back at its source record
            self.assertEqual(
                sorted(value['record_id'] for _, value in notices),
                ['r8', 'r9'])
            for _, value in notices:
                self.assertEqual(value['partition'], 'p1')

            counters = runner.counters()[0]['Canonical Key Quarantine']
            self.assertEqual(counters['duplicate-member'], 1)
            self.assertEqual(counters['source-precision'], 1)

    def test_unsafe_records_never_reach_a_group(self):
        job = MRCanonicalKeyGroupsJob(['--canonical-batches', 'p1'])
        job.sandbox(stdin=BytesIO(INPUT))

        with job.make_runner() as runner:
            runner.run()
            raw = b''.join(runner.cat_output())

        # the duplicate's last-wins value must not appear as a group
        self.assertNotIn(b'{"a":2}', raw)
        # the over-precise number must not be rounded into a real group
        self.assertNotIn(b'0.12345678901234568', raw)

    def test_rollout_defaults_to_legacy_namespace(self):
        # no --canonical-batches: old behavior for every partition,
        # including the duplicate-member record (silent last-wins)
        output, _ = run_frozen_job([], INPUT)

        self.assertNotIn('__quarantine__', output)
        self.assertEqual(
            output[freeze(('__legacy__', 'p1', {'a': 2}))], 1)
        self.assertEqual(
            output[freeze(('__legacy__', 'p2', {'a': 1, 'b': 2}))], 7)

    def test_partial_rollout_isolates_batches(self):
        # enable p1 but not p2: the same shape appears in both
        # namespaces as distinct groups
        output, _ = run_frozen_job(
            ['--canonical-batches', 'p1'], INPUT)

        self.assertIn(freeze({'a': 1, 'b': 2}), output)
        self.assertIn(
            freeze(('__legacy__', 'p2', {'a': 1, 'b': 2})), output)

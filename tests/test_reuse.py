# Copyright 2026 mrjob contributors
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
"""Tests for per-step reuse of intermediate output (mrjob.reuse)."""
import json
import os
from os.path import join

from mrjob import cmd
from mrjob.inline import InlineMRJobRunner
from mrjob.job import MRJob
from mrjob.protocol import PickleProtocol
from mrjob.reuse import fingerprint_directory
from mrjob.reuse import fingerprint_file
from mrjob.reuse import fingerprint_side_input
from mrjob.reuse import load_mrjob_class
from mrjob.reuse import MANIFEST_VERSION
from mrjob.reuse import REASON_INPUT_MISMATCH
from mrjob.reuse import REASON_MANIFEST_CORRUPT
from mrjob.reuse import REASON_MANIFEST_MISSING
from mrjob.reuse import REASON_MISSING_SHARDS
from mrjob.reuse import REASON_PREVIOUS_STEP_RERUN
from mrjob.reuse import REASON_PROTOCOL_MISMATCH
from mrjob.reuse import REASON_SIDE_INPUT_MISMATCH
from mrjob.reuse import REASON_STEP_LOGIC_MISMATCH
from mrjob.reuse import REUSE
from mrjob.reuse import RERUN
from mrjob.reuse import ReuseStore
from mrjob.step import MRStep
from mrjob.tools.reuse_check import main as reuse_check_main
from mrjob.tools.reuse_check import plan_reuse

from tests.py2 import patch
from tests.py2 import StringIO
from tests.sandbox import SandboxedTestCase


# jobs used by the integration tests; defined in this real file so they
# have a source path to pass as mr_job_script

class CountJob(MRJob):
    """Two-step job: count words, then uppercase the display field."""

    def mapper1(self, _, line):
        for word in line.split():
            yield word, 1

    def reducer1(self, word, counts):
        yield word, sum(counts)

    def mapper2(self, word, count):
        yield word.upper(), count

    def steps(self):
        return [
            MRStep(mapper=self.mapper1, reducer=self.reducer1),
            MRStep(mapper=self.mapper2),
        ]


class DisplayOnlyChangeJob(CountJob):
    """Identical first step; only the downstream presentation changes."""

    def mapper2(self, word, count):
        yield 'word=' + word.upper(), count


class PickleOutputJob(CountJob):
    """Same logic, different final output protocol."""

    OUTPUT_PROTOCOL = PickleProtocol


class InstrumentedCountJob(CountJob):
    """Counts how many times its first-step mapper actually runs."""

    mapper_invocations = 0

    def mapper1(self, _, line):
        InstrumentedCountJob.mapper_invocations += 1
        for word in line.split():
            yield word, 1


class FingerprintTestCase(SandboxedTestCase):

    def test_file_fingerprint_changes_with_content(self):
        path = self.makefile('data.txt', b'hello\n')

        fp1 = fingerprint_file(path)
        fp2 = fingerprint_file(path)

        self.assertEqual(fp1, fp2)
        self.assertEqual(fp1['size'], 6)
        self.assertIn('sha256', fp1)
        self.assertEqual(fp1['path'], os.path.abspath(path))

        with open(path, 'ab') as f:
            f.write(b'more\n')

        self.assertNotEqual(fingerprint_file(path)['sha256'], fp1['sha256'])

    def test_side_input_keeps_name(self):
        path = self.makefile('side.csv', b'a,b\n')
        fp = fingerprint_side_input('lookup', path)

        self.assertEqual(fp['name'], 'lookup')
        self.assertIn('sha256', fp)

    def test_directory_fingerprint_detects_nested_change(self):
        directory = self.makedirs('lookup')
        self.makefile('lookup/a.txt', b'one')
        self.makefile('lookup/sub/b.txt', b'two')

        fp1 = fingerprint_directory(directory, 'lookup')
        self.assertTrue(fp1['is_dir'])
        self.assertEqual(fp1['num_files'], 2)

        self.makefile('lookup/sub/b.txt', b'two-changed')
        fp2 = fingerprint_directory(directory, 'lookup')

        self.assertNotEqual(fp1['sha256'], fp2['sha256'])

        # adding a file is detected too
        self.makefile('lookup/c.txt', b'three')
        fp3 = fingerprint_directory(directory, 'lookup')
        self.assertNotEqual(fp2['sha256'], fp3['sha256'])


class ReuseStoreDecisionTestCase(SandboxedTestCase):

    def setUp(self):
        super(ReuseStoreDecisionTestCase, self).setUp()
        self.store = ReuseStore(join(self.tmp_dir, 'store'))
        self.input_path = self.makefile('input.txt', b'cat dog\ncat\n')
        self.step_desc = {'type': 'streaming',
                          'mapper': {'type': 'script'},
                          'reducer': {'type': 'script'}}
        self.logic = {'mapper': {'module': 'm', 'qualname': 'f',
                                 'sha256': 'aaa'},
                      'reducer': {'module': 'm', 'qualname': 'g',
                                  'sha256': 'bbb'}}

    def _inputs(self):
        return [fingerprint_file(self.input_path)]

    def _seed(self, step_num=0):
        self.store.ensure_exists()
        self.store.write_manifest(step_num, {
            'input_manifest': self._inputs(),
            'side_inputs': [],
            'output_protocol': 'mrjob.protocol.JSONProtocol',
            'step_desc': self.step_desc,
            'step_logic': self.logic,
        })
        output_dir = self.makedirs('out')
        part = join(output_dir, 'part-00000')
        with open(part, 'wb') as f:
            f.write(b'cat\tdog\n')
        self.store.snapshot_shards(step_num, [part])

    def _explain(self, step_num=0, previous_reusable=True, **overrides):
        kwargs = dict(
            step_desc=self.step_desc,
            input_fingerprints=self._inputs(),
            side_input_fingerprints=[],
            output_protocol='mrjob.protocol.JSONProtocol',
            previous_step_reusable=previous_reusable,
            step_logic=self.logic)
        kwargs.update(overrides)
        return self.store.explain_step(step_num, **kwargs)

    def test_missing_manifest(self):
        decision = self._explain()
        self.assertEqual(decision.action, RERUN)
        self.assertEqual(decision.reason, REASON_MANIFEST_MISSING)

    def test_reuse_when_everything_matches(self):
        self._seed()
        decision = self._explain()
        self.assertTrue(decision.reusable)
        self.assertIsNone(decision.reason)

    def test_input_mismatch(self):
        self._seed()
        self.makefile('input.txt', b'completely different bytes\n')
        decision = self._explain()
        self.assertEqual(decision.reason, REASON_INPUT_MISMATCH)

    def test_side_input_mismatch(self):
        self._seed()
        side = self.makefile('side.txt', b'v1\n')
        fp = fingerprint_side_input('side', side)
        decision = self._explain(side_input_fingerprints=[fp])
        self.assertEqual(decision.reason, REASON_SIDE_INPUT_MISMATCH)

        # same name, new content is still a mismatch
        with open(side, 'w') as f:
            f.write('v2\n')
        fp2 = fingerprint_side_input('side', side)
        decision = self._explain(side_input_fingerprints=[fp2])
        self.assertEqual(decision.reason, REASON_SIDE_INPUT_MISMATCH)

    def test_protocol_mismatch(self):
        self._seed()
        decision = self._explain(
            output_protocol='mrjob.protocol.PickleProtocol')
        self.assertEqual(decision.reason, REASON_PROTOCOL_MISMATCH)

    def test_step_description_mismatch(self):
        self._seed()
        decision = self._explain(
            step_desc=dict(self.step_desc, jobconf={'x': '1'}))
        self.assertEqual(decision.reason, 'step_description_mismatch')

    def test_step_logic_mismatch(self):
        self._seed()
        changed_logic = dict(self.logic)
        changed_logic['mapper'] = dict(self.logic['mapper'], sha256='zzz')
        decision = self._explain(step_logic=changed_logic)
        self.assertEqual(decision.reason, REASON_STEP_LOGIC_MISMATCH)

    def test_corrupt_manifest(self):
        self._seed()
        with open(self.store.manifest_path(0), 'w') as f:
            f.write('{not json')
        decision = self._explain()
        self.assertEqual(decision.reason, REASON_MANIFEST_CORRUPT)

    def test_unsupported_manifest_version(self):
        self._seed()
        path = self.store.manifest_path(0)
        manifest = json.load(open(path))
        manifest['version'] = MANIFEST_VERSION + 99
        with open(path, 'w') as f:
            json.dump(manifest, f)
        decision = self._explain()
        self.assertEqual(decision.reason, REASON_MANIFEST_CORRUPT)

    def test_missing_shard(self):
        self._seed()
        os.remove(self.store.shard_path(0, 'part-00000'))
        decision = self._explain()
        self.assertEqual(decision.reason, REASON_MISSING_SHARDS)
        self.assertEqual(decision.detail['missing'], ['part-00000'])

    def test_changed_shard(self):
        self._seed()
        shard = self.store.shard_path(0, 'part-00000')
        with open(shard, 'ab') as f:
            f.write(b'tampered\n')
        decision = self._explain()
        self.assertEqual(decision.reason, REASON_MISSING_SHARDS)
        self.assertEqual(decision.detail['changed'], ['part-00000'])

    def test_partial_store_cannot_pretend_to_be_complete(self):
        # a manifest with a shard list but no actual shard directory
        self.store.ensure_exists()
        self.store.write_manifest(0, {
            'input_manifest': self._inputs(),
            'side_inputs': [],
            'output_protocol': 'mrjob.protocol.JSONProtocol',
            'step_desc': self.step_desc,
            'step_logic': self.logic,
        })
        self.store.snapshot_shards(0, [])
        decision = self._explain()
        self.assertEqual(decision.reason, REASON_MISSING_SHARDS)

    def test_previous_step_rerun_cascades(self):
        self._seed(step_num=1)
        decision = self._explain(
            step_num=1, previous_reusable=False)
        self.assertEqual(decision.reason, REASON_PREVIOUS_STEP_RERUN)

    def test_inspect_step_reports_completeness(self):
        self._seed()
        info = self.store.inspect_step(0)
        self.assertTrue(info['complete'])
        self.assertTrue(info['has_manifest'])

        os.remove(self.store.shard_path(0, 'part-00000'))
        info = self.store.inspect_step(0)
        self.assertFalse(info['complete'])

    def test_decision_to_dict_is_jsonable(self):
        self._seed()
        json.dumps(self._explain().to_dict())


class LoadJobClassTestCase(SandboxedTestCase):

    def test_loads_job_class_from_script(self):
        script = self.makefile('my_job.py', (
            'from mrjob.job import MRJob\n'
            'class MyJob(MRJob):\n'
            '    def mapper(self, k, v):\n'
            '        yield k, v\n'
            'if __name__ == "__main__":\n'
            '    MyJob.run()\n'))

        cls = load_mrjob_class(script)
        self.assertIsNotNone(cls)
        self.assertEqual(cls.__name__, 'MyJob')

    def test_returns_none_without_a_job(self):
        script = self.makefile('no_job.py', 'x = 1\n')
        self.assertIsNone(load_mrjob_class(script))


class InlineReuseTestCase(SandboxedTestCase):

    def _make_runner(self, job_cls, store_dir, input_paths,
                     upload_files=None):
        return InlineMRJobRunner(
            mr_job_script=os.path.abspath(__file__),
            mrjob_cls=job_cls,
            steps=job_cls([])._steps_desc(),
            input_paths=input_paths,
            reuse_store=store_dir,
            upload_files=upload_files or [],
            conf_paths=[])

    def _run(self, job_cls, store_dir, input_paths, upload_files=None):
        runner = self._make_runner(
            job_cls, store_dir, input_paths, upload_files)
        with runner:
            runner.run()
            output = dict(
                (k, v)
                for k, v in
                job_cls([]).parse_output(runner.cat_output()))
            return output, runner.reuse_decisions(), \
                runner.reuse_store_path()

    def test_full_reuse_on_identical_rerun(self):
        input_path = self.makefile('input.txt', b'cat dog\ncat\n')
        store = join(self.tmp_dir, 'store')

        output1, decisions1, store_path = self._run(
            CountJob, store, [input_path])

        self.assertEqual(store_path, os.path.abspath(store))
        self.assertEqual([d['action'] for d in decisions1],
                         [RERUN, RERUN])
        self.assertEqual(decisions1[0]['reason'], REASON_MANIFEST_MISSING)
        self.assertEqual(decisions1[1]['reason'],
                         REASON_PREVIOUS_STEP_RERUN)

        # every step cached a full set of shards
        for step_num in range(2):
            self.assertTrue(
                self.store_shards_complete(store, step_num))

        output2, decisions2, _ = self._run(
            CountJob, store, [input_path])

        self.assertEqual(output1, output2)
        self.assertEqual([d['action'] for d in decisions2],
                         [REUSE, REUSE])

    def test_display_only_change_does_not_recompute_first_step(self):
        input_path = self.makefile('input.txt', b'cat dog\ncat\n')
        store = join(self.tmp_dir, 'store')

        self._run(CountJob, store, [input_path])

        _, decisions, _ = self._run(
            DisplayOnlyChangeJob, store, [input_path])

        self.assertEqual(decisions[0]['action'], REUSE)
        self.assertEqual(decisions[1]['action'], RERUN)
        self.assertEqual(decisions[1]['reason'],
                         REASON_STEP_LOGIC_MISMATCH)

    def test_side_table_same_name_new_content_forces_rerun(self):
        input_path = self.makefile('input.txt', b'cat dog\n')
        side = self.makefile('side.txt', b'v1\n')
        store = join(self.tmp_dir, 'store')

        self._run(CountJob, store, [input_path], upload_files=[side])

        with open(side, 'w') as f:
            f.write('v2-different\n')

        _, decisions, _ = self._run(
            CountJob, store, [input_path], upload_files=[side])

        self.assertEqual(decisions[0]['reason'],
                         REASON_SIDE_INPUT_MISMATCH)
        self.assertEqual(decisions[1]['reason'],
                         REASON_PREVIOUS_STEP_RERUN)

    def test_input_change_forces_rerun(self):
        input_path = self.makefile('input.txt', b'cat dog\n')
        store = join(self.tmp_dir, 'store')

        self._run(CountJob, store, [input_path])

        with open(input_path, 'wb') as f:
            f.write(b'wholly different input bytes\n')

        _, decisions, _ = self._run(
            CountJob, store, [input_path])

        self.assertEqual(decisions[0]['reason'], REASON_INPUT_MISMATCH)
        self.assertEqual(decisions[1]['reason'],
                         REASON_PREVIOUS_STEP_RERUN)

    def test_protocol_change_only_reruns_final_step(self):
        input_path = self.makefile('input.txt', b'cat dog\n')
        store = join(self.tmp_dir, 'store')

        self._run(CountJob, store, [input_path])

        _, decisions, _ = self._run(
            PickleOutputJob, store, [input_path])

        self.assertEqual(decisions[0]['action'], REUSE)
        self.assertEqual(decisions[1]['reason'],
                         REASON_PROTOCOL_MISMATCH)

    def test_partial_store_is_recomputed_and_output_is_correct(self):
        input_path = self.makefile('input.txt', b'cat dog\ncat\n')
        store = join(self.tmp_dir, 'store')

        good_output, _, _ = self._run(
            CountJob, store, [input_path])

        # simulate a failed first run: manifest present but a shard gone
        shards_dir = join(store, 'step-0000', 'shards')
        os.remove(join(shards_dir, sorted(os.listdir(shards_dir))[0]))

        output, decisions, _ = self._run(
            CountJob, store, [input_path])

        self.assertEqual(decisions[0]['reason'], REASON_MISSING_SHARDS)
        self.assertEqual(decisions[1]['reason'],
                         REASON_PREVIOUS_STEP_RERUN)
        self.assertEqual(output, good_output)

    def test_plan_reuse_does_not_run_and_matches(self):
        input_path = self.makefile('input.txt', b'cat dog\n')
        store = join(self.tmp_dir, 'store')

        self._run(CountJob, store, [input_path])

        runner = self._make_runner(CountJob, store, [input_path])
        decisions = runner.plan_reuse()

        self.assertEqual([d['action'] for d in decisions],
                         [REUSE, REUSE])

    def test_plan_with_missing_side_table_reports_rerun_not_crash(self):
        input_path = self.makefile('input.txt', b'cat dog\n')
        side = self.makefile('side.txt', b'v1\n')
        store = join(self.tmp_dir, 'store')

        self._run(CountJob, store, [input_path], upload_files=[side])

        # the side table goes away; planning must still return a decision
        os.remove(side)
        runner = self._make_runner(
            CountJob, store, [input_path], upload_files=[side])
        decisions = runner.plan_reuse()

        self.assertEqual(decisions[0]['action'], RERUN)
        self.assertEqual(decisions[1]['action'], RERUN)

    def test_reused_step_does_not_invoke_its_mapper(self):
        input_path = self.makefile('input.txt', b'cat dog\ncat\n')
        store = join(self.tmp_dir, 'store')

        InstrumentedCountJob.mapper_invocations = 0
        self._run(InstrumentedCountJob, store, [input_path])
        first_run_invocations = InstrumentedCountJob.mapper_invocations
        self.assertGreater(first_run_invocations, 0)

        # identical rerun reuses step 0, so its mapper never runs again
        InstrumentedCountJob.mapper_invocations = 0
        _, decisions, _ = self._run(
            InstrumentedCountJob, store, [input_path])

        self.assertEqual(decisions[0]['action'], REUSE)
        self.assertEqual(InstrumentedCountJob.mapper_invocations, 0)

    def test_no_store_means_no_reuse(self):
        input_path = self.makefile('input.txt', b'cat dog\n')
        runner = InlineMRJobRunner(
            mr_job_script=os.path.abspath(__file__),
            mrjob_cls=CountJob,
            steps=CountJob([])._steps_desc(),
            input_paths=[input_path],
            conf_paths=[])

        with runner:
            runner.run()
            self.assertEqual(runner.reuse_decisions(), [])
            self.assertIsNone(runner.reuse_store_path())

    def store_shards_complete(self, store, step_num):
        step_store = ReuseStore(store)
        info = step_store.inspect_step(step_num)
        return info['complete']


_REUSE_CHECK_JOB = (
    'from mrjob.job import MRJob\n'
    'from mrjob.step import MRStep\n'
    'class ReuseCheckJob(MRJob):\n'
    '    def mapper1(self, _, line):\n'
    '        for word in line.split():\n'
    '            yield word, 1\n'
    '    def reducer1(self, word, counts):\n'
    '        yield word, sum(counts)\n'
    '    def mapper2(self, word, count):\n'
    '        yield word.upper(), count\n'
    '    def steps(self):\n'
    '        return [MRStep(mapper=self.mapper1, reducer=self.reducer1),\n'
    '                MRStep(mapper=self.mapper2)]\n'
    'if __name__ == "__main__":\n'
    '    ReuseCheckJob.run()\n')


class ReuseCheckToolTestCase(SandboxedTestCase):

    def setUp(self):
        super(ReuseCheckToolTestCase, self).setUp()
        self.script = self.makefile('reuse_check_job.py',
                                    _REUSE_CHECK_JOB)
        self.input_path = self.makefile('input.txt', b'cat dog\ncat\n')
        self.store = join(self.tmp_dir, 'store')
        self.cls = load_mrjob_class(self.script)

    def _seed_store(self):
        runner = InlineMRJobRunner(
            mr_job_script=self.script,
            mrjob_cls=self.cls,
            steps=self.cls([])._steps_desc(),
            input_paths=[self.input_path],
            reuse_store=self.store,
            conf_paths=[])
        with runner:
            runner.run()

    def test_command_is_registered(self):
        self.assertIn('reuse-check', cmd.commands)

    def test_plan_reuse_helper_reports_reuse(self):
        self._seed_store()

        decisions = plan_reuse(
            self.store, self.script, [self.input_path])

        self.assertEqual(len(decisions), 2)
        self.assertTrue(all(d['action'] == REUSE for d in decisions))

    def test_main_text_output(self):
        self._seed_store()
        stdout = StringIO()

        with patch('sys.stdout', stdout):
            reuse_check_main([
                '-q', '--reuse-store', self.store,
                self.script, self.input_path])

        text = stdout.getvalue()
        self.assertIn('step 1: REUSE', text)
        self.assertIn('step 2: REUSE', text)
        self.assertIn('2 of 2 step(s) reusable', text)

    def test_main_json_output(self):
        self._seed_store()
        stdout = StringIO()

        with patch('sys.stdout', stdout):
            reuse_check_main([
                '-q', '--reuse-store', self.store, '--json',
                self.script, self.input_path])

        decisions = json.loads(stdout.getvalue())
        self.assertEqual(len(decisions), 2)
        for decision in decisions:
            self.assertEqual(decision['action'], REUSE)
            self.assertIn('reason_description', decision)

    def test_main_reports_rerun_for_unknown_store(self):
        # don't seed the store: every step must be a rerun
        stdout = StringIO()

        with patch('sys.stdout', stdout):
            reuse_check_main([
                '-q', '--reuse-store', self.store,
                self.script, self.input_path])

        text = stdout.getvalue()
        self.assertIn('RERUN', text)
        self.assertIn('0 of 2 step(s) reusable', text)


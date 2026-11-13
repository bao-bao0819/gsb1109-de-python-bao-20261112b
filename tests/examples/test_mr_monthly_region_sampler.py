import json
import random
from io import BytesIO

from mrjob.examples.mr_monthly_region_sampler import MRMonthlyRegionSampler
from mrjob.examples.mr_monthly_region_sampler import score_for_event

from tests.job import run_job
from tests.sandbox import SandboxedTestCase


def record(region, event_id, version=1, payload=None):
    return json.dumps({
        'region': region,
        'event_id': event_id,
        'version': version,
        'payload': event_id if payload is None else payload,
    })


def raw_input(records):
    return ('\n'.join(records) + '\n').encode('utf-8')


def run_sampler(args, input_bytes=b''):
    job = MRMonthlyRegionSampler(args)
    job.sandbox(stdin=BytesIO(input_bytes))

    with job.make_runner() as runner:
        runner.run()
        return dict(job.parse_output(runner.cat_output()))


def expected_sample_ids(event_ids, k, seed=''):
    return sorted(event_ids, key=lambda e: (score_for_event(seed, e), e))[:k]


class MRMonthlyRegionSamplerTestCase(SandboxedTestCase):

    def test_sample_size_is_required(self):
        self.assertRaises(ValueError, MRMonthlyRegionSampler, [])

    def test_empty(self):
        self.assertEqual(run_job(MRMonthlyRegionSampler(['--sample-size', '3'])),
                         {})

    def test_keeps_k_lowest_scoring_events_per_region(self):
        event_ids = ['us-%02d' % i for i in range(20)]

        output = run_job(
            MRMonthlyRegionSampler(['--sample-size', '5']),
            raw_input(record('us', event_id) for event_id in event_ids))

        sample = output['us']['sample']
        self.assertEqual(len(sample), 5)
        self.assertEqual(
            [entry['event_id'] for entry in sample],
            expected_sample_ids(event_ids, 5))
        self.assertEqual(output['us']['conflicts'], [])

    def test_each_region_gets_its_own_sample(self):
        records = []
        for region in ('eu', 'us'):
            records.extend(
                record(region, '%s-%02d' % (region, i)) for i in range(10))

        output = run_job(
            MRMonthlyRegionSampler(['--sample-size', '3']),
            raw_input(records))

        self.assertEqual(sorted(output), ['eu', 'us'])
        for region in ('eu', 'us'):
            sample = output[region]['sample']
            self.assertEqual(len(sample), 3)
            self.assertTrue(all(
                entry['event_id'].startswith(region) for entry in sample))

    def test_resent_events_dont_take_extra_slots(self):
        event_ids = ['e%d' % i for i in range(6)]

        # each event re-sent three times (e.g. at shard boundaries)
        records = [record('us', event_id)
                   for _ in range(3)
                   for event_id in event_ids]

        output = run_job(
            MRMonthlyRegionSampler(['--sample-size', '4']),
            raw_input(records))

        sample = output['us']['sample']
        self.assertEqual(len(sample), 4)
        self.assertEqual(
            [entry['event_id'] for entry in sample],
            expected_sample_ids(event_ids, 4))
        self.assertEqual(output['us']['conflicts'], [])

    def test_highest_declared_version_wins(self):
        records = [
            record('us', 'e1', version=1, payload='old'),
            record('us', 'e1', version=3, payload='new'),
            # a stale re-send of the old version must not win either
            record('us', 'e1', version=2, payload='older-than-new'),
        ]

        output = run_job(
            MRMonthlyRegionSampler(['--sample-size', '1']),
            raw_input(records))

        self.assertEqual(output['us']['sample'], [
            dict(event_id='e1', version=3, payload='new'),
        ])
        self.assertEqual(output['us']['conflicts'], [])

    def test_same_version_conflict_is_reported(self):
        records = [
            record('us', 'e1', version=2, payload='a'),
            record('us', 'e1', version=2, payload='b'),
        ]

        output = run_job(
            MRMonthlyRegionSampler(['--sample-size', '1']),
            raw_input(records))

        sample = output['us']['sample']
        self.assertEqual(len(sample), 1)
        self.assertEqual(sample[0]['event_id'], 'e1')
        self.assertEqual(sample[0]['version'], 2)
        # canonical payload choice is deterministic
        self.assertIn(sample[0]['payload'], ('a', 'b'))

        self.assertEqual(len(output['us']['conflicts']), 1)
        conflict = output['us']['conflicts'][0]
        self.assertEqual(conflict['event_id'], 'e1')
        self.assertEqual(conflict['version'], 2)
        self.assertEqual(sorted(conflict['payloads']), ['a', 'b'])

    def test_conflict_at_losing_version_is_not_reported(self):
        records = [
            record('us', 'e1', version=1, payload='a'),
            record('us', 'e1', version=1, payload='b'),
            record('us', 'e1', version=2, payload='c'),
        ]

        output = run_job(
            MRMonthlyRegionSampler(['--sample-size', '1']),
            raw_input(records))

        self.assertEqual(output['us']['sample'], [
            dict(event_id='e1', version=2, payload='c'),
        ])
        self.assertEqual(output['us']['conflicts'], [])

    def test_conflict_reported_even_if_event_is_not_sampled(self):
        event_ids = ['e%d' % i for i in range(10)]
        records = [record('us', event_id) for event_id in event_ids]

        # make sure the conflicted event can't make the top-3 sample
        third_best = sorted(event_ids,
                            key=lambda e: score_for_event('', e))[2]
        threshold = score_for_event('', third_best)
        conflicted = next(
            name for name in ('conflicted-%d' % i for i in range(100))
            if score_for_event('', name) > threshold)
        records += [
            record('us', conflicted, version=1, payload='x'),
            record('us', conflicted, version=1, payload='y'),
        ]

        output = run_job(
            MRMonthlyRegionSampler(['--sample-size', '3']),
            raw_input(records))

        self.assertEqual(len(output['us']['sample']), 3)
        conflicts = output['us']['conflicts']
        self.assertEqual([c['event_id'] for c in conflicts], [conflicted])
        # unsampled events are reported by payload hash
        self.assertEqual(len(conflicts[0]['payload_hashes']), 2)

    def test_cross_shard_conflict_is_reported(self):
        # the contradictory records land in different input files, so no
        # single mapper ever sees both payloads
        path_1 = self.makefile(
            'shard1.json', raw_input([record('us', 'e1', version=1,
                                             payload='a')]))
        path_2 = self.makefile(
            'shard2.json', raw_input([record('us', 'e1', version=1,
                                             payload='b')]))

        output = run_sampler(
            ['-r', 'inline', '--num-cores', '2', '--sample-size', '5',
             path_1, path_2])

        self.assertEqual(len(output['us']['sample']), 1)
        conflicts = output['us']['conflicts']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['event_id'], 'e1')
        self.assertEqual(conflicts[0]['version'], 1)
        self.assertEqual(sorted(conflicts[0]['payloads']), ['a', 'b'])

    def test_seed_changes_the_sample(self):
        event_ids = ['e%02d' % i for i in range(50)]
        input_bytes = raw_input(record('us', event_id)
                                for event_id in event_ids)

        output_1 = run_job(
            MRMonthlyRegionSampler(['--sample-size', '5', '--seed', 'one']),
            input_bytes)
        output_2 = run_job(
            MRMonthlyRegionSampler(['--sample-size', '5', '--seed', 'two']),
            input_bytes)

        sample_1 = [e['event_id'] for e in output_1['us']['sample']]
        sample_2 = [e['event_id'] for e in output_2['us']['sample']]

        self.assertEqual(sample_1,
                         expected_sample_ids(event_ids, 5, seed='one'))
        self.assertEqual(sample_2,
                         expected_sample_ids(event_ids, 5, seed='two'))
        self.assertNotEqual(sample_1, sample_2)

    def _mixed_records(self):
        records = []
        for region in ('eu', 'us'):
            for i in range(30):
                event_id = '%s-%02d' % (region, i)
                records.append(record(region, event_id))
                # boundary re-sends
                if i % 3 == 0:
                    records.append(record(region, event_id))
                # re-sends with a newer version
                if i % 5 == 0:
                    records.append(record(region, event_id, version=2,
                                          payload=event_id + '-v2'))
                # same-version contradictions
                if i % 7 == 0:
                    records.append(record(region, event_id, version=2,
                                          payload=event_id + '-alt'))
        return records

    def test_input_order_does_not_change_results(self):
        records = self._mixed_records()

        expected = run_job(
            MRMonthlyRegionSampler(['--sample-size', '4', '--seed', 's']),
            raw_input(records))

        for shuffle_seed in range(5):
            shuffled = list(records)
            random.Random(shuffle_seed).shuffle(shuffled)

            self.assertEqual(
                run_job(
                    MRMonthlyRegionSampler(
                        ['--sample-size', '4', '--seed', 's']),
                    raw_input(shuffled)),
                expected)

    def test_mapper_count_does_not_change_results(self):
        records = self._mixed_records()

        input_path = self.makefile('visits.json', raw_input(records))

        expected = run_sampler(
            ['-r', 'inline', '--num-cores', '1',
             '--sample-size', '4', '--seed', 's', input_path])

        self.assertEqual(
            run_sampler(
                ['-r', 'inline', '--num-cores', '8',
                 '--sample-size', '4', '--seed', 's', input_path]),
            expected)

        # several small input files (i.e. different sharding) as well
        paths = []
        for shard in range(4):
            shard_records = records[shard::4]
            paths.append(self.makefile('shard%d.json' % shard,
                                       raw_input(shard_records)))

        self.assertEqual(
            run_sampler(
                ['-r', 'inline', '--num-cores', '4',
                 '--sample-size', '4', '--seed', 's'] + paths),
            expected)

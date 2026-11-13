# Copyright 2020 Yelp
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
"""Tests for the per-store net sales job, including splitting-invariance."""
from io import BytesIO
from decimal import Decimal

from mrjob.examples.mr_store_sales import MRStoreSales
from mrjob.examples.mr_store_sales import _merge_values
from mrjob.examples.mr_store_sales import parse_store_sales_line
from mrjob.metrics import StoreSales

from tests.job import run_job
from tests.sandbox import BasicTestCase
from tests.sandbox import SandboxedTestCase


def _line(store, sales, units, is_return=False):
    return '%s\t%s\t%s\t%s' % (
        store, sales, units, 'true' if is_return else 'false')


def _run_lines(lines, *args, **kwargs):
    """Run MRStoreSales over *lines*, returning store => result dict."""
    return run_job(MRStoreSales(list(args)), _input(lines, **kwargs))


def _input(lines, ending=b'\n'):
    return ending.join(line.encode('utf-8') for line in lines) + ending


class ParseStoreSalesLineTestCase(BasicTestCase):

    def test_sale(self):
        self.assertEqual(
            parse_store_sales_line('A\t10.50\t2\tfalse'),
            ('A', Decimal('10.50'), 2, False))

    def test_return(self):
        self.assertEqual(
            parse_store_sales_line('A\t10.50\t2\ttrue'),
            ('A', Decimal('10.50'), 2, True))

    def test_boolean_aliases(self):
        for value in ('1', 'TRUE', 'True'):
            self.assertTrue(parse_store_sales_line(
                'A\t1\t1\t%s' % value)[3])
        for value in ('0', 'FALSE', 'False'):
            self.assertFalse(parse_store_sales_line(
                'A\t1\t1\t%s' % value)[3])

    def test_bad_field_count(self):
        self.assertRaises(ValueError, parse_store_sales_line, 'A\t1')

    def test_three_fields_defaults_to_sale(self):
        self.assertEqual(
            parse_store_sales_line('A\t10.50\t2'),
            ('A', Decimal('10.50'), 2, False))

    def test_negative_values_are_returns(self):
        self.assertEqual(
            parse_store_sales_line('A\t-10.50\t-2'),
            ('A', Decimal('-10.50'), -2, False))

    def test_empty_store(self):
        self.assertRaises(
            ValueError, parse_store_sales_line, '\t1\t1\tfalse')

    def test_bad_is_return(self):
        self.assertRaises(
            ValueError, parse_store_sales_line, 'A\t1\t1\tmaybe')

    def test_decimal_kept_exact(self):
        # the input value must not be turned into a binary float
        self.assertEqual(
            parse_store_sales_line('A\t0.005\t1\tfalse')[1],
            Decimal('0.005'))


class MRStoreSalesTestCase(SandboxedTestCase):

    def test_empty(self):
        self.assertEqual(run_job(MRStoreSales([])), {})

    def test_help(self):
        # argparse expands %(default)s; catch broken help strings
        self.assertRaises(SystemExit, MRStoreSales, ['--help'])

    def test_blank_lines_ignored(self):
        self.assertEqual(
            _run_lines([
                '',
                _line('A', '1.00', 1),
                '   ',
            ]),
            {
                'A': {
                    'net_sales': '1.00',
                    'net_units': 1,
                    'net_sales_per_item': '1.00',
                },
            })

    def test_basic(self):
        self.assertEqual(
            _run_lines([
                _line('A', '10.005', 3),
                _line('A', '10.00', 1, is_return=True),
                _line('B', '5.00', 2),
            ]),
            {
                'A': {
                    'net_sales': '0.01',
                    'net_units': 2,
                    'net_sales_per_item': '0.01',
                },
                'B': {
                    'net_sales': '5.00',
                    'net_units': 2,
                    'net_sales_per_item': '2.50',
                },
            })

    def test_zero_units_store_is_reported(self):
        self.assertEqual(
            _run_lines([
                _line('Z', '5.00', 1),
                _line('Z', '5.00', 1, is_return=True),
            ]),
            {
                'Z': {
                    'net_sales': '0.00',
                    'net_units': 0,
                    'net_sales_per_item': None,
                },
            })

    def test_zero_sales_store_is_reported(self):
        # zero sales is a valid settlement
        self.assertEqual(
            _run_lines([_line('Z', '0', 0)]),
            {
                'Z': {
                    'net_sales': '0.00',
                    'net_units': 0,
                    'net_sales_per_item': None,
                },
            })

    def test_returns_can_make_sales_negative(self):
        self.assertEqual(
            _run_lines([
                _line('A', '5.00', 1),
                _line('A', '-8.00', -2),
            ]),
            {
                'A': {
                    'net_sales': '-3.00',
                    'net_units': -1,
                    'net_sales_per_item': '3.00',
                },
            })

    def test_negative_amount_and_is_return_rejected(self):
        with self.assertRaises(ValueError):
            _run_lines([_line('A', '-8.00', -2, is_return=True)])

    def test_places_option(self):
        self.assertEqual(
            _run_lines(
                [_line('A', '10.555', 1)],
                '--places', '1'),
            {
                'A': {
                    'net_sales': '10.6',
                    'net_units': 1,
                    'net_sales_per_item': '10.6',
                },
            })

    def test_places_zero(self):
        self.assertEqual(
            _run_lines([_line('A', '10.5', 1)], '--places', '0'),
            {
                'A': {
                    'net_sales': '11',
                    'net_units': 1,
                    'net_sales_per_item': '11',
                },
            })

    def test_rounds_only_once(self):
        # rounding each row would give 0.01 + 0.01 = 0.02
        self.assertEqual(
            _run_lines([
                _line('A', '0.004', 1),
                _line('A', '0.004', 1),
            ]),
            {
                'A': {
                    'net_sales': '0.01',
                    'net_units': 2,
                    'net_sales_per_item': '0.01',
                },
            })

    def test_output_is_json(self):
        job = MRStoreSales([])
        job.sandbox(stdin=BytesIO(_input([_line('A', '1.00', 1)])),

                    )

        with job.make_runner() as runner:
            runner.run()
            output = list(job.parse_output(runner.cat_output()))

        self.assertEqual(len(output), 1)
        store, value = output[0]
        self.assertEqual(store, 'A')
        # JSONProtocol parses bytes back into a dict
        self.assertEqual(value['net_sales'], '1.00')


class MRStoreSalesInvarianceTestCase(SandboxedTestCase):
    """The same sales rows must settle the same way however they're split."""

    # a data set with:
    # * decimal fractions that don't round cleanly
    # * partials that cancel to zero within one split
    # * a store whose returns exceed its sales
    # * a store with zero net units
    LINES = [
        _line('A', '0.004', 1),
        _line('A', '0.004', 1),
        _line('A', '10.005', 3),
        _line('A', '10.00', 1, is_return=True),
        _line('B', '5.00', 2),
        _line('B', '5.00', 2, is_return=True),
        _line('C', '5.00', 1),
        _line('C', '8.00', 2, is_return=True),
    ]

    INPUT = _input(LINES)

    def _expected(self):
        return _run_lines(self.LINES)

    def _run(self, *args):
        return run_job(MRStoreSales(list(args)), self.INPUT)

    def test_all_split_sizes_agree(self):
        expected = self._expected()

        for num_cores in (1, 2, 3, 4, 8):
            # num_cores controls both the mapper and reducer count in the
            # local/inline simulator, so this produces very different splits
            self.assertEqual(
                self._run('--num-cores', str(num_cores)),
                expected)

    def test_multiple_input_files_agree(self):
        # two files still produce one logical result; which file a shop's
        # rows landed in must not matter
        first = self.makefile('input1.txt', _input(self.LINES[:3]))
        second = self.makefile('input2.txt', _input(self.LINES[3:]))

        self.assertEqual(
            _run_lines(self.LINES),  # same rows, one file
            run_job(MRStoreSales([first, second]), b''))

    def test_zero_partial_in_one_file_is_carried(self):
        # A's rows in file 1 cancel exactly to zero; the zero partial must
        # still reach the reducer and be merged with A's rows from file 2
        first = self.makefile(
            'input1.txt',
            _input([
                _line('A', '5.00', 1),
                _line('A', '5.00', 1, is_return=True),
            ]))

        second = self.makefile(
            'input2.txt',
            _input([_line('A', '2.00', 1)]))

        self.assertEqual(
            run_job(MRStoreSales([first, second]), b''),
            {
                'A': {
                    'net_sales': '2.00',
                    'net_units': 1,
                    'net_sales_per_item': '2.00',
                },
            })

    def test_store_with_only_returns_is_reported(self):
        self.assertEqual(
            _run_lines([
                _line('A', '-3.00', -1),
                _line('B', '1.00', 1),
            ]),
            {
                'A': {
                    'net_sales': '-3.00',
                    'net_units': -1,
                    'net_sales_per_item': '3.00',
                },
                'B': {
                    'net_sales': '1.00',
                    'net_units': 1,
                    'net_sales_per_item': '1.00',
                },
            })

    def test_skipping_combiner_agrees(self):
        # the combiner is an optimization; it must not change results
        expected = self._expected()

        job = MRStoreSales(['--num-cores', '3'])
        steps = job.steps()
        steps[0] = self._without_combiner(steps[0])
        job.steps = lambda: steps

        self.assertEqual(
            run_job(job, self.INPUT),
            expected)

    def test_double_combining_agrees(self):
        # simulate a combiner running twice on already-combined output
        expected = self._expected()

        job = MRStoreSales([])

        one_pass = list(job.combiner('A', [
            StoreSales('A', Decimal('0.004'), 1).as_dict(),
            StoreSales('A', Decimal('0.004'), 1).as_dict(),
        ]))

        # second pass feeds the first pass's output back into the combiner
        second_pass = list(job.combiner(one_pass[0][0], [one_pass[0][1]]))

        self.assertEqual(second_pass, one_pass)
        self.assertEqual(
            _merge_values('A', [second_pass[0][1]]),
            StoreSales('A', Decimal('0.008'), 2))
        self.assertEqual(expected['A']['net_sales'], '0.01')

    def test_reducer_accepts_mixed_partial_and_combined(self):
        # a reducer can receive some values already reduced by a combiner
        # and some that the combiner never saw; totals must not change
        job = MRStoreSales([])

        raw = [
            StoreSales('A', Decimal('0.004'), 1).as_dict(),
            StoreSales('A', Decimal('0.004'), 1).as_dict(),
            StoreSales('A', Decimal('10.005'), 3).as_dict(),
            StoreSales('A', Decimal('-10.00'), -1).as_dict(),
        ]
        combined = job.combiner('A', raw[:2])

        output = list(job.reducer(
            'A', [combined_value for _, combined_value in combined] +
            raw[2:]))

        self.assertEqual(
            output,
            [('A', {
                'net_sales': '0.01',
                'net_units': 4,
                'net_sales_per_item': '0.00',
            })])

    def test_merge_is_order_independent(self):
        values = [
            StoreSales('A', Decimal('0.004'), 1),
            StoreSales('A', Decimal('10.005'), 3),
            StoreSales('A', Decimal('-10.00'), -1),
        ]

        left = _merge_values('A', [v.as_dict() for v in values])
        right = _merge_values('A', [v.as_dict() for v in reversed(values)])

        self.assertEqual(left, right)

    def test_zero_partial_carried_to_reducer(self):
        # one mapper's partial for A is exactly zero; A must still appear
        lines = [
            _line('A', '5.00', 1),
            _line('A', '5.00', 1, is_return=True),
            _line('B', '1.00', 1),
        ]

        self.assertEqual(
            _run_lines(lines),
            {
                'A': {
                    'net_sales': '0.00',
                    'net_units': 0,
                    'net_sales_per_item': None,
                },
                'B': {
                    'net_sales': '1.00',
                    'net_units': 1,
                    'net_sales_per_item': '1.00',
                },
            })

    @classmethod
    def _without_combiner(cls, step):
        from mrjob.step import MRStep
        return MRStep(
            mapper=step['mapper'],
            mapper_init=step['mapper_init'],
            mapper_final=step['mapper_final'],
            reducer=step['reducer'],
        )

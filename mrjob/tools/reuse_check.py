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
"""Explain, per step, whether a rerun would reuse cached output or
recompute it, and why.

Usage::

    mrjob reuse-check --reuse-store DIR [--json] JOB_SCRIPT [INPUT ...]

This never runs the job; it builds the same reuse plan the inline runner
would use and prints one line per step, for example::

    step 1: REUSE   (all shards present and manifest matches)
    step 2: RERUN   (side table content changed)

A step is only reported as reusable when every output shard is present
and the stored manifest -- input list, side tables, output protocol,
and step description -- matches the current run. A partially completed
step directory always shows up as a rerun.

.. versionadded:: 0.7.5
"""
from __future__ import print_function

import json
from argparse import ArgumentParser

from mrjob.inline import InlineMRJobRunner
from mrjob.job import MRJob
from mrjob.reuse import load_mrjob_class
from mrjob.reuse import REUSE


def main(cl_args=None):
    arg_parser = _make_arg_parser()
    options = arg_parser.parse_args(cl_args)

    MRJob.set_up_logging(quiet=options.quiet, verbose=options.verbose)

    decisions = plan_reuse(
        options.reuse_store, options.job_script, options.inputs)

    if options.json:
        print(json.dumps(decisions, sort_keys=True, indent=2))
    else:
        for decision in decisions:
            action = decision['action'].upper()
            print('step %d: %-5s  (%s)' % (
                decision['step_num'] + 1,
                action,
                decision['reason_description']))

        reusable = [d for d in decisions if d['action'] == REUSE]
        print('\n%d of %d step(s) reusable' % (
            len(reusable), len(decisions)))


def plan_reuse(store_dir, job_script, input_paths=None):
    """Build (without running anything) the per-step reuse decisions for
    *job_script* reading *input_paths*, against the store at *store_dir*.
    """
    job_cls = load_mrjob_class(job_script)
    if job_cls is None:
        raise ValueError(
            'could not find an MRJob subclass in %s' % job_script)

    # an instance just to render step descriptions; [] keeps argparse
    # from reading this process's command line
    steps = job_cls([])._steps_desc()

    runner = InlineMRJobRunner(
        mr_job_script=job_script,
        mrjob_cls=job_cls,
        steps=steps,
        input_paths=input_paths or [],
        reuse_store=store_dir,
        conf_paths=[])

    return runner.plan_reuse()


def _make_arg_parser():
    arg_parser = ArgumentParser(
        prog='mrjob reuse-check',
        description='Show, per step, whether cached output would be'
                    ' reused or recomputed, and why.')

    arg_parser.add_argument(
        '-q', '--quiet', dest='quiet', action='store_true',
        help="Don't print anything to stderr")
    arg_parser.add_argument(
        '-v', '--verbose', dest='verbose', action='store_true',
        help='Print more messages to stderr')
    arg_parser.add_argument(
        '--reuse-store', dest='reuse_store', required=True,
        help='directory holding cached step manifests and shards')
    arg_parser.add_argument(
        '--json', dest='json', action='store_true',
        help='print the decisions as JSON')
    arg_parser.add_argument(
        dest='job_script',
        help='path to the MRJob script')
    arg_parser.add_argument(
        dest='inputs', nargs='*',
        help='input paths the job would read')

    return arg_parser


if __name__ == '__main__':
    main()

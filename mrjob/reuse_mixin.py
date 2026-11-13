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
"""Runner mixin that wires :py:mod:`mrjob.reuse` into step execution.

This is mixed into :py:class:`~mrjob.sim.SimMRJobRunner`, so both the
``inline`` and ``local`` runners can keep a persistent *reuse store* and
skip steps whose intermediate output is still valid on a rerun.

Pass ``reuse_store=<dir>`` to the runner to enable it. The store is
never cleaned up automatically; it is meant to outlive individual job
runs.
"""
import logging
import os
import shutil
from os.path import basename
from os.path import join

from mrjob.fs.local import _from_file_uri
from mrjob.reuse import fingerprint_directory
from mrjob.reuse import fingerprint_file
from mrjob.reuse import fingerprint_side_input
from mrjob.reuse import fingerprint_step_logic
from mrjob.reuse import load_mrjob_class
from mrjob.reuse import REASON_INPUT_MISMATCH
from mrjob.reuse import RERUN
from mrjob.reuse import ReuseStore
from mrjob.reuse import StepReuseDecision
from mrjob.setup import parse_legacy_hash_path

log = logging.getLogger(__name__)


class StepReuseMixin(object):
    """Adds per-step reuse of intermediate output to a runner.

    A runner using this mixin gains:

    * a ``reuse_store`` constructor keyword giving the directory in which
      to cache step manifests and output shards
    * :py:meth:`reuse_decisions`, which reports, per step, whether its
      output was reused or recomputed, and why
    """

    def __init__(self, reuse_store=None, **kwargs):
        super(StepReuseMixin, self).__init__(**kwargs)

        self._reuse_store_path = reuse_store
        self._reuse_store = (
            ReuseStore(reuse_store) if reuse_store else None)

        # one StepReuseDecision per planned step, in step order
        self._reuse_decisions = []

        # cache so we never hash the same side table twice
        self._side_input_fingerprints_cache = None

        # the MRJob class, for output-protocol probing (inline runners
        # already hold one; subprocess runners load it from the script)
        self._reuse_mrjob_cls = getattr(self, '_mrjob_cls', None)

        # captured before the runner starts generating wrapper scripts
        # (which also land in the working-dir manager), so this only
        # contains side tables the user actually asked for
        self._user_side_input_specs = self._collect_user_side_input_specs()

    ### public API ###

    def reuse_decisions(self):
        """Return a list of decision dicts, one per step that has been
        planned so far. Each dict has ``step_num``, ``action``
        (``'reuse'`` or ``'rerun'``), ``reason``, and
        ``reason_description`` keys.
        """
        return [decision.to_dict() for decision in self._reuse_decisions]

    def reuse_store_path(self):
        """Path of the reuse store, or ``None`` if reuse is disabled."""
        if self._reuse_store is None:
            return None
        return self._reuse_store.path

    def plan_reuse(self):
        """Compute reuse decisions for every step *without running the
        job*. This backs the ``mrjob reuse-check`` command. Returns the
        same list of dicts as :py:meth:`reuse_decisions`.
        """
        if not self._reuse_enabled():
            return []

        self._reuse_decisions = []
        predecessor_reusable = True

        for step_num in range(self._num_steps()):
            decision = self._plan_step_reuse(
                step_num, predecessor_reusable)
            predecessor_reusable = decision.reusable

        return self.reuse_decisions()

    def _reuse_enabled(self):
        return self._reuse_store is not None

    ### side tables ###

    def _collect_user_side_input_specs(self):
        """The side tables explicitly requested by the user, as
        ``(kind, path, name_or_None)`` tuples. ``kind`` is ``'file'``,
        ``'archive'``, or ``'dir'``.
        """
        specs = []

        for hash_path in self._opts['upload_files']:
            parsed = parse_legacy_hash_path(
                'file', hash_path, must_name='upload_files')
            specs.append(('file', parsed['path'], parsed['name']))

        for hash_path in self._opts['upload_archives']:
            parsed = parse_legacy_hash_path(
                'archive', hash_path, must_name='upload_archives')
            specs.append(('archive', parsed['path'], parsed['name']))

        for hash_path in self._opts.get('upload_dirs') or []:
            parsed = parse_legacy_hash_path(
                'dir', hash_path, must_name='upload_archives')
            specs.append(('dir', parsed['path'], parsed['name']))

        # side files passed programmatically via extra_args
        for extra_arg in self._extra_args:
            if (isinstance(extra_arg, dict) and
                    extra_arg.get('type') == 'file'):
                specs.append(
                    ('file', extra_arg['path'], extra_arg.get('name')))

        return specs

    def _side_input_fingerprints(self):
        """Fingerprints of every user-requested side table, sorted by
        working-directory name so list comparison is order-stable."""
        if self._side_input_fingerprints_cache is not None:
            return self._side_input_fingerprints_cache

        fingerprints = []

        for kind, path, name in self._user_side_input_specs:
            if not name:
                name = basename(path.rstrip('/' + os.sep))

            if kind == 'dir' or os.path.isdir(path):
                fingerprints.append(fingerprint_directory(path, name))
            else:
                fingerprints.append(fingerprint_side_input(name, path))

        fingerprints.sort(key=lambda fp: (fp['name'], fp['path']))

        self._side_input_fingerprints_cache = fingerprints
        return fingerprints

    ### inputs ###

    def _reuse_input_paths(self, step_num):
        """Resolved local input paths for step 0 (globs expanded,
        ``file://`` URIs stripped)."""
        paths = []
        for path_glob in self._get_input_paths():
            for path in self.fs.ls(path_glob):
                paths.append(_from_file_uri(path))
        return paths

    def _reuse_input_fingerprints(self, step_num, predecessor_reusable):
        """Fingerprints of the inputs the given step actually reads.

        For step 0 these are the resolved input files. For later steps
        they are the previous step's output shards -- either the cached
        copies (when the predecessor is reused, in which case the new
        tmp directory doesn't contain its output yet) or the shards the
        predecessor just produced in its output directory.
        """
        if step_num == 0:
            # sort so the input manifest has a canonical order across runs
            return [fingerprint_file(path)
                    for path in sorted(self._reuse_input_paths(step_num))]

        if predecessor_reusable:
            metadata = (
                self._reuse_store.read_shard_metadata(step_num - 1) or [])
            return [
                fingerprint_file(
                    self._reuse_store.shard_path(
                        step_num - 1, entry['name']))
                for entry in metadata
            ]

        # the predecessor was just recomputed, so its output shards are
        # in its output directory
        prev_output_dir = self._output_dir_for_step(step_num - 1)
        prev_shards = sorted(
            (_from_file_uri(path)
             for path in self.fs.ls(join(prev_output_dir, 'part-*'))),
            key=basename)
        return [fingerprint_file(path) for path in prev_shards]

    ### output protocol ###

    def _reuse_mrjob_class(self):
        # inline runners receive their job class through __init__ after
        # the mixin, so look it up lazily rather than only at init time
        cls = getattr(self, '_mrjob_cls', None) or self._reuse_mrjob_cls

        if cls is None and self._script_path:
            cls = load_mrjob_class(self._script_path)
            self._reuse_mrjob_cls = cls

        return cls

    def _reuse_write_step_type(self, step_num):
        """Which script substep's write protocol defines the step's
        output encoding."""
        step = self._get_step(step_num)

        if step.get('reducer'):
            return 'reducer'
        if step.get('combiner'):
            return 'combiner'
        return 'mapper'

    def _reuse_output_protocol_name(self, step_num):
        """Fully-qualified name of the protocol the step writes with,
        or ``None`` if it can't be determined (e.g. command steps)."""
        job_cls = self._reuse_mrjob_class()
        if job_cls is None:
            return None

        try:
            # pass [] explicitly so the job's argparse doesn't read
            # this process's command line
            job = job_cls([])
            _, protocol = job._pick_protocol_instances(
                step_num, self._reuse_write_step_type(step_num))
        except Exception as ex:
            log.debug('could not determine output protocol for step %d: %s'
                      % (step_num, ex))
            return None

        return '%s.%s' % (
            protocol.__class__.__module__, protocol.__class__.__name__)

    def _reuse_step_logic(self, step_num):
        """Source fingerprint of the step's mapper/reducer/combiner
        functions, or ``None`` if it can't be determined."""
        try:
            return fingerprint_step_logic(
                self._reuse_mrjob_class(),
                step_num,
                self._get_step(step_num))
        except Exception as ex:
            log.debug('could not fingerprint step %d logic: %s'
                      % (step_num, ex))
            return None

    ### planning, snapshots and restoration ###

    def _plan_step_reuse(self, step_num, predecessor_reusable):
        """Decide whether *step_num* can reuse its cached output,
        record the decision, and return it."""
        try:
            decision = self._reuse_store.explain_step(
                step_num,
                self._get_step(step_num),
                self._reuse_input_fingerprints(
                    step_num, predecessor_reusable),
                self._side_input_fingerprints(),
                self._reuse_output_protocol_name(step_num),
                predecessor_reusable,
                step_logic=self._reuse_step_logic(step_num))
        except (IOError, OSError) as ex:
            # e.g. an input or side table disappeared; a real run would
            # fail too, but a read-only plan/CLI must report a rerun
            # rather than crash
            decision = StepReuseDecision(
                step_num, RERUN, REASON_INPUT_MISMATCH,
                detail={'error': str(ex)})

        self._reuse_decisions.append(decision)

        if decision.reusable:
            log.info(
                'Reusing cached output for step %d of %d from %s' % (
                    step_num + 1, self._num_steps(),
                    self._reuse_store.step_dir(step_num)))
        else:
            log.info(
                'Recomputing step %d of %d: %s' % (
                    step_num + 1, self._num_steps(),
                    decision.reason_description))

        return decision

    def _snapshot_step_for_reuse(self, step_num, predecessor_reusable):
        """After a step has run, record its manifest and copy its output
        shards into the store. Any stale state for the step is removed
        first."""
        store = self._reuse_store
        store.ensure_exists()
        store.reset_step(step_num)

        manifest = {
            'input_manifest': self._reuse_input_fingerprints(
                step_num, predecessor_reusable),
            'side_inputs': self._side_input_fingerprints(),
            'output_protocol':
                self._reuse_output_protocol_name(step_num),
            'step_desc': self._get_step(step_num),
            'step_logic': self._reuse_step_logic(step_num),
        }
        store.write_manifest(step_num, manifest)

        output_dir = self._output_dir_for_step(step_num)
        shard_paths = sorted(
            _from_file_uri(path)
            for path in self.fs.ls(join(output_dir, 'part-*')))

        if not shard_paths:
            log.warning(
                'step %d produced no output shards; not caching it' %
                (step_num + 1))
            # leave the manifest in place but with no shards, which the
            # integrity check treats as incomplete
            return

        store.snapshot_shards(step_num, shard_paths)

    def _restore_reused_shards(self, step_num):
        """Copy a reusable step's cached shards into its output
        directory so downstream steps can read them as usual."""
        store = self._reuse_store
        output_dir = self._output_dir_for_step(step_num)

        self.fs.mkdir(output_dir)

        metadata = store.read_shard_metadata(step_num) or []
        for entry in metadata:
            source = store.shard_path(step_num, entry['name'])
            dest = join(output_dir, entry['name'])
            log.debug('restoring cached shard %s -> %s' % (source, dest))
            shutil.copyfile(source, dest)

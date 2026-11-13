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
"""Reuse of intermediate step output across reruns.

Reading the original input shards is usually the most expensive part of
an offline batch job. A *reuse store* lets a rerun skip steps whose
intermediate output is still valid instead of recomputing them.

A reuse store is a directory with one sub-directory per step::

    <store>/
        step-0000/
            manifest.json     # what the step's output was derived from
            shards.json       # every output shard the step produced
            shards/
                part-00000    # copy of the output shard
                part-00001
        step-0001/
            ...

A completed step may only be reused when its stored manifest matches the
current run on all of the following:

* the **input manifest** -- the same input files, in the same order,
  each with the same size and SHA-256 checksum
* the **side tables** the step reads (e.g. files passed with
  ``--files``/``--archives``) -- same names and content; a side table
  with the same name but new content forces a recompute
* the **output protocol** -- so a change to how output is encoded is
  detected, while a purely cosmetic downstream change (e.g. display
  fields) does not force the upstream step to rerun
* the **step description** -- the mapper/reducer/combiner configuration
  of the step itself

In addition, every shard listed in ``shards.json`` must actually be
present with matching content. A partially completed step (some shards
missing or tampered with) can never masquerade as a complete input for
the next step.
"""
import hashlib
import inspect
import json
import os
import shutil
from os.path import abspath
from os.path import exists
from os.path import isdir
from os.path import join


#: manifest format version, so future changes can be detected
MANIFEST_VERSION = 1

MANIFEST_NAME = 'manifest.json'
SHARDS_NAME = 'shards.json'
SHARDS_DIR = 'shards'
STEP_DIR_PREFIX = 'step-'

#: the step's output can be reused
REUSE = 'reuse'

#: the step has to be recomputed
RERUN = 'rerun'

# Stable reason codes (see :class:`StepReuseDecision`).
#: no reuse store was configured for this run
REASON_NO_STORE = 'no_store'
#: the step directory has no manifest yet (it never completed)
REASON_MANIFEST_MISSING = 'manifest_missing'
#: the manifest or shard list can't be parsed or has the wrong version
REASON_MANIFEST_CORRUPT = 'manifest_corrupt'
#: the input manifest (file list or content) no longer matches
REASON_INPUT_MISMATCH = 'input_mismatch'
#: a side table with the same name has different content
REASON_SIDE_INPUT_MISMATCH = 'side_input_mismatch'
#: the output protocol changed since the manifest was written
REASON_PROTOCOL_MISMATCH = 'output_protocol_mismatch'
#: the step description (mapper/reducer/combiner config) changed
REASON_STEP_MISMATCH = 'step_description_mismatch'
#: the code behind one of the step's mapper/reducer/combiner functions
#: changed (the rendered description is the same)
REASON_STEP_LOGIC_MISMATCH = 'step_logic_mismatch'
#: one or more output shards are missing, empty, or have changed
REASON_MISSING_SHARDS = 'missing_or_changed_shards'
#: the previous step is being recomputed, so this step's input changes
REASON_PREVIOUS_STEP_RERUN = 'previous_step_rerun'

# human-readable explanations for the reason codes
REASON_DESCRIPTIONS = {
    REASON_NO_STORE: 'no reuse store configured',
    REASON_MANIFEST_MISSING: 'no manifest from a previous run',
    REASON_MANIFEST_CORRUPT: 'manifest or shard list is unreadable',
    REASON_INPUT_MISMATCH: 'input manifest changed',
    REASON_SIDE_INPUT_MISMATCH: 'side table content changed',
    REASON_PROTOCOL_MISMATCH: 'output protocol changed',
    REASON_STEP_MISMATCH: 'step description changed',
    REASON_STEP_LOGIC_MISMATCH: 'step code changed',
    REASON_MISSING_SHARDS: 'not all output shards are present',
    REASON_PREVIOUS_STEP_RERUN: 'the previous step is being recomputed',
}


def fingerprint_file(path):
    """Return a fingerprint of the file at *path*: its absolute path,
    size in bytes, and SHA-256 checksum. Raises :py:class:`IOError`
    (or :py:class:`OSError`) if the file can't be read.
    """
    path = abspath(path)
    sha256 = hashlib.sha256()

    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            sha256.update(chunk)

    return {
        'path': path,
        'size': os.path.getsize(path),
        'sha256': sha256.hexdigest(),
    }


def fingerprint_side_input(name, path):
    """Fingerprint a named side table (a file or archive placed in the
    task working directory). The *name* matters because tasks refer to
    side tables by their working-directory name.
    """
    fp = fingerprint_file(path)
    fp['name'] = name
    return fp


def fingerprint_callable(func):
    """Fingerprint a function/method by its source and qualified name.

    The rendered step description only records ``{'type': 'script'}``
    for script steps, so it can't tell ``mapper`` from ``mapper2``. We
    instead hash the function's source and pair it with its qualified
    name. The module is deliberately excluded: the same script can be
    imported normally or loaded (e.g. via :pymod:`runpy`) under a
    different module name without its code changing, and that must not
    force a recompute. Returns ``None`` if the source can't be read
    (e.g. a C extension or a REPL-defined function).
    """
    try:
        qualname = getattr(func, '__qualname__', func.__name__)
        source = inspect.getsource(func)
    except (OSError, IOError, TypeError):
        return None

    sha256 = hashlib.sha256()
    sha256.update(source.encode('utf-8'))

    return {
        'qualname': qualname,
        'sha256': sha256.hexdigest(),
    }


def fingerprint_step_logic(job_cls, step_num, step_desc):
    """Build a per-substep source fingerprint of the code behind
    *step_num*'s mapper/reducer/combiner.

    Only script substeps (functions on the job class) are fingerprinted;
    command substeps are already fully described by *step_desc*.
    """
    if job_cls is None:
        return None

    # an instance is needed to resolve per-step method names; [] keeps
    # argparse from reading the current process's command line
    try:
        job = job_cls([])
        raw_steps = job.steps()
        raw_step = raw_steps[step_num]
    except Exception:
        return None

    logic = {}

    for substep in ('mapper', 'combiner', 'reducer'):
        rendered = step_desc.get(substep)
        if not rendered or rendered.get('type') != 'script':
            continue

        func = raw_step[substep]
        if func is None:
            # identity mapper/reducer shim supplied by MRStep
            logic[substep] = None
            continue

        logic[substep] = fingerprint_callable(func)

    return logic


def fingerprint_directory(path, name):
    """Fingerprint a side table directory (e.g. one passed with
    ``--dirs``). Hashes every file under *path*, together with its
    relative path, so adding, removing, or modifying any file changes
    the fingerprint.
    """
    root = abspath(path)
    sha256 = hashlib.sha256()
    total_size = 0
    num_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            file_path = join(dirpath, filename)
            rel_path = os.path.relpath(file_path, root)

            fp = fingerprint_file(file_path)
            total_size += fp['size']
            num_files += 1

            sha256.update(rel_path.encode('utf-8'))
            sha256.update(b'\0')
            sha256.update(fp['sha256'].encode('ascii'))
            sha256.update(b'\0')

    return {
        'name': name,
        'path': root,
        'is_dir': True,
        'size': total_size,
        'num_files': num_files,
        'sha256': sha256.hexdigest(),
    }


def _input_identity(fp):
    """Content identity of an input fingerprint, independent of its
    (possibly run-specific) absolute path."""
    return (fp['size'], fp['sha256'])


def _side_input_identity(fp):
    return (fp['name'], fp['size'], fp['sha256'])


class StepReuseDecision(object):
    """Whether one step can reuse its cached output, and why.

    :ivar int step_num: 0-indexed step number
    :ivar str action: :py:data:`REUSE` or :py:data:`RERUN`
    :ivar str reason: stable reason code from this module
    :ivar detail: optional object with specifics (e.g. the offending
                  file's path), useful for logging and the
                  ``mrjob reuse-check`` command
    """

    def __init__(self, step_num, action, reason=None, detail=None):
        self.step_num = step_num
        self.action = action
        self.reason = reason
        self.detail = detail

    @property
    def reusable(self):
        return self.action == REUSE

    @property
    def reason_description(self):
        if self.action == REUSE:
            return 'all shards present and manifest matches'
        return REASON_DESCRIPTIONS.get(self.reason, self.reason or '')

    def to_dict(self):
        return {
            'step_num': self.step_num,
            'action': self.action,
            'reason': self.reason,
            'reason_description': self.reason_description,
            'detail': self.detail,
        }

    def __repr__(self):
        return 'StepReuseDecision(%r, %r, reason=%r, detail=%r)' % (
            self.step_num, self.action, self.reason, self.detail)

    def __eq__(self, other):
        return (isinstance(other, StepReuseDecision) and
                self.to_dict() == other.to_dict())

    def __ne__(self, other):
        return not self == other


class ReuseStore(object):
    """A directory on the local filesystem holding per-step manifests and
    cached output shards, shared across reruns.

    The store is deliberately filesystem-based and self-describing so it
    can be inspected with ordinary tools and by ``mrjob reuse-check``
    without recreating a runner.
    """

    def __init__(self, store_dir):
        self._store_dir = abspath(store_dir)

    @property
    def path(self):
        return self._store_dir

    def ensure_exists(self):
        if not isdir(self._store_dir):
            os.makedirs(self._store_dir)

    # directory layout

    def step_dir(self, step_num):
        return join(
            self._store_dir, '%s%04d' % (STEP_DIR_PREFIX, step_num))

    def manifest_path(self, step_num):
        return join(self.step_dir(step_num), MANIFEST_NAME)

    def shards_meta_path(self, step_num):
        return join(self.step_dir(step_num), SHARDS_NAME)

    def shard_dir(self, step_num):
        return join(self.step_dir(step_num), SHARDS_DIR)

    def shard_path(self, step_num, name):
        return join(self.shard_dir(step_num), name)

    # reading

    def has_manifest(self, step_num):
        return exists(self.manifest_path(step_num))

    def _read_json(self, path):
        with open(path) as f:
            return json.load(f)

    def read_manifest(self, step_num):
        """Return the stored manifest, or ``None`` if it is missing or
        can't be parsed."""
        if not self.has_manifest(step_num):
            return None

        try:
            manifest = self._read_json(self.manifest_path(step_num))
        except (IOError, OSError, ValueError):
            return None

        if not isinstance(manifest, dict):
            return None

        return manifest

    def read_shard_metadata(self, step_num):
        """Return the stored shard metadata list, or ``None`` if it is
        missing or can't be parsed."""
        path = self.shards_meta_path(step_num)
        if not exists(path):
            return None

        try:
            shards = self._read_json(path)
        except (IOError, OSError, ValueError):
            return None

        if not isinstance(shards, list):
            return None

        return shards

    # writing

    def reset_step(self, step_num):
        """Remove everything stored for *step_num*, so a fresh run can't
        accidentally pick up stale state."""
        step_dir = self.step_dir(step_num)
        if isdir(step_dir):
            shutil.rmtree(step_dir)

    def write_manifest(self, step_num, manifest):
        step_dir = self.step_dir(step_num)
        if not isdir(step_dir):
            os.makedirs(step_dir)

        manifest = dict(manifest)
        manifest['version'] = MANIFEST_VERSION
        manifest['step_num'] = step_num

        with open(self.manifest_path(step_num), 'w') as f:
            json.dump(manifest, f, sort_keys=True, indent=2)
            f.write('\n')

    def snapshot_shards(self, step_num, shard_paths):
        """Copy *shard_paths* (the step's ``part-*`` outputs) into the
        store and write ``shards.json``. Each shard is fingerprinted at
        copy time so later tampering can be detected.
        """
        shard_dir = self.shard_dir(step_num)
        if isdir(shard_dir):
            shutil.rmtree(shard_dir)
        os.makedirs(shard_dir)

        metadata = []

        for path in sorted(shard_paths):
            name = os.path.basename(path)
            dest = self.shard_path(step_num, name)
            shutil.copyfile(path, dest)

            fp = fingerprint_file(dest)
            metadata.append({
                'name': name,
                'size': fp['size'],
                'sha256': fp['sha256'],
            })

        with open(self.shards_meta_path(step_num), 'w') as f:
            json.dump(metadata, f, sort_keys=True, indent=2)
            f.write('\n')

        return metadata

    # decisions

    def explain_step(self, step_num, step_desc, input_fingerprints,
                     side_input_fingerprints, output_protocol,
                     previous_step_reusable, step_logic=None):
        """Decide whether the cached output of *step_num* can be reused.

        All arguments describe the *current* run; the method compares
        them against the stored manifest and shards.

        :param step_desc: description dict of the step as produced by
                          :py:meth:`mrjob.step.MRStep.description`
        :param input_fingerprints: list of :py:func:`fingerprint_file`
                                   dicts for the step's resolved inputs
        :param side_input_fingerprints: list of
                                        :py:func:`fingerprint_side_input`
                                        dicts
        :param output_protocol: fully-qualified name of the protocol the
                                step writes, or ``None`` if it can't be
                                determined
        :param previous_step_reusable: whether the previous step is
                                       reused; a recomputed predecessor
                                       always forces a recompute
        :param step_logic: :py:func:`fingerprint_step_logic` dict for the
                           code behind the step's functions, or ``None``
                           if it can't be determined
        """
        if step_num > 0 and not previous_step_reusable:
            return StepReuseDecision(
                step_num, RERUN, REASON_PREVIOUS_STEP_RERUN)

        manifest = self.read_manifest(step_num)
        if manifest is None:
            if self.has_manifest(step_num):
                return StepReuseDecision(
                    step_num, RERUN, REASON_MANIFEST_CORRUPT)
            return StepReuseDecision(
                step_num, RERUN, REASON_MANIFEST_MISSING)

        if manifest.get('version') != MANIFEST_VERSION:
            return StepReuseDecision(
                step_num, RERUN, REASON_MANIFEST_CORRUPT,
                detail={'stored_version': manifest.get('version')})

        mismatch = self._find_manifest_mismatch(
            manifest, step_desc, input_fingerprints,
            side_input_fingerprints, output_protocol, step_logic)
        if mismatch is not None:
            return StepReuseDecision(step_num, RERUN, mismatch[0],
                                     detail=mismatch[1])

        shard_check = self._check_shards(step_num)
        if shard_check is not None:
            return StepReuseDecision(
                step_num, RERUN, REASON_MISSING_SHARDS,
                detail=shard_check)

        return StepReuseDecision(step_num, REUSE)

    def _find_manifest_mismatch(self, manifest, step_desc,
                                input_fingerprints, side_input_fingerprints,
                                output_protocol, step_logic):
        stored_inputs = manifest.get('input_manifest')
        if not isinstance(stored_inputs, list):
            return REASON_MANIFEST_CORRUPT, {'field': 'input_manifest'}

        current_inputs = [_input_identity(fp) for fp in input_fingerprints]
        if current_inputs != [
                (fp.get('size'), fp.get('sha256'))
                for fp in stored_inputs]:
            return REASON_INPUT_MISMATCH, {
                'stored': stored_inputs,
                'current': input_fingerprints,
            }

        stored_sides = manifest.get('side_inputs')
        if not isinstance(stored_sides, list):
            return REASON_MANIFEST_CORRUPT, {'field': 'side_inputs'}

        current_sides = [
            _side_input_identity(fp) for fp in side_input_fingerprints]
        if current_sides != [
                (fp.get('name'), fp.get('size'), fp.get('sha256'))
                for fp in stored_sides]:
            return REASON_SIDE_INPUT_MISMATCH, {
                'stored': stored_sides,
                'current': side_input_fingerprints,
            }

        if manifest.get('output_protocol') != output_protocol:
            return REASON_PROTOCOL_MISMATCH, {
                'stored': manifest.get('output_protocol'),
                'current': output_protocol,
            }

        if manifest.get('step_desc') != step_desc:
            return REASON_STEP_MISMATCH, {
                'stored': manifest.get('step_desc'),
                'current': step_desc,
            }

        # the rendered description can't distinguish two functions of
        # the same script step type, so compare their source too
        if step_logic is not None and manifest.get('step_logic') != step_logic:
            return REASON_STEP_LOGIC_MISMATCH, {
                'stored': manifest.get('step_logic'),
                'current': step_logic,
            }

        return None

    def _check_shards(self, step_num):
        """Verify every stored shard exists and still matches the
        checksum recorded in ``shards.json``. Return a detail dict if
        anything is wrong, otherwise ``None``."""
        metadata = self.read_shard_metadata(step_num)
        if not metadata:
            # an empty shard list means the step never really finished
            return {'missing': [], 'changed': [],
                    'reason': 'no shards recorded'}

        missing = []
        changed = []

        for entry in metadata:
            name = entry.get('name')
            path = self.shard_path(step_num, name)

            if not exists(path):
                missing.append(name)
                continue

            try:
                fp = fingerprint_file(path)
            except (IOError, OSError):
                missing.append(name)
                continue

            if (fp['size'] != entry.get('size') or
                    fp['sha256'] != entry.get('sha256')):
                changed.append(name)

        if missing or changed:
            return {'missing': missing, 'changed': changed}

        return None

    def list_step_nums(self):
        """Step numbers that have a directory in the store (regardless of
        whether they are complete), in ascending order."""
        if not isdir(self._store_dir):
            return []

        step_nums = []
        for name in os.listdir(self._store_dir):
            if not name.startswith(STEP_DIR_PREFIX):
                continue
            try:
                step_nums.append(int(name[len(STEP_DIR_PREFIX):]))
            except ValueError:
                continue

        return sorted(step_nums)

    def inspect_step(self, step_num):
        """Read-only description of what is stored for *step_num*, for
        the ``mrjob reuse-check`` command. Never raises for missing or
        corrupt state; instead reports it through the returned dict.
        """
        result = {
            'step_num': step_num,
            'step_dir': self.step_dir(step_num),
            'has_manifest': False,
            'manifest': None,
            'shards': None,
            'shard_check': None,
            'complete': False,
        }

        manifest = self.read_manifest(step_num)
        if manifest is None:
            if self.has_manifest(step_num):
                result['manifest_error'] = REASON_MANIFEST_CORRUPT
            else:
                result['manifest_error'] = REASON_MANIFEST_MISSING
        else:
            result['has_manifest'] = True
            result['manifest'] = manifest
            result['manifest_error'] = None

        shards = self.read_shard_metadata(step_num)
        result['shards'] = shards

        if manifest is not None:
            result['shard_check'] = self._check_shards(step_num)
            result['complete'] = (
                bool(shards) and result['shard_check'] is None)

        return result


def load_mrjob_class(script_path):
    """Load the :py:class:`~mrjob.job.MRJob` subclass defined in
    *script_path*. Return ``None`` if no such class can be found.

    Used by runners that invoke a job script as a subprocess (e.g.
    ``-r local``) and by ``mrjob reuse-check``.
    """
    import runpy

    from mrjob.job import MRJob

    script_path = abspath(script_path)

    # job scripts conventionally end with `if __name__ == '__main__':`
    # guarding MRJob.run(); use a non-__main__ module name so importing
    # the script doesn't actually launch a job
    try:
        namespace = runpy.run_path(
            script_path, run_name='mrjob_reuse_probe')
    except Exception:
        return None

    for obj in namespace.values():
        if (isinstance(obj, type) and
                issubclass(obj, MRJob) and
                obj is not MRJob):
            return obj

    return None

"""
pyexcel_io.selector
~~~~~~~~~~~~~~~~~~~

Explainable plugin selection.

When more than one plugin is registered for the same file type,
picking one silently (e.g. by registration order) is confusing.
This module filters the candidates against the actual input and
the requested read/write operation, and reports why each skipped
candidate did not qualify.

Guarantees:

* capability probes on a stream are lossless: they use
  ``seekable()``, ``tell()`` and ``read(0)`` only, so the first
  line of the data is never consumed;
* the outcome does not depend on plugin installation or
  registration order: candidates are compared in a canonical
  sorted order;
* if the missing piece of information cannot be obtained through
  a lossless probe (e.g. the file type of a single-pass stream),
  :class:`InsufficientInformationError` is raised before any data
  is read, naming the information the caller has to supply.

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

from pyexcel_io import constants
from pyexcel_io.exceptions import (
    NoSupportingPluginFound,
    InsufficientInformationError,
)
from pyexcel_io.plugins import (
    NEW_READERS,
    NEW_WRITERS,
    OLD_READERS,
    OLD_WRITERS,
)

READ = "read"
WRITE = "write"

SOURCE_FILE = "file"
SOURCE_STREAM = "stream"
SOURCE_CONTENT = "content"

# pyexcel-io's v2 plugins tag their readers/writers with a location:
# "file" for file names, "memory" for streams, "content" for content
_LOCATION_BY_SOURCE = {
    SOURCE_FILE: "file",
    SOURCE_STREAM: "memory",
    SOURCE_CONTENT: "content",
}

_MESSAGE_MISSING_INPUT = (
    "Please provide one of file_name, file_stream or file_content "
    "so that a matching plugin can be selected."
)
_MESSAGE_MISSING_FILE_TYPE = (
    "Please provide file_type: the file type of a %s cannot be "
    "probed without consuming its data, and no data has been read yet."
)


class Candidate:
    """One registered plugin considered during selection

    :attr reasons: human readable explanations of why this candidate
        was skipped. Empty when the candidate is eligible.
    """

    def __init__(
        self,
        name,
        action,
        plugin_info=None,
        locations=None,
        package=None,
        manager=None,
    ):
        self.name = name
        self.action = action
        self.plugin_info = plugin_info
        # locations is None for v1 plugins, which accept any input
        self.locations = locations
        self.package = package
        self._manager = manager
        self.reasons = []

    @property
    def eligible(self):
        return len(self.reasons) == 0

    def reject(self, reason):
        self.reasons.append(reason)

    def load(self):
        """import and return the actual plugin class"""
        if self.plugin_info is None:
            raise NoSupportingPluginFound(
                "Plugin package '%s' is not installed" % self.package
            )
        return self._manager.dynamic_load_library(self.plugin_info)

    def __repr__(self):
        return "<Candidate %s action=%s eligible=%s>" % (
            self.name,
            self.action,
            self.eligible,
        )


class PluginSelection:
    """The outcome of :func:`select_plugin`

    :attr chosen: the winning :class:`Candidate`, or None when no
        candidate qualified
    :attr candidates: all considered candidates, eligible or not,
        in a deterministic order
    """

    def __init__(self, file_type, operation, source, candidates):
        self.file_type = file_type
        self.operation = operation
        self.source = source
        self.candidates = candidates

    @property
    def chosen(self):
        for candidate in self.candidates:
            if candidate.eligible:
                return candidate
        return None

    @property
    def skipped(self):
        return [candidate for candidate in self.candidates
                if not candidate.eligible]

    def get(self):
        """return the chosen plugin class

        :raises NoSupportingPluginFound: with the full explanation
            when no candidate qualified
        """
        chosen = self.chosen
        if chosen is None:
            raise NoSupportingPluginFound(self.explain())
        return chosen.load()

    def explain(self):
        """a human readable account of the selection"""
        lines = [
            "file type '%s', %s operation, input: %s"
            % (self.file_type, self.operation, self.source)
        ]
        if self.chosen:
            lines.append("chosen: %s" % self.chosen.name)
        else:
            lines.append("chosen: none")
        for candidate in self.skipped:
            lines.append("skipped: %s" % candidate.name)
            for reason in candidate.reasons:
                lines.append("  - %s" % reason)
        return "\n".join(lines)


def select_plugin(
    file_type=None,
    operation=READ,
    source=None,
    file_name=None,
    file_stream=None,
    file_content=None,
    library=None,
):
    """Select a plugin for the given input and operation, explainably

    :param file_type: the file format, e.g. 'csv'. Required for
        streams and content, because probing it would consume data.
    :param operation: 'read' or 'write'
    :param source: 'file', 'stream' or 'content'. Inferred from
        whichever of file_name/file_stream/file_content is given.
    :param library: restrict the selection to one plugin package,
        e.g. library='pyexcel-xls'
    :returns: a :class:`PluginSelection`
    :raises InsufficientInformationError: before any data is read,
        when a lossless probe cannot supply the missing information
    """
    if operation not in (READ, WRITE):
        raise ValueError("operation should be '%s' or '%s'" % (READ, WRITE))

    source = _infer_source(source, file_name, file_stream, file_content)
    file_type = _resolve_file_type(file_type, source, file_name)

    candidates = _gather_candidates(file_type)
    for candidate in candidates:
        _evaluate(
            candidate,
            operation=operation,
            source=source,
            file_stream=file_stream,
            library=library,
        )

    # canonical order: the outcome must not depend on the order in
    # which plugins happened to be installed or registered
    candidates.sort(key=lambda candidate: (candidate.action, candidate.name))
    return PluginSelection(file_type, operation, source, candidates)


def _infer_source(source, file_name, file_stream, file_content):
    inputs = {
        SOURCE_FILE: file_name,
        SOURCE_STREAM: file_stream,
        SOURCE_CONTENT: file_content,
    }
    if source is None:
        given = [kind for kind, value in inputs.items() if value is not None]
        if len(given) != 1:
            raise InsufficientInformationError(_MESSAGE_MISSING_INPUT)
        return given[0]
    if source not in inputs:
        raise ValueError(
            "source should be one of: %s" % ", ".join(sorted(inputs))
        )
    return source


def _resolve_file_type(file_type, source, file_name):
    if file_type is not None:
        return file_type.lower()
    if source == SOURCE_FILE and file_name is not None:
        try:
            return file_name.split(".")[-1].lower()
        except AttributeError:
            raise Exception(constants.MESSAGE_FILE_NAME_SHOULD_BE_STRING)
    raise InsufficientInformationError(_MESSAGE_MISSING_FILE_TYPE % source)


def _gather_candidates(file_type):
    candidates = []
    represented_packages = set()
    for action, old_manager, new_manager in (
        (READ, OLD_READERS, NEW_READERS),
        (WRITE, OLD_WRITERS, NEW_WRITERS),
    ):
        for info, locations in _iter_new_plugins(new_manager, file_type):
            candidates.append(
                Candidate(
                    name=info.absolute_import_path,
                    action=action,
                    plugin_info=info,
                    locations=locations,
                    manager=new_manager,
                )
            )
            represented_packages.add(_package_of(info))
        for info in old_manager.registry.get(file_type, []):
            candidates.append(
                Candidate(
                    name=info.absolute_import_path,
                    action=action,
                    plugin_info=info,
                    locations=None,
                    manager=old_manager,
                )
            )
            represented_packages.add(_package_of(info))
        for package in old_manager.known_plugins.get(file_type, []):
            if package not in represented_packages:
                candidates.append(
                    Candidate(
                        name=package,
                        action=action,
                        package=package,
                    )
                )
                candidates[-1].reject(
                    "package '%s' is not installed" % package
                )
    return candidates


def _iter_new_plugins(manager, file_type):
    """yield (plugin_info, locations) pairs registered for file_type"""
    locations_by_info = {}
    for tag, plugin_infos in manager.registry.items():
        location, separator, tagged_file_type = tag.rpartition("-")
        if not separator or tagged_file_type != file_type:
            continue
        for plugin_info in plugin_infos:
            entry = locations_by_info.setdefault(
                id(plugin_info), (plugin_info, set())
            )
            entry[1].add(location)
    return locations_by_info.values()


def _package_of(plugin_info):
    module_name = plugin_info.absolute_import_path.split(".")[0]
    return module_name.replace("_", "-")


def _evaluate(candidate, operation, source, file_stream, library):
    if candidate.plugin_info is None:
        # already rejected: not installed
        return

    if candidate.action != operation:
        candidate.reject(
            "supports '%s' only, but a '%s' operation was requested"
            % (candidate.action, operation)
        )

    if library:
        package = _package_of(candidate.plugin_info)
        if package != library:
            candidate.reject(
                "belongs to package '%s', not the requested library '%s'"
                % (package, library)
            )

    if candidate.locations is not None:
        location = _LOCATION_BY_SOURCE[source]
        if location not in candidate.locations:
            candidate.reject(
                "registered for %s input, not for a %s"
                % (_format_locations(candidate.locations), source)
            )

    if source == SOURCE_STREAM and file_stream is not None:
        _evaluate_stream(candidate, file_stream)


def _evaluate_stream(candidate, file_stream):
    expected_type = candidate.plugin_info.stream_type
    actual_type = _probe_stream_type(file_stream)
    if (
        expected_type in ("text", "binary")
        and actual_type is not None
        and expected_type != actual_type
    ):
        candidate.reject(
            "expects a %s stream, but the given stream is %s"
            % (expected_type, actual_type)
        )

    if _requires_seekable_stream(candidate) and not _is_seekable(file_stream):
        candidate.reject(
            "requires a seekable stream, but the given stream "
            "is single-pass and cannot be rewound"
        )


def _requires_seekable_stream(candidate):
    flag = candidate.plugin_info.properties.get("requires_seekable_stream")
    if flag is None:
        try:
            plugin_class = candidate.load()
        except Exception as error:
            candidate.reject("cannot be loaded: %s" % error)
            return False
        flag = getattr(plugin_class, "requires_seekable_stream", False)
    return bool(flag)


def _is_seekable(stream):
    """lossless seekability probe: never moves the stream position"""
    seekable = getattr(stream, "seekable", None)
    if callable(seekable):
        try:
            return bool(seekable())
        except Exception:
            pass
    try:
        stream.tell()
        return True
    except Exception:
        return False


def _probe_stream_type(stream):
    """lossless text/binary probe: read(0) consumes nothing"""
    read = getattr(stream, "read", None)
    if read is None:
        return None
    try:
        probe = stream.read(0)
    except Exception:
        return None
    if isinstance(probe, bytes):
        return "binary"
    if isinstance(probe, str):
        return "text"
    return None


def _format_locations(locations):
    names = sorted(
        source
        for source, location in _LOCATION_BY_SOURCE.items()
        if location in locations
    )
    if len(names) == 1:
        return "'%s' only" % names[0]
    return " or ".join("'%s'" % name for name in names)

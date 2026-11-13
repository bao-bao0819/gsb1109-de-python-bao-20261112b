# -* coding: utf-8 -*-
#
# License: MIT (see LICENSE file provided)
# vim: set expandtab tabstop=4 shiftwidth=4 softtabstop=4:

"""
**polib** allows you to manipulate, create, modify gettext files (pot, po and
mo files).  You can load existing files, iterate through it's entries, add,
modify entries, comments or metadata, etc. or create new po files from scratch.

**polib** provides a simple and pythonic API via the :func:`~polib.pofile` and
:func:`~polib.mofile` convenience functions.
"""

import array
import codecs
import json
import os
import re
import struct
import sys
import textwrap
import io


__author__ = 'David Jean Louis <izimobil@gmail.com>'
__version__ = '1.2.0'
__all__ = ['pofile', 'POFile', 'POEntry', 'mofile', 'MOFile', 'MOEntry',
           'default_encoding', 'escape', 'unescape', 'detect_encoding',
           'PluralRule', 'PluralRuleError', 'TranslationCatalog',
           'TranslationResolution', 'TranslationResolver', 'SkippedLayer',
           'CatalogSet', 'load_catalog_config', 'resolve_translation',
           'resolve_tickets', ]


# the default encoding to use when encoding cannot be detected
default_encoding = 'utf-8'

# python 2/3 compatibility helpers {{{


if sys.version_info < (3,):
    PY3 = False
    text_type = unicode

    def b(s):
        return s

    def u(s):
        return unicode(s, "unicode_escape")

else:
    PY3 = True
    text_type = str
    long = int

    def b(s):
        return s.encode("latin-1")

    def u(s):
        return s
# }}}
# _pofile_or_mofile {{{


def _pofile_or_mofile(f, type, **kwargs):
    """
    Internal function used by :func:`polib.pofile` and :func:`polib.mofile` to
    honor the DRY concept.
    """
    # get the file encoding
    enc = kwargs.get('encoding')
    if enc is None:
        enc = detect_encoding(f, type == 'mofile')

    # parse the file
    kls = type == 'pofile' and _POFileParser or _MOFileParser
    parser = kls(
        f,
        encoding=enc,
        check_for_duplicates=kwargs.get('check_for_duplicates', False),
        klass=kwargs.get('klass')
    )
    instance = parser.parse()
    instance.wrapwidth = kwargs.get('wrapwidth', 78)
    return instance
# }}}
# _is_file {{{


def _is_file(filename_or_contents):
    """
    Safely returns the value of os.path.exists(filename_or_contents).

    Arguments:

    ``filename_or_contents``
        either a filename, or a string holding the contents of some file.
        In the latter case, this function will always return False.
    """
    try:
        return os.path.isfile(filename_or_contents)
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
# }}}
# function pofile() {{{


def pofile(pofile, **kwargs):
    """
    Convenience function that parses the po or pot file ``pofile`` and returns
    a :class:`~polib.POFile` instance.

    Arguments:

    ``pofile``
        string, full or relative path to the po/pot file or its content (data).

    ``wrapwidth``
        integer, the wrap width, only useful when the ``-w`` option was passed
        to xgettext (optional, default: ``78``).

    ``encoding``
        string, the encoding to use (e.g. "utf-8") (default: ``None``, the
        encoding will be auto-detected).

    ``check_for_duplicates``
        whether to check for duplicate entries when adding entries to the
        file (optional, default: ``False``).

    ``klass``
        class which is used to instantiate the return value (optional,
        default: ``None``, the return value with be a :class:`~polib.POFile`
        instance).
    """
    return _pofile_or_mofile(pofile, 'pofile', **kwargs)
# }}}
# function mofile() {{{


def mofile(mofile, **kwargs):
    """
    Convenience function that parses the mo file ``mofile`` and returns a
    :class:`~polib.MOFile` instance.

    Arguments:

    ``mofile``
        string, full or relative path to the mo file or its content (string
        or bytes).

    ``wrapwidth``
        integer, the wrap width, only useful when the ``-w`` option was passed
        to xgettext to generate the po file that was used to format the mo file
        (optional, default: ``78``).

    ``encoding``
        string, the encoding to use (e.g. "utf-8") (default: ``None``, the
        encoding will be auto-detected).

    ``check_for_duplicates``
        whether to check for duplicate entries when adding entries to the
        file (optional, default: ``False``).

    ``klass``
        class which is used to instantiate the return value (optional,
        default: ``None``, the return value with be a :class:`~polib.POFile`
        instance).
    """
    return _pofile_or_mofile(mofile, 'mofile', **kwargs)
# }}}
# function detect_encoding() {{{


def detect_encoding(file, binary_mode=False):
    """
    Try to detect the encoding used by the ``file``. The ``file`` argument can
    be a PO or MO file path or a string containing the contents of the file.
    If the encoding cannot be detected, the function will return the value of
    ``default_encoding``.

    Arguments:

    ``file``
        string, full or relative path to the po/mo file or its content.

    ``binary_mode``
        boolean, set this to True if ``file`` is a mo file.
    """
    PATTERN = r'"?Content-Type:.+? charset=([\w_\-:\.]+)'
    rxt = re.compile(u(PATTERN))
    rxb = re.compile(b(PATTERN))

    def charset_exists(charset):
        """Check whether ``charset`` is valid or not."""
        try:
            codecs.lookup(charset)
        except LookupError:
            return False
        return True

    if not _is_file(file):
        try:
            match = rxt.search(file)
        except TypeError:
            match = rxb.search(file)
        if match:
            enc = match.group(1).strip()
            if not isinstance(enc, text_type):
                enc = enc.decode('utf-8')
            if charset_exists(enc):
                return enc
    else:
        # For PY3, always treat as binary
        if binary_mode or PY3:
            mode = 'rb'
            rx = rxb
        else:
            mode = 'r'
            rx = rxt
        with open(file, mode) as f:
            for line in f.readlines():
                match = rx.search(line)
                if match:
                    f.close()
                    enc = match.group(1).strip()
                    if not isinstance(enc, text_type):
                        enc = enc.decode('utf-8')
                    if charset_exists(enc):
                        return enc
    return default_encoding
# }}}
# function escape() {{{


def escape(st):
    """
    Escapes the characters ``\\\\``, ``\\t``, ``\\n``, ``\\r``, ``\\v``,
    ``\\b``, ``\\f``, ``\\a`` and ``"`` in the given string ``st`` and returns it.
    """
    return st.replace('\\', r'\\')\
             .replace('\t', r'\t')\
             .replace('\r', r'\r')\
             .replace('\n', r'\n')\
             .replace('\v', r'\v')\
             .replace('\b', r'\b')\
             .replace('\f', r'\f')\
             .replace('\a', r'\a')\
             .replace('\"', r'\"')
# }}}
# function unescape() {{{


def unescape(st):
    """
    Unescapes the characters ``\\\\``, ``\\t``, ``\\n``, ``\\r``, ``\\v``,
    ``\\b``, ``\\f``, ``\\a`` and ``"`` in the given string ``st`` and returns it.
    """
    def unescape_repl(m):
        m = m.group(1)
        if m == 'n':
            return '\n'
        if m == 't':
            return '\t'
        if m == 'r':
            return '\r'
        if m == 'v':
            return '\v'
        if m == 'b':
            return '\b'
        if m == 'f':
            return '\f'
        if m == 'a':
            return '\a'
        if m == '\\':
            return '\\'
        return m  # handles escaped double quote
    return re.sub(r'\\(\\|n|t|r|v|b|f|a|")', unescape_repl, st)
# }}}
# function natural_sort() {{{


def natural_sort(lst):
    """
    Sort naturally the given list.
    Credits: http://stackoverflow.com/a/4836734
    """
    def convert(text):
        return int(text) if text.isdigit() else text.lower()

    def alphanum_key(key):
        return [convert(c) for c in re.split('([0-9]+)', key)]

    return sorted(lst, key=alphanum_key)

# }}}
# class _BaseFile {{{


class _BaseFile(list):
    """
    Common base class for the :class:`~polib.POFile` and :class:`~polib.MOFile`
    classes. This class should **not** be instantiated directly.
    """

    def __init__(self, *args, **kwargs):
        """
        Constructor, accepts the following keyword arguments:

        ``pofile``
            string, the path to the po or mo file, or its content as a string.

        ``wrapwidth``
            integer, the wrap width, only useful when the ``-w`` option was
            passed to xgettext (optional, default: ``78``).

        ``encoding``
            string, the encoding to use, defaults to ``default_encoding``
            global variable (optional).

        ``check_for_duplicates``
            whether to check for duplicate entries when adding entries to the
            file, (optional, default: ``False``).
        """
        list.__init__(self)
        # the opened file handle
        pofile = kwargs.get('pofile', None)
        if pofile and _is_file(pofile):
            self.fpath = pofile
        else:
            self.fpath = kwargs.get('fpath')
        # the width at which lines should be wrapped
        self.wrapwidth = kwargs.get('wrapwidth', 78)
        # the file encoding
        self.encoding = kwargs.get('encoding', default_encoding)
        # whether to check for duplicate entries or not
        self.check_for_duplicates = kwargs.get('check_for_duplicates', False)
        # header
        self.header = ''
        # both po and mo files have metadata
        self.metadata = {}
        self.metadata_is_fuzzy = 0

    def __unicode__(self):
        """
        Returns the unicode representation of the file.
        """
        ret = []
        entries = [self.metadata_as_entry()] + \
                  [e for e in self if not e.obsolete]
        for entry in entries:
            ret.append(entry.__unicode__(self.wrapwidth))
        for entry in self.obsolete_entries():
            ret.append(entry.__unicode__(self.wrapwidth))
        ret = u('\n').join(ret)
        return ret

    if PY3:
        def __str__(self):
            return self.__unicode__()
    else:
        def __str__(self):
            """
            Returns the string representation of the file.
            """
            return unicode(self).encode(self.encoding)

    def __contains__(self, entry):
        """
        Overridden ``list`` method to implement the membership test (in and
        not in).
        The method considers that an entry is in the file if it finds an entry
        that has the same msgid (the test is **case sensitive**) and the same
        msgctxt (or none for both entries).

        Argument:

        ``entry``
            an instance of :class:`~polib._BaseEntry`.
        """
        return self.find(entry.msgid, by='msgid', msgctxt=entry.msgctxt) \
            is not None

    def __eq__(self, other):
        return str(self) == str(other)

    def append(self, entry):
        """
        Overridden method to check for duplicates entries, if a user tries to
        add an entry that is already in the file, the method will raise a
        ``ValueError`` exception.

        Argument:

        ``entry``
            an instance of :class:`~polib._BaseEntry`.
        """
        # check_for_duplicates may not be defined (yet) when unpickling.
        # But if pickling, we never want to check for duplicates anyway.
        if getattr(self, 'check_for_duplicates', False) and entry in self:
            raise ValueError('Entry "%s" already exists' % entry.msgid)
        super(_BaseFile, self).append(entry)

    def insert(self, index, entry):
        """
        Overridden method to check for duplicates entries, if a user tries to
        add an entry that is already in the file, the method will raise a
        ``ValueError`` exception.

        Arguments:

        ``index``
            index at which the entry should be inserted.

        ``entry``
            an instance of :class:`~polib._BaseEntry`.
        """
        if self.check_for_duplicates and entry in self:
            raise ValueError('Entry "%s" already exists' % entry.msgid)
        super(_BaseFile, self).insert(index, entry)

    def metadata_as_entry(self):
        """
        Returns the file metadata as a :class:`~polib.POFile` instance.
        """
        e = POEntry(msgid='')
        mdata = self.ordered_metadata()
        if mdata:
            strs = []
            for name, value in mdata:
                # Strip whitespace off each line in a multi-line entry
                strs.append('%s: %s' % (name, value))
            e.msgstr = '\n'.join(strs) + '\n'
        if self.metadata_is_fuzzy:
            e.flags.append('fuzzy')
        return e

    def save(self, fpath=None, repr_method='__unicode__', newline=None):
        """
        Saves the po file to ``fpath``.
        If it is an existing file and no ``fpath`` is provided, then the
        existing file is rewritten with the modified data.

        Keyword arguments:

        ``fpath``
            string, full or relative path to the file.

        ``repr_method``
            string, the method to use for output.

        ``newline``
            string, controls how universal newlines works
        """
        if self.fpath is None and fpath is None:
            raise IOError('You must provide a file path to save() method')
        contents = getattr(self, repr_method)()
        if fpath is None:
            fpath = self.fpath
        if repr_method == 'to_binary':
            with open(fpath, 'wb') as fhandle:
                fhandle.write(contents)
        else:
            with io.open(
                fpath,
                'w',
                encoding=self.encoding,
                newline=newline
            ) as fhandle:
                if not isinstance(contents, text_type):
                    contents = contents.decode(self.encoding)
                fhandle.write(contents)

        # set the file path if not set
        if self.fpath is None and fpath:
            self.fpath = fpath

    def find(self, st, by='msgid', include_obsolete_entries=False,
             msgctxt=False):
        """
        Find the entry which msgid (or property identified by the ``by``
        argument) matches the string ``st``.

        Keyword arguments:

        ``st``
            string, the string to search for.

        ``by``
            string, the property to use for comparison (default: ``msgid``).

        ``include_obsolete_entries``
            boolean, whether to also search in entries that are obsolete.

        ``msgctxt``
            string, allows specifying a specific message context for the
            search.
        """
        if include_obsolete_entries:
            entries = self[:]
        else:
            entries = [e for e in self if not e.obsolete]
        matches = []
        for e in entries:
            if getattr(e, by) == st:
                if msgctxt is not False and e.msgctxt != msgctxt:
                    continue
                matches.append(e)
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            if not msgctxt:
                # find the entry with no msgctx
                e = None
                for m in matches:
                    if not m.msgctxt:
                        e = m
                if e:
                    return e
                # fallback to the first entry found
                return matches[0]
        return None

    def ordered_metadata(self):
        """
        Convenience method that returns an ordered version of the metadata
        dictionary. The return value is list of tuples (metadata name,
        metadata_value).
        """
        # copy the dict first
        metadata = self.metadata.copy()
        data_order = [
            'Project-Id-Version',
            'Report-Msgid-Bugs-To',
            'POT-Creation-Date',
            'PO-Revision-Date',
            'Last-Translator',
            'Language-Team',
            'Language',
            'MIME-Version',
            'Content-Type',
            'Content-Transfer-Encoding',
            'Plural-Forms'
        ]
        ordered_data = []
        for data in data_order:
            try:
                value = metadata.pop(data)
                ordered_data.append((data, value))
            except KeyError:
                pass
        # the rest of the metadata will be alphabetically ordered since there
        # are no specs for this AFAIK
        for data in natural_sort(metadata.keys()):
            value = metadata[data]
            ordered_data.append((data, value))
        return ordered_data

    def to_binary(self):
        """
        Return the binary representation of the file.
        """
        offsets = []
        entries = self.translated_entries()

        # the keys are sorted in the .mo file
        def cmp(_self, other):
            # msgfmt compares entries with msgctxt if it exists
            self_msgid = _self.msgctxt and _self.msgctxt or _self.msgid
            other_msgid = other.msgctxt and other.msgctxt or other.msgid
            if self_msgid > other_msgid:
                return 1
            elif self_msgid < other_msgid:
                return -1
            else:
                return 0
        # add metadata entry
        entries.sort(key=lambda o: o.msgid_with_context.encode('utf-8'))
        mentry = self.metadata_as_entry()
        entries = [mentry] + entries
        entries_len = len(entries)
        ids, strs = b(''), b('')
        for e in entries:
            # For each string, we need size and file offset.  Each string is
            # NUL terminated; the NUL does not count into the size.
            msgid = b('')
            if e.msgctxt:
                # Contexts are stored by storing the concatenation of the
                # context, a <EOT> byte, and the original string
                msgid = self._encode(e.msgctxt + '\4')
            if e.msgid_plural:
                msgstr = []
                for index in sorted(e.msgstr_plural.keys()):
                    msgstr.append(e.msgstr_plural[index])
                msgid += self._encode(e.msgid + '\0' + e.msgid_plural)
                msgstr = self._encode('\0'.join(msgstr))
            else:
                msgid += self._encode(e.msgid)
                msgstr = self._encode(e.msgstr)
            offsets.append((len(ids), len(msgid), len(strs), len(msgstr)))
            ids += msgid + b('\0')
            strs += msgstr + b('\0')

        # The header is 7 32-bit unsigned integers.
        keystart = 7 * 4 + 16 * entries_len
        # and the values start after the keys
        valuestart = keystart + len(ids)
        koffsets = []
        voffsets = []
        # The string table first has the list of keys, then the list of values.
        # Each entry has first the size of the string, then the file offset.
        for o1, l1, o2, l2 in offsets:
            koffsets += [l1, o1 + keystart]
            voffsets += [l2, o2 + valuestart]
        offsets = koffsets + voffsets

        output = struct.pack(
            "Iiiiiii",
            # Magic number
            MOFile.MAGIC,
            # Version
            0,
            # number of entries
            entries_len,
            # start of key index
            7 * 4,
            # start of value index
            7 * 4 + entries_len * 8,
            # size and offset of hash table, we don't use hash tables
            0, keystart

        )
        if PY3 and sys.version_info.minor > 1:  # python 3.2 or superior
            output += array.array("i", offsets).tobytes()
        else:
            output += array.array("i", offsets).tostring()
        output += ids
        output += strs
        return output

    def _encode(self, mixed):
        """
        Encodes the given ``mixed`` argument with the file encoding if and
        only if it's an unicode string and returns the encoded string.
        """
        if isinstance(mixed, text_type):
            mixed = mixed.encode(self.encoding)
        return mixed
# }}}
# class POFile {{{


class POFile(_BaseFile):
    """
    Po (or Pot) file reader/writer.
    This class inherits the :class:`~polib._BaseFile` class and, by extension,
    the python ``list`` type.
    """

    def __unicode__(self):
        """
        Returns the unicode representation of the po file.
        """
        ret, headers = '', self.header.split('\n')
        for header in headers:
            if not len(header):
                ret += "#\n"
            elif header[:1] in [',', ':']:
                ret += '#%s\n' % header
            else:
                ret += '# %s\n' % header

        if not isinstance(ret, text_type):
            ret = ret.decode(self.encoding)

        return ret + _BaseFile.__unicode__(self)

    def save_as_mofile(self, fpath):
        """
        Saves the binary representation of the file to given ``fpath``.

        Keyword argument:

        ``fpath``
            string, full or relative path to the mo file.
        """
        _BaseFile.save(self, fpath, 'to_binary')

    def percent_translated(self):
        """
        Convenience method that returns the percentage of translated
        messages.
        """
        total = len([e for e in self if not e.obsolete])
        if total == 0:
            return 100
        translated = len(self.translated_entries())
        return int(translated * 100 / float(total))

    def translated_entries(self):
        """
        Convenience method that returns the list of translated entries.
        """
        return [e for e in self if e.translated()]

    def untranslated_entries(self):
        """
        Convenience method that returns the list of untranslated entries.
        """
        return [e for e in self if not e.translated() and not e.obsolete
                and not e.fuzzy]

    def fuzzy_entries(self):
        """
        Convenience method that returns the list of fuzzy entries.
        """
        return [e for e in self if e.fuzzy and not e.obsolete]

    def obsolete_entries(self):
        """
        Convenience method that returns the list of obsolete entries.
        """
        return [e for e in self if e.obsolete]

    def merge(self, refpot):
        """
        Convenience method that merges the current pofile with the pot file
        provided. It behaves exactly as the gettext msgmerge utility:

        * comments of this file will be preserved, but extracted comments and
          occurrences will be discarded;
        * any translations or comments in the file will be discarded, however,
          dot comments and file positions will be preserved;
        * the fuzzy flags are preserved.

        Keyword argument:

        ``refpot``
            object POFile, the reference catalog.
        """
        # Store entries in dict/set for faster access
        self_entries = dict(
            (entry.msgid_with_context, entry) for entry in self
        )
        refpot_msgids = set(entry.msgid_with_context for entry in refpot)
        # Merge entries that are in the refpot
        for entry in refpot:
            e = self_entries.get(entry.msgid_with_context)
            if e is None:
                e = POEntry()
                self.append(e)
            e.merge(entry)
        # ok, now we must "obsolete" entries that are not in the refpot anymore
        for entry in self:
            if entry.msgid_with_context not in refpot_msgids:
                entry.obsolete = True
# }}}
# class MOFile {{{


class MOFile(_BaseFile):
    """
    Mo file reader/writer.
    This class inherits the :class:`~polib._BaseFile` class and, by
    extension, the python ``list`` type.
    """
    MAGIC = 0x950412de
    MAGIC_SWAPPED = 0xde120495

    def __init__(self, *args, **kwargs):
        """
        Constructor, accepts all keywords arguments accepted by
        :class:`~polib._BaseFile` class.
        """
        _BaseFile.__init__(self, *args, **kwargs)
        self.magic_number = None
        self.version = 0

    def save_as_pofile(self, fpath):
        """
        Saves the mofile as a pofile to ``fpath``.

        Keyword argument:

        ``fpath``
            string, full or relative path to the file.
        """
        _BaseFile.save(self, fpath)

    def save(self, fpath=None):
        """
        Saves the mofile to ``fpath``.

        Keyword argument:

        ``fpath``
            string, full or relative path to the file.
        """
        _BaseFile.save(self, fpath, 'to_binary')

    def percent_translated(self):
        """
        Convenience method to keep the same interface with POFile instances.
        """
        return 100

    def translated_entries(self):
        """
        Convenience method to keep the same interface with POFile instances.
        """
        return self

    def untranslated_entries(self):
        """
        Convenience method to keep the same interface with POFile instances.
        """
        return []

    def fuzzy_entries(self):
        """
        Convenience method to keep the same interface with POFile instances.
        """
        return []

    def obsolete_entries(self):
        """
        Convenience method to keep the same interface with POFile instances.
        """
        return []
# }}}
# class _BaseEntry {{{


class _BaseEntry(object):
    """
    Base class for :class:`~polib.POEntry` and :class:`~polib.MOEntry` classes.
    This class should **not** be instantiated directly.
    """

    def __init__(self, *args, **kwargs):
        """
        Constructor, accepts the following keyword arguments:

        ``msgid``
            string, the entry msgid.

        ``msgstr``
            string, the entry msgstr.

        ``msgid_plural``
            string, the entry msgid_plural.

        ``msgstr_plural``
            dict, the entry msgstr_plural lines.

        ``msgctxt``
            string, the entry context (msgctxt).

        ``obsolete``
            bool, whether the entry is "obsolete" or not.

        ``encoding``
            string, the encoding to use, defaults to ``default_encoding``
            global variable (optional).
        """
        self.msgid = kwargs.get('msgid', '')
        self.msgstr = kwargs.get('msgstr', '')
        self.msgid_plural = kwargs.get('msgid_plural', '')
        self.msgstr_plural = kwargs.get('msgstr_plural', {})
        self.msgctxt = kwargs.get('msgctxt', None)
        self.obsolete = kwargs.get('obsolete', False)
        self.encoding = kwargs.get('encoding', default_encoding)

    def __unicode__(self, wrapwidth=78):
        """
        Returns the unicode representation of the entry.
        """
        if self.obsolete:
            delflag = '#~ '
        else:
            delflag = ''
        ret = []
        # write the msgctxt if any
        if self.msgctxt is not None:
            ret += self._str_field("msgctxt", delflag, "", self.msgctxt,
                                   wrapwidth)
        # write the msgid
        ret += self._str_field("msgid", delflag, "", self.msgid, wrapwidth)
        # write the msgid_plural if any
        if self.msgid_plural:
            ret += self._str_field("msgid_plural", delflag, "",
                                   self.msgid_plural, wrapwidth)
        if self.msgstr_plural:
            # write the msgstr_plural if any
            msgstrs = self.msgstr_plural
            keys = list(msgstrs)
            keys.sort()
            for index in keys:
                msgstr = msgstrs[index]
                plural_index = '[%s]' % index
                ret += self._str_field("msgstr", delflag, plural_index, msgstr,
                                       wrapwidth)
        else:
            # otherwise write the msgstr
            ret += self._str_field("msgstr", delflag, "", self.msgstr,
                                   wrapwidth)
        ret.append('')
        ret = u('\n').join(ret)
        return ret

    if PY3:
        def __str__(self):
            return self.__unicode__()
    else:
        def __str__(self):
            """
            Returns the string representation of the entry.
            """
            return unicode(self).encode(self.encoding)

    def __eq__(self, other):
        return str(self) == str(other)

    def _str_field(self, fieldname, delflag, plural_index, field,
                   wrapwidth=78):
        lines = field.splitlines(True)
        if len(lines) > 1:
            lines = [''] + lines  # start with initial empty line
        else:
            escaped_field = escape(field)
            specialchars_count = 0
            for c in ['\\', '\n', '\r', '\t', '\v', '\b', '\f', '\a', '"']:
                specialchars_count += field.count(c)
            # comparison must take into account fieldname length + one space
            # + 2 quotes (eg. msgid "<string>")
            flength = len(fieldname) + 3
            if plural_index:
                flength += len(plural_index)
            real_wrapwidth = wrapwidth - flength + specialchars_count
            if wrapwidth > 0 and len(field) > real_wrapwidth:
                # Wrap the line but take field name into account
                lines = [''] + [unescape(item) for item in textwrap.wrap(
                    escaped_field,
                    wrapwidth - 2,  # 2 for quotes ""
                    drop_whitespace=False,
                    break_long_words=False
                )]
            else:
                lines = [field]
        if fieldname.startswith('previous_'):
            # quick and dirty trick to get the real field name
            fieldname = fieldname[9:]

        ret = ['%s%s%s "%s"' % (delflag, fieldname, plural_index,
                                escape(lines.pop(0)))]
        for line in lines:
            ret.append('%s"%s"' % (delflag, escape(line)))
        return ret

    @property
    def msgid_with_context(self):
        if self.msgctxt:
            return '%s%s%s' % (self.msgctxt, "\x04", self.msgid)
        return self.msgid
# }}}
# class POEntry {{{


class POEntry(_BaseEntry):
    """
    Represents a po file entry.
    """

    def __init__(self, *args, **kwargs):
        """
        Constructor, accepts the following keyword arguments:

        ``comment``
            string, the entry comment.

        ``tcomment``
            string, the entry translator comment.

        ``occurrences``
            list, the entry occurrences.

        ``flags``
            list, the entry flags.

        ``previous_msgctxt``
            string, the entry previous context.

        ``previous_msgid``
            string, the entry previous msgid.

        ``previous_msgid_plural``
            string, the entry previous msgid_plural.

        ``linenum``
            integer, the line number of the entry
        """
        _BaseEntry.__init__(self, *args, **kwargs)
        self.comment = kwargs.get('comment', '')
        self.tcomment = kwargs.get('tcomment', '')
        self.occurrences = kwargs.get('occurrences', [])
        self.flags = kwargs.get('flags', [])
        self.previous_msgctxt = kwargs.get('previous_msgctxt', None)
        self.previous_msgid = kwargs.get('previous_msgid', None)
        self.previous_msgid_plural = kwargs.get('previous_msgid_plural', None)
        self.linenum = kwargs.get('linenum', None)

    def __unicode__(self, wrapwidth=78):
        """
        Returns the unicode representation of the entry.
        """
        ret = []
        # comments first, if any (with text wrapping as xgettext does)
        if self.obsolete:
            comments = [('tcomment', '# ')]
        else:
            comments = [('tcomment', '# '), ('comment', '#. ')]
        for c in comments:
            val = getattr(self, c[0])
            if val:
                for comment in val.split('\n'):
                    if wrapwidth > 0 and len(comment) + len(c[1]) > wrapwidth:
                        ret += textwrap.wrap(
                            comment,
                            wrapwidth,
                            initial_indent=c[1],
                            subsequent_indent=c[1],
                            break_long_words=False
                        )
                    else:
                        ret.append('%s%s' % (c[1], comment))

        # occurrences (with text wrapping as xgettext does)
        if not self.obsolete and self.occurrences:
            filelist = []
            for fpath, lineno in self.occurrences:
                if lineno:
                    filelist.append('%s:%s' % (fpath, lineno))
                else:
                    filelist.append(fpath)
            filestr = ' '.join(filelist)
            if wrapwidth > 0 and len(filestr) + 3 > wrapwidth:
                # textwrap split words that contain hyphen, this is not
                # what we want for filenames, so the dirty hack is to
                # temporally replace hyphens with a char that a file cannot
                # contain, like "*"
                ret += [line.replace('*', '-') for line in textwrap.wrap(
                    filestr.replace('-', '*'),
                    wrapwidth,
                    initial_indent='#: ',
                    subsequent_indent='#: ',
                    break_long_words=False
                )]
            else:
                ret.append('#: ' + filestr)

        # flags (TODO: wrapping ?)
        if self.flags:
            ret.append('#, %s' % ', '.join(self.flags))

        # previous context and previous msgid/msgid_plural
        fields = ['previous_msgctxt', 'previous_msgid',
                  'previous_msgid_plural']
        if self.obsolete:
            prefix = "#~| "
        else:
            prefix = "#| "
        for f in fields:
            val = getattr(self, f)
            if val is not None:
                ret += self._str_field(f, prefix, "", val, wrapwidth)

        ret.append(_BaseEntry.__unicode__(self, wrapwidth))
        ret = u('\n').join(ret)
        return ret

    def __cmp__(self, other):
        """
        Called by comparison operations if rich comparison is not defined.
        """
        # First: Obsolete test
        if self.obsolete != other.obsolete:
            if self.obsolete:
                return -1
            else:
                return 1
        # Work on a copy to protect original
        occ1 = sorted(self.occurrences[:])
        occ2 = sorted(other.occurrences[:])
        if occ1 > occ2:
            return 1
        if occ1 < occ2:
            return -1
        # Compare context
        msgctxt = self.msgctxt or '0'
        othermsgctxt = other.msgctxt or '0'
        if msgctxt > othermsgctxt:
            return 1
        elif msgctxt < othermsgctxt:
            return -1
        # Compare msgid_plural
        msgid_plural = self.msgid_plural or '0'
        othermsgid_plural = other.msgid_plural or '0'
        if msgid_plural > othermsgid_plural:
            return 1
        elif msgid_plural < othermsgid_plural:
            return -1
        # Compare msgstr_plural
        if self.msgstr_plural and isinstance(self.msgstr_plural, dict):
            msgstr_plural = list(self.msgstr_plural.values())
        else:
            msgstr_plural = []
        if other.msgstr_plural and isinstance(other.msgstr_plural, dict):
            othermsgstr_plural = list(other.msgstr_plural.values())
        else:
            othermsgstr_plural = []
        if msgstr_plural > othermsgstr_plural:
            return 1
        elif msgstr_plural < othermsgstr_plural:
            return -1
        # Compare msgid
        if self.msgid > other.msgid:
            return 1
        elif self.msgid < other.msgid:
            return -1
        # Compare msgstr
        if self.msgstr > other.msgstr:
            return 1
        elif self.msgstr < other.msgstr:
            return -1
        return 0

    def __gt__(self, other):
        return self.__cmp__(other) > 0

    def __lt__(self, other):
        return self.__cmp__(other) < 0

    def __ge__(self, other):
        return self.__cmp__(other) >= 0

    def __le__(self, other):
        return self.__cmp__(other) <= 0

    def __eq__(self, other):
        return self.__cmp__(other) == 0

    def __ne__(self, other):
        return self.__cmp__(other) != 0

    def translated(self):
        """
        Returns ``True`` if the entry has been translated or ``False``
        otherwise.
        """
        if self.obsolete or self.fuzzy:
            return False
        if self.msgstr != '':
            return True
        if self.msgstr_plural:
            for pos in self.msgstr_plural:
                if self.msgstr_plural[pos] == '':
                    return False
            return True
        return False

    def merge(self, other):
        """
        Merge the current entry with the given pot entry.
        """
        self.msgid = other.msgid
        self.msgctxt = other.msgctxt
        self.occurrences = other.occurrences
        self.comment = other.comment
        fuzzy = self.fuzzy
        self.flags = other.flags[:]  # clone flags
        if fuzzy:
            self.flags.append('fuzzy')
        self.msgid_plural = other.msgid_plural
        self.obsolete = other.obsolete
        self.previous_msgctxt = other.previous_msgctxt
        self.previous_msgid = other.previous_msgid
        self.previous_msgid_plural = other.previous_msgid_plural
        if other.msgstr_plural:
            for pos in other.msgstr_plural:
                try:
                    # keep existing translation at pos if any
                    self.msgstr_plural[pos]
                except KeyError:
                    self.msgstr_plural[pos] = ''

    @property
    def fuzzy(self):
        return 'fuzzy' in self.flags

    @fuzzy.setter
    def fuzzy(self, value):
        if value and not self.fuzzy:
            self.flags.insert(0, 'fuzzy')
        elif not value and self.fuzzy:
            self.flags.remove('fuzzy')

    def __hash__(self):
        return hash((self.msgid, self.msgstr))
# }}}
# class MOEntry {{{


class MOEntry(_BaseEntry):
    """
    Represents a mo file entry.
    """
    def __init__(self, *args, **kwargs):
        """
        Constructor, accepts the following keyword arguments,
        for consistency with :class:`~polib.POEntry`:

        ``comment``
        ``tcomment``
        ``occurrences``
        ``flags``
        ``previous_msgctxt``
        ``previous_msgid``
        ``previous_msgid_plural``

        Note: even though these keyword arguments are accepted,
        they hold no real meaning in the context of MO files
        and are simply ignored.
        """
        _BaseEntry.__init__(self, *args, **kwargs)
        self.comment = ''
        self.tcomment = ''
        self.occurrences = []
        self.flags = []
        self.previous_msgctxt = None
        self.previous_msgid = None
        self.previous_msgid_plural = None

    def __hash__(self):
        return hash((self.msgid, self.msgstr))

# }}}
# class _POFileParser {{{


class _POFileParser(object):
    """
    A finite state machine to efficiently and correctly parse po
    file format.
    """

    def __init__(self, pofile, *args, **kwargs):
        """
        Constructor.

        Keyword arguments:

        ``pofile``
            string, path to the po file or its content

        ``encoding``
            string, the encoding to use, defaults to ``default_encoding``
            global variable (optional).

        ``check_for_duplicates``
            whether to check for duplicate entries when adding entries to the
            file (optional, default: ``False``).
        """
        enc = kwargs.get('encoding', default_encoding)
        if _is_file(pofile):
            try:
                self.fhandle = io.open(pofile, 'rt', encoding=enc)
            except LookupError:
                enc = default_encoding
                self.fhandle = io.open(pofile, 'rt', encoding=enc)
        else:
            self.fhandle = pofile.splitlines()

        klass = kwargs.get('klass')
        if klass is None:
            klass = POFile
        self.instance = klass(
            pofile=pofile,
            encoding=enc,
            check_for_duplicates=kwargs.get('check_for_duplicates', False)
        )
        self.transitions = {}
        self.current_line = 0
        self.current_entry = POEntry(linenum=self.current_line)
        self.current_state = 'st'
        self.current_token = None
        # two memo flags used in handlers
        self.msgstr_index = 0
        self.entry_obsolete = 0
        # Configure the state machine, by adding transitions.
        # Signification of symbols:
        #     * ST: Beginning of the file (start)
        #     * HE: Header
        #     * TC: a translation comment
        #     * GC: a generated comment
        #     * OC: a file/line occurrence
        #     * FL: a flags line
        #     * CT: a message context
        #     * PC: a previous msgctxt
        #     * PM: a previous msgid
        #     * PP: a previous msgid_plural
        #     * MI: a msgid
        #     * MP: a msgid plural
        #     * MS: a msgstr
        #     * MX: a msgstr plural
        #     * MC: a msgid or msgstr continuation line
        all = ['st', 'he', 'gc', 'oc', 'fl', 'ct', 'pc', 'pm', 'pp', 'tc',
               'ms', 'mp', 'mx', 'mi']

        self.add('tc', ['st', 'he'],                                     'he')
        self.add('tc', ['gc', 'oc', 'fl', 'tc', 'pc', 'pm', 'pp', 'ms',
                        'mp', 'mx', 'mi'],                               'tc')
        self.add('gc', all,                                              'gc')
        self.add('oc', all,                                              'oc')
        self.add('fl', all,                                              'fl')
        self.add('pc', all,                                              'pc')
        self.add('pm', all,                                              'pm')
        self.add('pp', all,                                              'pp')
        self.add('ct', ['st', 'he', 'gc', 'oc', 'fl', 'tc', 'pc', 'pm',
                        'pp', 'ms', 'mx'],                               'ct')
        self.add('mi', ['st', 'he', 'gc', 'oc', 'fl', 'ct', 'tc', 'pc',
                 'pm', 'pp', 'ms', 'mx'],                                'mi')
        self.add('mp', ['tc', 'gc', 'pc', 'pm', 'pp', 'mi'],             'mp')
        self.add('ms', ['mi', 'mp', 'tc'],                               'ms')
        self.add('mx', ['mi', 'mx', 'mp', 'tc'],                         'mx')
        self.add('mc', ['ct', 'mi', 'mp', 'ms', 'mx', 'pm', 'pp', 'pc'], 'mc')

    def parse(self):
        """
        Run the state machine, parse the file line by line and call process()
        with the current matched symbol.
        """
        try:
            keywords = {
                'msgctxt': 'ct',
                'msgid': 'mi',
                'msgstr': 'ms',
                'msgid_plural': 'mp',
            }
            prev_keywords = {
                'msgid_plural': 'pp',
                'msgid': 'pm',
                'msgctxt': 'pc',
            }
            tokens = []
            fpath = '%s ' % self.instance.fpath if self.instance.fpath else ''
            for line in self.fhandle:
                self.current_line += 1
                if self.current_line == 1:
                    BOM = codecs.BOM_UTF8.decode('utf-8')
                    if line.startswith(BOM):
                        line = line[len(BOM):]
                line = line.strip()
                if line == '':
                    continue

                tokens = line.split(None, 2)
                nb_tokens = len(tokens)

                if tokens[0] == '#~|':
                    continue

                if tokens[0] == '#~' and nb_tokens > 1:
                    line = line[3:].strip()
                    tokens = tokens[1:]
                    nb_tokens -= 1
                    self.entry_obsolete = 1
                else:
                    self.entry_obsolete = 0

                # Take care of keywords like
                # msgid, msgid_plural, msgctxt & msgstr.
                if tokens[0] in keywords and nb_tokens > 1:
                    line = line[len(tokens[0]):].lstrip()
                    if re.search(r'([^\\]|^)"', line[1:-1]):
                        raise IOError('Syntax error in po file %s(line %s): '
                                      'unescaped double quote found' %
                                      (fpath, self.current_line))
                    if line[0] != '"' or line[-1] != '"':
                        raise IOError('Syntax error in po file %s(line %s): '
                                      'string not delimited by double quotes' %
                                      (fpath, self.current_line))
                    self.current_token = line
                    self.process(keywords[tokens[0]])
                    continue

                self.current_token = line

                if tokens[0] == '#:':
                    if nb_tokens <= 1:
                        continue
                    # we are on a occurrences line
                    self.process('oc')

                elif line[:1] == '"':
                    # we are on a continuation line
                    if re.search(r'([^\\]|^)"', line[1:-1]):
                        raise IOError('Syntax error in po file %s(line %s): '
                                      'unescaped double quote found' %
                                      (fpath, self.current_line))
                    if line[-1] != '"':
                        raise IOError('Syntax error in po file %s(line %s): '
                                      'string not delimited by double quotes' %
                                      (fpath, self.current_line))
                    self.process('mc')

                elif line[:7] == 'msgstr[':
                    # we are on a msgstr plural
                    self.process('mx')

                elif tokens[0] == '#,':
                    if nb_tokens <= 1:
                        continue
                    # we are on a flags line
                    self.process('fl')

                elif tokens[0] == '#' or tokens[0].startswith('##'):
                    if line == '#':
                        line += ' '
                    # we are on a translator comment line
                    self.process('tc')

                elif tokens[0] == '#.':
                    if nb_tokens <= 1:
                        continue
                    # we are on a generated comment line
                    self.process('gc')

                elif tokens[0] == '#|':
                    if nb_tokens <= 1:
                        raise IOError('Syntax error in po file %s(line %s)' %
                                      (fpath, self.current_line))

                    # Remove the marker and any whitespace right after that.
                    line = line[2:].lstrip()
                    self.current_token = line

                    if tokens[1].startswith('"'):
                        # Continuation of previous metadata.
                        self.process('mc')
                        continue

                    if nb_tokens == 2:
                        # Invalid continuation line.
                        raise IOError('Syntax error in po file %s(line %s): '
                                      'invalid continuation line' %
                                      (fpath, self.current_line))

                    # we are on a "previous translation" comment line,
                    if tokens[1] not in prev_keywords:
                        # Unknown keyword in previous translation comment.
                        raise IOError('Syntax error in po file %s(line %s): '
                                      'unknown keyword %s' %
                                      (fpath, self.current_line,
                                       tokens[1]))

                    # Remove the keyword and any whitespace
                    # between it and the starting quote.
                    line = line[len(tokens[1]):].lstrip()
                    self.current_token = line
                    self.process(prev_keywords[tokens[1]])

                else:
                    raise IOError('Syntax error in po file %s(line %s)' %
                                  (fpath, self.current_line))

            if self.current_entry and len(tokens) > 0 and \
               not tokens[0].startswith('#'):
                # since entries are added when another entry is found, we must
                # add the last entry here (only if there are lines). Trailing
                # comments are ignored
                self.instance.append(self.current_entry)

            # before returning the instance, check if there's metadata and if
            # so extract it in a dict
            metadataentry = self.instance.find('')
            if metadataentry:  # metadata found
                # remove the entry
                self.instance.remove(metadataentry)
                self.instance.metadata_is_fuzzy = metadataentry.flags
                key = None
                for msg in metadataentry.msgstr.splitlines():
                    try:
                        key, val = msg.split(':', 1)
                        self.instance.metadata[key] = val.strip()
                    except (ValueError, KeyError):
                        if key is not None:
                            self.instance.metadata[key] += '\n' + msg.strip()
        finally:
            # close opened file
            if not isinstance(self.fhandle, list):  # must be file
                self.fhandle.close()
        return self.instance

    def add(self, symbol, states, next_state):
        """
        Add a transition to the state machine.

        Keywords arguments:

        ``symbol``
            string, the matched token (two chars symbol).

        ``states``
            list, a list of states (two chars symbols).

        ``next_state``
            the next state the fsm will have after the action.
        """
        for state in states:
            action = getattr(self, 'handle_%s' % next_state)
            self.transitions[(symbol, state)] = (action, next_state)

    def process(self, symbol):
        """
        Process the transition corresponding to the current state and the
        symbol provided.

        Keywords arguments:

        ``symbol``
            string, the matched token (two chars symbol).

        ``linenum``
            integer, the current line number of the parsed file.
        """
        try:
            (action, state) = self.transitions[(symbol, self.current_state)]
            if action():
                self.current_state = state
        except Exception:
            fpath = '%s ' % self.instance.fpath if self.instance.fpath else ''
            if hasattr(self.fhandle, 'close'):
                self.fhandle.close()
            raise IOError('Syntax error in po file %s(line %s)' %
                          (fpath, self.current_line))

    # state handlers

    def handle_he(self):
        """Handle a header comment."""
        if self.instance.header != '':
            self.instance.header += '\n'
        self.instance.header += self.current_token[2:]
        return 1

    def handle_tc(self):
        """Handle a translator comment."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        if self.current_entry.tcomment != '':
            self.current_entry.tcomment += '\n'
        tcomment = self.current_token.lstrip('#')
        if tcomment.startswith(' '):
            tcomment = tcomment[1:]
        self.current_entry.tcomment += tcomment
        return True

    def handle_gc(self):
        """Handle a generated comment."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        if self.current_entry.comment != '':
            self.current_entry.comment += '\n'
        self.current_entry.comment += self.current_token[3:]
        return True

    def handle_oc(self):
        """Handle a file:num occurrence."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        occurrences = self.current_token[3:].split()
        for occurrence in occurrences:
            if occurrence != '':
                try:
                    fil, line = occurrence.rsplit(':', 1)
                    if not line.isdigit():
                        fil = occurrence
                        line = ''
                    self.current_entry.occurrences.append((fil, line))
                except (ValueError, AttributeError):
                    self.current_entry.occurrences.append((occurrence, ''))
        return True

    def handle_fl(self):
        """Handle a flags line."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        self.current_entry.flags += [c.strip() for c in
                                     self.current_token[3:].split(',')]
        return True

    def handle_pp(self):
        """Handle a previous msgid_plural line."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        self.current_entry.previous_msgid_plural = \
            unescape(self.current_token[1:-1])
        return True

    def handle_pm(self):
        """Handle a previous msgid line."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        self.current_entry.previous_msgid = \
            unescape(self.current_token[1:-1])
        return True

    def handle_pc(self):
        """Handle a previous msgctxt line."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        self.current_entry.previous_msgctxt = \
            unescape(self.current_token[1:-1])
        return True

    def handle_ct(self):
        """Handle a msgctxt."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        self.current_entry.msgctxt = unescape(self.current_token[1:-1])
        return True

    def handle_mi(self):
        """Handle a msgid."""
        if self.current_state in ['mc', 'ms', 'mx']:
            self.instance.append(self.current_entry)
            self.current_entry = POEntry(linenum=self.current_line)
        self.current_entry.obsolete = self.entry_obsolete
        self.current_entry.msgid = unescape(self.current_token[1:-1])
        return True

    def handle_mp(self):
        """Handle a msgid plural."""
        self.current_entry.msgid_plural = unescape(self.current_token[1:-1])
        return True

    def handle_ms(self):
        """Handle a msgstr."""
        self.current_entry.msgstr = unescape(self.current_token[1:-1])
        return True

    def handle_mx(self):
        """Handle a msgstr plural."""
        index = self.current_token[7]
        value = self.current_token[self.current_token.find('"') + 1:-1]
        self.current_entry.msgstr_plural[int(index)] = unescape(value)
        self.msgstr_index = int(index)
        return True

    def handle_mc(self):
        """Handle a msgid or msgstr continuation line."""
        token = unescape(self.current_token[1:-1])
        if self.current_state == 'ct':
            self.current_entry.msgctxt += token
        elif self.current_state == 'mi':
            self.current_entry.msgid += token
        elif self.current_state == 'mp':
            self.current_entry.msgid_plural += token
        elif self.current_state == 'ms':
            self.current_entry.msgstr += token
        elif self.current_state == 'mx':
            self.current_entry.msgstr_plural[self.msgstr_index] += token
        elif self.current_state == 'pp':
            self.current_entry.previous_msgid_plural += token
        elif self.current_state == 'pm':
            self.current_entry.previous_msgid += token
        elif self.current_state == 'pc':
            self.current_entry.previous_msgctxt += token
        # don't change the current state
        return False
# }}}
# class _MOFileParser {{{


class _MOFileParser(object):
    """
    A class to parse binary mo files.
    """

    def __init__(self, mofile, *args, **kwargs):
        """
        Constructor.

        Keyword arguments:

        ``mofile``
            string, path to the mo file or its content

        ``encoding``
            string, the encoding to use, defaults to ``default_encoding``
            global variable (optional).

        ``check_for_duplicates``
            whether to check for duplicate entries when adding entries to the
            file (optional, default: ``False``).
        """
        if _is_file(mofile):
            self.fhandle = open(mofile, 'rb')
        else:
            self.fhandle = io.BytesIO(mofile)

        klass = kwargs.get('klass')
        if klass is None:
            klass = MOFile
        self.instance = klass(
            fpath=mofile,
            encoding=kwargs.get('encoding', default_encoding),
            check_for_duplicates=kwargs.get('check_for_duplicates', False)
        )

    def __del__(self):
        """
        Make sure the file is closed, this prevents warnings on unclosed file
        when running tests with python >= 3.2.
        """
        if self.fhandle and hasattr(self.fhandle, 'close'):
            self.fhandle.close()

    def parse(self):
        """
        Build the instance with the file handle provided in the
        constructor.
        """
        # parse magic number
        magic_number = self._readbinary('<I', 4)
        if magic_number == MOFile.MAGIC:
            ii = '<II'
        elif magic_number == MOFile.MAGIC_SWAPPED:
            ii = '>II'
        else:
            raise IOError('Invalid mo file, magic number is incorrect !')
        self.instance.magic_number = magic_number
        # parse the version number and the number of strings
        version, numofstrings = self._readbinary(ii, 8)
        # from MO file format specs: "A program seeing an unexpected major
        # revision number should stop reading the MO file entirely"
        if version >> 16 not in (0, 1):
            raise IOError('Invalid mo file, unexpected major revision number')
        self.instance.version = version
        # original strings and translation strings hash table offset
        msgids_hash_offset, msgstrs_hash_offset = self._readbinary(ii, 8)
        # move to msgid hash table and read length and offset of msgids
        self.fhandle.seek(msgids_hash_offset)
        msgids_index = []
        for i in range(numofstrings):
            msgids_index.append(self._readbinary(ii, 8))
        # move to msgstr hash table and read length and offset of msgstrs
        self.fhandle.seek(msgstrs_hash_offset)
        msgstrs_index = []
        for i in range(numofstrings):
            msgstrs_index.append(self._readbinary(ii, 8))
        # build entries
        encoding = self.instance.encoding
        for i in range(numofstrings):
            self.fhandle.seek(msgids_index[i][1])
            msgid = self.fhandle.read(msgids_index[i][0])

            self.fhandle.seek(msgstrs_index[i][1])
            msgstr = self.fhandle.read(msgstrs_index[i][0])
            if i == 0 and not msgid:  # metadata
                raw_metadata, metadata = msgstr.split(b('\n')), {}
                for line in raw_metadata:
                    tokens = line.split(b(':'), 1)
                    if tokens[0] != b(''):
                        try:
                            k = tokens[0].decode(encoding)
                            v = tokens[1].decode(encoding)
                            metadata[k] = v.strip()
                        except IndexError:
                            metadata[k] = u('')
                self.instance.metadata = metadata
                continue
            # test if we have a plural entry
            msgid_tokens = msgid.split(b('\0'))
            if len(msgid_tokens) > 1:
                entry = self._build_entry(
                    msgid=msgid_tokens[0],
                    msgid_plural=msgid_tokens[1],
                    msgstr_plural=dict((k, v) for k, v in
                                       enumerate(msgstr.split(b('\0'))))
                )
            else:
                entry = self._build_entry(msgid=msgid, msgstr=msgstr)
            self.instance.append(entry)
        # close opened file
        self.fhandle.close()
        return self.instance

    def _build_entry(self, msgid, msgstr=None, msgid_plural=None,
                     msgstr_plural=None):
        msgctxt_msgid = msgid.split(b('\x04'))
        encoding = self.instance.encoding
        if len(msgctxt_msgid) > 1:
            kwargs = {
                'msgctxt': msgctxt_msgid[0].decode(encoding),
                'msgid': msgctxt_msgid[1].decode(encoding),
            }
        else:
            kwargs = {'msgid': msgid.decode(encoding)}
        if msgstr:
            kwargs['msgstr'] = msgstr.decode(encoding)
        if msgid_plural:
            kwargs['msgid_plural'] = msgid_plural.decode(encoding)
        if msgstr_plural:
            for k in msgstr_plural:
                msgstr_plural[k] = msgstr_plural[k].decode(encoding)
            kwargs['msgstr_plural'] = msgstr_plural
        return MOEntry(**kwargs)

    def _readbinary(self, fmt, numbytes):
        """
        Private method that unpack n bytes of data using format <fmt>.
        It returns a tuple or a mixed value if the tuple length is 1.
        """
        bytes = self.fhandle.read(numbytes)
        tup = struct.unpack(fmt, bytes)
        if len(tup) == 1:
            return tup[0]
        return tup
# }}}
# layered catalog translation {{{


class PluralRuleError(ValueError):
    """
    Raised when a ``Plural-Forms`` header cannot be parsed or evaluated.

    The ``code`` attribute is one of ``missing``, ``invalid`` or ``index``.
    """

    def __init__(self, message, code='invalid'):
        ValueError.__init__(self, message)
        self.code = code


class _CatalogUnavailable(Exception):
    """
    Internal exception used to turn a missing or broken catalog into a
    fallback reason instead of aborting the whole resolution.
    """

    def __init__(self, reason, detail):
        Exception.__init__(self, detail)
        self.reason = reason
        self.detail = detail


class _PluralExpressionParser(object):
    """
    Small recursive-descent parser for the C-like expressions used by
    gettext's ``Plural-Forms`` header.

    Only the operators and operands permitted by gettext are accepted, and
    the expression is interpreted by this module instead of by ``eval()``.
    """

    _TOKEN_RE = re.compile(
        r'\s*(?:(\d+)|(n)|(\|\||&&|==|!=|<=|>=|[<>+\-*/%!?:()]))'
    )

    def __init__(self, expression):
        self.expression = expression
        self.tokens = self._tokenize(expression)
        self.position = 0

    def _tokenize(self, expression):
        tokens = []
        position = 0
        length = len(expression)
        while position < length:
            match = self._TOKEN_RE.match(expression, position)
            if match is None:
                rest = expression[position:]
                if rest.strip() == '':
                    break
                raise PluralRuleError(
                    'unsupported token in plural expression: %r' % rest,
                    'invalid'
                )
            position = match.end()
            number, name, operator = match.groups()
            if number is not None:
                tokens.append(('int', int(number)))
            elif name is not None:
                tokens.append(('n', name))
            else:
                tokens.append(('op', operator))
        return tokens

    def _peek(self):
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return (None, None)

    def _take(self):
        token = self._peek()
        self.position += 1
        return token

    def _expect(self, value):
        kind, token = self._take()
        if kind != 'op' or token != value:
            raise PluralRuleError(
                'expected %r in plural expression: %s' % (
                    value, self.expression
                ),
                'invalid'
            )

    def parse(self):
        node = self._ternary()
        if self.position != len(self.tokens):
            raise PluralRuleError(
                'unexpected token in plural expression: %s' % self.expression,
                'invalid'
            )
        return node

    def _ternary(self):
        condition = self._logical_or()
        kind, token = self._peek()
        if kind == 'op' and token == '?':
            self._take()
            when_true = self._ternary()
            self._expect(':')
            when_false = self._ternary()
            return ('conditional', condition, when_true, when_false)
        return condition

    def _logical_or(self):
        node = self._logical_and()
        while self._peek() == ('op', '||'):
            self._take()
            node = ('or', node, self._logical_and())
        return node

    def _logical_and(self):
        node = self._equality()
        while self._peek() == ('op', '&&'):
            self._take()
            node = ('and', node, self._equality())
        return node

    def _equality(self):
        node = self._relational()
        while self._peek()[0] == 'op' and self._peek()[1] in ('==', '!='):
            operator = self._take()[1]
            node = ('compare', operator, node, self._relational())
        return node

    def _relational(self):
        node = self._additive()
        while self._peek()[0] == 'op' and self._peek()[1] in (
                '<', '>', '<=', '>='):
            operator = self._take()[1]
            node = ('compare', operator, node, self._additive())
        return node

    def _additive(self):
        node = self._multiplicative()
        while self._peek()[0] == 'op' and self._peek()[1] in ('+', '-'):
            operator = self._take()[1]
            node = ('arithmetic', operator, node, self._multiplicative())
        return node

    def _multiplicative(self):
        node = self._unary()
        while self._peek()[0] == 'op' and self._peek()[1] in ('*', '/', '%'):
            operator = self._take()[1]
            node = ('arithmetic', operator, node, self._unary())
        return node

    def _unary(self):
        kind, token = self._peek()
        if kind == 'op' and token in ('!', '-', '+'):
            self._take()
            return ('unary', token, self._unary())
        return self._primary()

    def _primary(self):
        kind, token = self._take()
        if kind == 'int':
            return ('int', token)
        if kind == 'n':
            return ('n', token)
        if kind == 'op' and token == '(':
            node = self._ternary()
            self._expect(')')
            return node
        raise PluralRuleError(
            'unexpected token in plural expression: %s' % self.expression,
            'invalid'
        )


def _plural_evaluate(node, count):
    kind = node[0]
    if kind == 'int':
        return node[1]
    if kind == 'n':
        return count
    if kind == 'unary':
        value = _plural_evaluate(node[2], count)
        if node[1] == '!':
            return 0 if value else 1
        if node[1] == '-':
            return -value
        return value
    if kind == 'conditional':
        condition = _plural_evaluate(node[1], count)
        return _plural_evaluate(
            node[2] if condition else node[3], count
        )
    if kind == 'or':
        left = _plural_evaluate(node[1], count)
        if left:
            return 1
        return 1 if _plural_evaluate(node[2], count) else 0
    if kind == 'and':
        left = _plural_evaluate(node[1], count)
        if not left:
            return 0
        return 1 if _plural_evaluate(node[2], count) else 0
    left = _plural_evaluate(node[2], count)
    right = _plural_evaluate(node[3], count)
    if kind == 'compare':
        operator = node[1]
        if operator == '==':
            return 1 if left == right else 0
        if operator == '!=':
            return 1 if left != right else 0
        if operator == '<':
            return 1 if left < right else 0
        if operator == '>':
            return 1 if left > right else 0
        if operator == '<=':
            return 1 if left <= right else 0
        if operator == '>=':
            return 1 if left >= right else 0
    if kind == 'arithmetic':
        operator = node[1]
        if operator == '+':
            return left + right
        if operator == '-':
            return left - right
        if operator == '*':
            return left * right
        if right == 0:
            raise PluralRuleError(
                'division by zero in plural expression', 'invalid'
            )
        if operator == '/':
            return int(left / float(right))
        if operator == '%':
            return left % right
    raise PluralRuleError('invalid plural expression node', 'invalid')


class PluralRule(object):
    """
    A parsed gettext plural rule.

    The rule is made of a ``nplurals`` count and a ``plural`` expression
    (for example ``nplurals=2; plural=(n != 1);``).  Each catalog carries
    its own rule, which is important when a region catalog and the common
    language catalog do not use the same number or ordering of plural
    forms.
    """

    def __init__(self, nplurals, expression, raw=None):
        try:
            nplurals = int(nplurals)
        except (TypeError, ValueError):
            raise PluralRuleError('nplurals must be an integer', 'invalid')
        if nplurals < 1:
            raise PluralRuleError('nplurals must be positive', 'invalid')
        if not expression or not text_type(expression).strip():
            raise PluralRuleError('plural expression is empty', 'invalid')
        self.nplurals = nplurals
        self.expression = text_type(expression).strip()
        self.raw = raw if raw is not None else self.expression
        self._tree = _PluralExpressionParser(self.expression).parse()

    @classmethod
    def from_metadata(cls, metadata):
        """
        Build a rule from a PO/MO metadata dictionary.

        ``Plural-Forms`` is the header written by gettext tools.  Catalogs
        without it cannot be used to resolve plural messages.
        """
        raw = None
        if metadata:
            raw = metadata.get('Plural-Forms')
        if not raw:
            raise PluralRuleError(
                'catalog has no Plural-Forms metadata', 'missing'
            )
        raw = text_type(raw).strip()
        nplurals_match = re.search(r'nplurals\s*=\s*(\d+)', raw, re.I)
        plural_match = re.search(
            r'plural\s*=\s*(.*?)\s*(?:;\s*)?$', raw, re.I | re.S
        )
        if nplurals_match is None or plural_match is None:
            raise PluralRuleError(
                'invalid Plural-Forms metadata: %s' % raw, 'invalid'
            )
        return cls(
            int(nplurals_match.group(1)),
            plural_match.group(1).replace(';', '').strip(),
            raw
        )

    def index(self, count):
        """
        Return the plural form index for ``count`` according to this rule.

        An ``PluralRuleError`` is raised when the expression evaluates
        outside of ``0 .. nplurals - 1``.
        """
        if isinstance(count, bool) or not isinstance(count, (int, long)):
            raise TypeError('count must be an integer')
        if count < 0:
            raise ValueError('count must be greater than or equal to zero')
        index = _plural_evaluate(self._tree, int(count))
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise PluralRuleError(
                'plural expression did not return an integer', 'invalid'
            )
        if index < 0 or index >= self.nplurals:
            raise PluralRuleError(
                'plural index %s is outside 0..%s' % (
                    index, self.nplurals - 1
                ),
                'index'
            )
        return index

    def __repr__(self):
        return 'PluralRule(nplurals=%r, expression=%r)' % (
            self.nplurals, self.expression
        )


class TranslationCatalog(object):
    """
    One catalog layer (a region, a common language or the product default).

    ``path`` is a PO or MO file and may be ``None`` when the layer is not
    configured for the selected region.  Catalogs are loaded lazily so that
    a missing nearer layer can be reported as a fallback reason.
    """

    def __init__(self, name, path, role=None, locale=None):
        self.name = text_type(name)
        self.role = role
        self.locale = locale
        self.path = None
        if path:
            self.path = os.path.abspath(os.path.expanduser(text_type(path)))
        self._file = None
        self._load_error = None
        self._plural_rule = None
        self._plural_rule_error = None

    @property
    def source_directory(self):
        if self.path is None:
            return None
        return os.path.dirname(self.path)

    def load(self):
        """
        Load and return the underlying POFile/MOFile instance.
        """
        if self._file is not None:
            return self._file
        if self._load_error is not None:
            raise self._load_error
        if self.path is None:
            self._load_error = _CatalogUnavailable(
                'catalog_not_configured',
                'no catalog is configured for this layer'
            )
            raise self._load_error
        if not os.path.isfile(self.path):
            self._load_error = _CatalogUnavailable(
                'catalog_missing',
                'catalog file does not exist: %s' % self.path
            )
            raise self._load_error
        try:
            if self.path.lower().endswith('.mo'):
                self._file = mofile(self.path)
            else:
                self._file = pofile(self.path)
        except Exception as exc:
            self._load_error = _CatalogUnavailable(
                'catalog_error',
                'cannot load catalog %s: %s' % (self.path, exc)
            )
            raise self._load_error
        return self._file

    def find_entry(self, key, msgctxt=None):
        """
        Return the entry for ``key`` including obsolete entries.

        The caller decides whether obsolete or fuzzy entries can be used,
        so this lookup deliberately does not hide them.  When the same
        msgid exists both live and obsolete, the live entry is preferred.
        A ``msgctxt`` of ``None`` selects the entry without context.
        """
        catalog = self.load()
        if msgctxt is None:
            matches = [
                entry for entry in catalog
                if entry.msgid == key and not entry.msgctxt
            ]
        else:
            matches = [
                entry for entry in catalog
                if entry.msgid == key and entry.msgctxt == msgctxt
            ]
        if not matches:
            return None
        active = [
            entry for entry in matches
            if not getattr(entry, 'obsolete', False)
        ]
        if active:
            matches = active
        return matches[0]

    @property
    def plural_rule(self):
        """
        The catalog's own :class:`PluralRule`, parsed from its metadata.
        """
        if self._plural_rule is not None:
            return self._plural_rule
        if self._plural_rule_error is not None:
            raise self._plural_rule_error
        try:
            metadata = self.load().metadata
            self._plural_rule = PluralRule.from_metadata(metadata)
        except _CatalogUnavailable:
            raise
        except PluralRuleError as exc:
            self._plural_rule_error = exc
            raise
        return self._plural_rule

    def to_dict(self):
        return {
            'name': self.name,
            'role': self.role,
            'locale': self.locale,
            'path': self.path,
            'source_directory': self.source_directory,
        }

    def __repr__(self):
        return 'TranslationCatalog(%r, %r)' % (self.name, self.path)


class SkippedLayer(object):
    """
    Why a layer was not used for a resolution.

    ``reason`` is a stable ASCII identifier (for example ``fuzzy`` or
    ``incomplete_plural_set``) and ``detail`` is a human-readable
    explanation suitable for a ticket.
    """

    def __init__(self, layer, reason, detail, plural_index=None):
        self.layer = layer
        self.reason = reason
        self.detail = detail
        self.plural_index = plural_index

    @property
    def name(self):
        return self.layer.name

    @property
    def path(self):
        return self.layer.path

    @property
    def source_directory(self):
        return self.layer.source_directory

    def to_dict(self):
        return {
            'layer': self.name,
            'role': self.layer.role,
            'locale': self.layer.locale,
            'path': self.path,
            'source_directory': self.source_directory,
            'reason': self.reason,
            'detail': self.detail,
            'plural_index': self.plural_index,
        }

    def __repr__(self):
        return 'SkippedLayer(%r, %r)' % (self.name, self.reason)


class TranslationResolution(object):
    """
    The result of resolving one key for one region and one count.

    ``text`` is the final translation (``None`` when nothing resolved),
    ``source`` is the winning layer name and ``source_directory`` is the
    directory that contains the winning catalog.  ``skipped`` lists the
    nearer layers that were passed over, with their reasons.
    """

    def __init__(self, region, key, count, msgctxt=None, policy=None):
        self.region = region
        self.key = key
        self.count = count
        self.msgctxt = msgctxt
        self.policy = policy
        self.text = None
        self.source = None
        self.source_directory = None
        self.source_catalog = None
        self.plural_index = None
        self.nplurals = None
        self.skipped = []

    @property
    def resolved(self):
        return self.text is not None

    def to_dict(self):
        return {
            'region': self.region,
            'key': self.key,
            'msgctxt': self.msgctxt,
            'count': self.count,
            'policy': self.policy,
            'resolved': self.resolved,
            'text': self.text,
            'source': self.source,
            'source_directory': self.source_directory,
            'source_catalog': self.source_catalog,
            'plural_index': self.plural_index,
            'nplurals': self.nplurals,
            'skipped': [item.to_dict() for item in self.skipped],
        }

    def __repr__(self):
        return 'TranslationResolution(key=%r, region=%r, source=%r)' % (
            self.key, self.region, self.source
        )


_POLICY_ALIASES = {
    'branch': 'branch',
    'per_branch': 'branch',
    'per-branch': 'branch',
    'branchwise': 'branch',
    'branch_wise': 'branch',
    'branch-fallback': 'branch',
    'branch_fallback': 'branch',
    'atomic': 'atomic',
    'same_layer': 'atomic',
    'same-layer': 'atomic',
    'whole_layer': 'atomic',
    'whole-layer': 'atomic',
    'atomic-fallback': 'atomic',
    'atomic_fallback': 'atomic',
}


def _normalize_policy(policy):
    if policy is None:
        return None
    key = text_type(policy).strip().lower().replace(' ', '_')
    if key not in _POLICY_ALIASES:
        raise ValueError(
            'unknown fallback policy %r (expected branch or atomic)' % policy
        )
    return _POLICY_ALIASES[key]


def _declared_policy(entry):
    for flag in entry.flags:
        policy = _POLICY_ALIASES.get(flag.strip().lower())
        if policy:
            return policy
    texts = [entry.comment or '', entry.tcomment or '']
    for text_value in texts:
        match = re.search(
            r'fallback\s*[:=]\s*(branch|per[_-]?branch|atomic|'
            r'same[_-]?layer|whole[_-]?layer)', text_value, re.I
        )
        if match:
            return _normalize_policy(match.group(1))
    return None


def _coerce_catalog_layer(value, index):
    if isinstance(value, TranslationCatalog):
        return value
    if isinstance(value, dict):
        name = value.get('name') or value.get('layer') or (
            value.get('role') or 'layer%d' % (index + 1)
        )
        path = value.get('path') or value.get('catalog') or value.get('file')
        return TranslationCatalog(
            name, path,
            role=value.get('role'),
            locale=value.get('locale')
        )
    if isinstance(value, (text_type, bytes)):
        return TranslationCatalog('layer%d' % (index + 1), value)
    raise TypeError('invalid catalog layer: %r' % (value,))


def _json_config(config):
    if isinstance(config, dict):
        return config
    if not isinstance(config, (text_type, bytes)):
        raise TypeError('config must be a path or a dictionary')
    with io.open(config, 'r', encoding='utf-8') as fhandle:
        return json.load(fhandle)


def _config_base_dir(config_path, explicit):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if isinstance(config_path, (text_type, bytes)):
        return os.path.dirname(os.path.abspath(config_path))
    return os.getcwd()


def _resolve_config_path(path, base_dir):
    if not path:
        return None
    path = os.path.expanduser(text_type(path))
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    return os.path.abspath(path)


def _normalized_locale(value):
    return text_type(value).replace('_', '-').lower()


class TranslationResolver(object):
    """
    Resolve keys against an ordered list of catalog layers.

    ``layers`` are ordered from nearest to farthest (typically region,
    common language, product default).  The first layer that supplies a
    usable translation wins.

    ``policies`` maps message keys to ``branch`` or ``atomic``.  With
    ``branch`` the requested plural branch may fall back on its own; with
    ``atomic`` every plural branch of the message must be present and
    translated in the same layer.  A message can override the configured
    policy through a ``fallback:branch``/``fallback:atomic`` extracted or
    translator comment, or through a ``branch-fallback``/``atomic-fallback``
    PO flag.
    """

    def __init__(self, layers, policies=None, default_policy='branch'):
        self.layers = [
            _coerce_catalog_layer(layer, index)
            for index, layer in enumerate(layers)
        ]
        self.policies = dict(policies or {})
        self.default_policy = _normalize_policy(default_policy) or 'branch'

    @classmethod
    def from_config(cls, config, base_dir=None):
        """
        Build a resolver from a configuration dictionary or JSON file.

        The ``layers`` form is ordered and region independent::

            {
              "layers": [
                {"name": "region", "path": "de-DE/app.po"},
                {"name": "language", "path": "de/app.po"},
                {"name": "default", "path": "en/app.po"}
              ],
              "policies": {"checkout.items": "atomic"}
            }

        Relative paths are resolved against the configuration file's
        directory (or ``base_dir`` when one is supplied).
        """
        config_source = config
        config = _json_config(config)
        base = _config_base_dir(config_source, base_dir)
        layers = []
        for index, layer in enumerate(config.get('layers') or []):
            if isinstance(layer, dict):
                layer = dict(layer)
                path = layer.get('path') or layer.get('catalog')
                if path:
                    layer['path'] = _resolve_config_path(path, base)
            layers.append(_coerce_catalog_layer(layer, index))
        return cls(
            layers,
            policies=config.get('policies') or config.get('messages'),
            default_policy=config.get('default_policy') or 'branch'
        )

    def _configured_policy(self, key, msgctxt):
        candidates = []
        if msgctxt:
            candidates.append('%s\x04%s' % (msgctxt, key))
        candidates.append(key)
        for candidate in candidates:
            if candidate not in self.policies:
                continue
            value = self.policies[candidate]
            if isinstance(value, dict):
                value = value.get('fallback') or value.get('policy')
            return _normalize_policy(value)
        return None

    def _policy_for(self, key, msgctxt, explicit):
        policy = _normalize_policy(explicit)
        if policy is not None:
            return policy
        policy = self._configured_policy(key, msgctxt)
        if policy is not None:
            return policy
        for layer in self.layers:
            try:
                entry = layer.find_entry(key, msgctxt)
            except _CatalogUnavailable:
                continue
            if entry is None:
                continue
            declared = _declared_policy(entry)
            if declared is not None:
                return declared
        return self.default_policy

    def _skip(self, result, layer, reason, detail, plural_index=None):
        result.skipped.append(
            SkippedLayer(layer, reason, detail, plural_index)
        )

    def _winner(self, result, layer, text, plural_index, nplurals):
        result.text = text
        result.source = layer.name
        result.source_directory = layer.source_directory
        result.source_catalog = layer.path
        result.plural_index = plural_index
        result.nplurals = nplurals
        return result

    def _incomplete_plural_branches(self, entry, rule):
        missing = []
        for index in range(rule.nplurals):
            value = entry.msgstr_plural.get(index)
            if value is None or value == '':
                missing.append(index)
        return missing

    def resolve(self, key, count=1, msgctxt=None, policy=None, region=None):
        """
        Resolve ``key`` for ``count``.

        ``count`` is used by every candidate layer's own plural rule; the
        selected slot is never copied from the nearer layer's index.
        """
        if not isinstance(key, (text_type, bytes)) or key == '':
            raise ValueError('key must be a non-empty string')
        key = text_type(key)
        if isinstance(count, bool) or not isinstance(count, (int, long)):
            raise TypeError('count must be an integer')
        if count < 0:
            raise ValueError('count must be greater than or equal to zero')
        policy = self._policy_for(key, msgctxt, policy)
        result = TranslationResolution(
            region=region,
            key=key,
            count=int(count),
            msgctxt=msgctxt,
            policy=policy
        )
        for layer in self.layers:
            try:
                layer.load()
            except _CatalogUnavailable as exc:
                self._skip(result, layer, exc.reason, exc.detail)
                continue
            entry = layer.find_entry(key, msgctxt)
            if entry is None:
                self._skip(result, layer, 'no_entry', 'key not found')
                continue
            if getattr(entry, 'obsolete', False):
                self._skip(result, layer, 'obsolete', 'entry is obsolete')
                continue
            if getattr(entry, 'fuzzy', False):
                self._skip(result, layer, 'fuzzy', 'entry is marked fuzzy')
                continue
            if not entry.msgid_plural:
                if entry.msgstr == '':
                    self._skip(
                        result, layer, 'untranslated', 'msgstr is empty'
                    )
                    continue
                return self._winner(
                    result, layer, entry.msgstr, None, None
                )
            try:
                rule = layer.plural_rule
            except _CatalogUnavailable as exc:
                self._skip(result, layer, exc.reason, exc.detail)
                continue
            except PluralRuleError as exc:
                reason = (
                    'plural_rule_missing'
                    if exc.code == 'missing' else 'plural_rule_invalid'
                )
                self._skip(result, layer, reason, text_type(exc))
                continue
            try:
                index = rule.index(count)
            except PluralRuleError as exc:
                reason = (
                    'plural_index_out_of_range'
                    if exc.code == 'index' else 'plural_rule_invalid'
                )
                self._skip(
                    result, layer, reason, text_type(exc),
                    plural_index=None
                )
                continue
            if policy == 'atomic':
                missing = self._incomplete_plural_branches(entry, rule)
                if missing:
                    detail = 'missing or empty plural branches: %s' % (
                        ', '.join([text_type(item) for item in missing])
                    )
                    self._skip(
                        result, layer, 'incomplete_plural_set', detail,
                        plural_index=index
                    )
                    continue
            value = entry.msgstr_plural.get(index)
            if value is None:
                self._skip(
                    result, layer, 'plural_slot_missing',
                    'msgstr[%s] is not present for count %s' % (
                        index, count
                    ),
                    plural_index=index
                )
                continue
            if value == '':
                self._skip(
                    result, layer, 'untranslated',
                    'msgstr[%s] is empty' % index,
                    plural_index=index
                )
                continue
            return self._winner(
                result, layer, value, index, rule.nplurals
            )
        return result

    def resolve_tickets(self, tickets, region=None):
        return [
            self.resolve(
                ticket.get('key') or ticket.get('msgid'),
                count=ticket.get('count', 1),
                msgctxt=ticket.get('msgctxt'),
                policy=ticket.get('policy'),
                region=ticket.get('region', region)
            )
            for ticket in tickets
        ]


class CatalogSet(object):
    """
    The three-tier region/language/product-default catalog configuration.

    The configuration describes shared catalog locations and per-region
    overrides::

        {
          "product_default": "locales/en/LC_MESSAGES/app.po",
          "languages": {"de": "locales/de/LC_MESSAGES/app.po"},
          "regions": {"de-DE": "locales/de-DE/LC_MESSAGES/app.po"},
          "policies": {"checkout.items": "atomic"},
          "default_policy": "branch"
        }

    A region value may also be an object with ``catalog`` and ``language``
    keys, which is useful when the region tag does not map directly to a
    language.  A missing region or language catalog is not fatal: it
    becomes a skipped layer with an explanatory reason.
    """

    def __init__(self, product_default, languages=None, regions=None,
                 policies=None, default_policy='branch', base_dir=None):
        self.base_dir = base_dir or os.getcwd()
        self.product_default = self._path_spec(product_default)
        self.languages = self._spec_map(languages)
        self.regions = self._spec_map(regions)
        self.policies = dict(policies or {})
        self.default_policy = _normalize_policy(default_policy) or 'branch'
        self._resolver_cache = {}

    def _path_spec(self, value):
        if value is None:
            return None
        if isinstance(value, dict):
            value = dict(value)
            path = value.get('path') or value.get('catalog')
            if path:
                value['path'] = _resolve_config_path(path, self.base_dir)
            return value
        return _resolve_config_path(value, self.base_dir)

    def _spec_map(self, values):
        result = {}
        for name, spec in (values or {}).items():
            result[_normalized_locale(name)] = (name, self._path_spec(spec))
        return result

    @classmethod
    def from_config(cls, config, base_dir=None):
        config_source = config
        config = _json_config(config)
        base = _config_base_dir(config_source, base_dir)
        product_default = (
            config.get('product_default') or
            config.get('default') or
            config.get('product_default_catalog')
        )
        return cls(
            product_default,
            languages=config.get('languages'),
            regions=config.get('regions'),
            policies=config.get('policies') or config.get('messages'),
            default_policy=config.get('default_policy') or 'branch',
            base_dir=base
        )

    def _lookup(self, mapping, locale):
        normalized = _normalized_locale(locale)
        if normalized in mapping:
            return mapping[normalized]
        for key, value in mapping.items():
            if key.lower() == normalized:
                return value
        return None, None

    def _lookup_language(self, region):
        parts = _normalized_locale(region).split('-')
        for size in range(len(parts) - 1, 0, -1):
            candidate = '-'.join(parts[:size])
            name, spec = self._lookup(self.languages, candidate)
            if name:
                return name, spec
        return None, None

    def layers_for_region(self, region):
        """
        Build the ordered layer list for ``region``.

        The order is always region, common language, product default.  The
        common language is the longest configured prefix of the region tag
        (``de`` for ``de-DE``, ``zh-Hant`` for ``zh-Hant-TW``).
        """
        if not region:
            raise ValueError('region must be a non-empty string')
        region = text_type(region)
        configured_name, region_spec = self._lookup(self.regions, region)
        if isinstance(region_spec, dict):
            region_path = region_spec.get('path') or region_spec.get(
                'catalog'
            )
            language = region_spec.get('language')
        else:
            region_path = region_spec
            language = None
        if not configured_name:
            region_path = None
        language_name, language_spec = self._lookup_language(
            language or region
        )
        if isinstance(language_spec, dict):
            language_path = language_spec.get('path') or language_spec.get(
                'catalog'
            )
        else:
            language_path = language_spec
        if not language_name:
            language_name = language or region.replace(
                '_', '-'
            ).split('-')[0]
        layers = [
            TranslationCatalog(
                'region:%s' % region, region_path, 'region', region
            ),
            TranslationCatalog(
                'language:%s' % language_name, language_path,
                'language', language_name
            ),
        ]
        default_name = 'default'
        default_path = self.product_default
        if isinstance(default_path, dict):
            default_name = default_path.get('name') or default_name
            default_path = default_path.get('path') or default_path.get(
                'catalog'
            )
        layers.append(
            TranslationCatalog(default_name, default_path, 'default', None)
        )
        return layers

    def resolver_for_region(self, region):
        normalized = _normalized_locale(region)
        if normalized not in self._resolver_cache:
            self._resolver_cache[normalized] = TranslationResolver(
                self.layers_for_region(region),
                policies=self.policies,
                default_policy=self.default_policy
            )
        return self._resolver_cache[normalized]

    def resolve(self, region, key, count=1, msgctxt=None, policy=None):
        resolver = self.resolver_for_region(region)
        return resolver.resolve(
            key, count=count, msgctxt=msgctxt, policy=policy, region=region
        )


def load_catalog_config(config, base_dir=None):
    """
    Load a catalog configuration from a JSON file or dictionary.

    Returns a :class:`CatalogSet` for a three-tier configuration or a
    :class:`TranslationResolver` for a configuration with an explicit
    ``layers`` list.  Already-built objects are returned unchanged.
    """
    if isinstance(config, (CatalogSet, TranslationResolver)):
        return config
    config_source = config
    raw = _json_config(config)
    if 'layers' in raw:
        return TranslationResolver.from_config(
            raw, base_dir=_config_base_dir(config_source, base_dir)
        )
    return CatalogSet.from_config(
        raw, base_dir=_config_base_dir(config_source, base_dir)
    )


def resolve_translation(config, region, key, count=1, msgctxt=None,
                        policy=None):
    """
    Resolve one ticket (``region``, ``key``, ``count``) against ``config``.

    ``config`` may be a path to a JSON file, a configuration dictionary, a
    :class:`CatalogSet` or a :class:`TranslationResolver`.  The return value
    is a :class:`TranslationResolution`; inspect ``text``, ``source`` and
    ``source_directory`` for the answer and ``skipped`` for the reasons
    nearer layers were passed over.
    """
    target = load_catalog_config(config)
    if isinstance(target, CatalogSet):
        return target.resolve(
            region, key, count=count, msgctxt=msgctxt, policy=policy
        )
    return target.resolve(
        key, count=count, msgctxt=msgctxt, policy=policy, region=region
    )


def resolve_tickets(config, tickets):
    """
    Resolve a batch of ticket dictionaries against one configuration.

    Each ticket needs ``region`` and ``key``; ``count``, ``msgctxt`` and
    ``policy`` are optional.  The configuration is loaded only once.
    """
    if isinstance(tickets, dict):
        tickets = tickets.get('tickets') or []
    target = load_catalog_config(config)
    results = []
    for index, ticket in enumerate(tickets):
        if not isinstance(ticket, dict):
            raise TypeError('ticket %d must be a dictionary' % index)
        key = ticket.get('key') or ticket.get('msgid')
        if not key:
            raise ValueError('ticket %d has no key' % index)
        region = ticket.get('region')
        if not region:
            raise ValueError('ticket %d has no region' % index)
        results.append(resolve_translation(
            target, region, key,
            count=ticket.get('count', 1),
            msgctxt=ticket.get('msgctxt'),
            policy=ticket.get('policy')
        ))
    return results


def _resolution_text(result):
    lines = [
        'key: %s' % result.key,
        'region: %s' % result.region,
        'count: %s' % result.count,
        'policy: %s' % result.policy,
        'text: %s' % (result.text if result.resolved else '<unresolved>'),
        'source: %s' % (result.source if result.source else '<none>'),
        'source_directory: %s' % (
            result.source_directory if result.source_directory
            else '<none>'
        ),
    ]
    if result.plural_index is not None:
        lines.append(
            'plural_index: %s (nplurals=%s)' % (
                result.plural_index, result.nplurals
            )
        )
    lines.append('skipped:')
    if not result.skipped:
        lines.append('  (none)')
    for item in result.skipped:
        lines.append(
            '  - %s [%s] %s' % (item.name, item.reason, item.detail)
        )
        lines.append(
            '    path: %s' % (item.path if item.path else '<none>')
        )
    return '\n'.join(lines)


def _cli_main(argv=None):
    import argparse
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if argv and argv[0] == 'resolve':
        argv = argv[1:]
    parser = argparse.ArgumentParser(
        prog='polib',
        description='Resolve a translation from layered gettext catalogs.'
    )
    parser.add_argument('--config', required=True)
    parser.add_argument('--region')
    parser.add_argument('--key')
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--msgctxt')
    parser.add_argument('--policy')
    parser.add_argument('--tickets')
    parser.add_argument('--json', action='store_true', dest='as_json')
    parser.add_argument('--out')
    args = parser.parse_args(argv)

    target = load_catalog_config(args.config)
    if args.tickets:
        if args.region or args.key:
            parser.error('--tickets cannot be combined with --region/--key')
        with io.open(args.tickets, 'r', encoding='utf-8') as fhandle:
            tickets = json.load(fhandle)
        results = resolve_tickets(target, tickets)
    else:
        if not args.region or not args.key:
            parser.error('--region and --key are required without --tickets')
        results = [resolve_translation(
            target, args.region, args.key,
            count=args.count,
            msgctxt=args.msgctxt,
            policy=args.policy
        )]

    if args.as_json:
        payload = [item.to_dict() for item in results]
        output = json.dumps(
            payload if args.tickets else payload[0],
            ensure_ascii=False, indent=2, sort_keys=True
        ) + '\n'
    else:
        output = '\n\n'.join([_resolution_text(item) for item in results])
        output += '\n'
    if args.out:
        with io.open(args.out, 'w', encoding='utf-8') as fhandle:
            fhandle.write(output)
    else:
        sys.stdout.write(output)
    if any(not item.resolved for item in results):
        return 1
    return 0


def main(argv=None):
    """
    Command-line entry point for layered catalog resolution.

    Returns the process exit status: ``0`` when every ticket resolved and
    ``1`` when at least one ticket had no translation in any layer.
    """
    return _cli_main(argv)


def _translation_cli():
    sys.exit(main())
# }}}


if __name__ == '__main__':
    _translation_cli()

#!/usr/bin/env python
# -*- coding: utf-8 -*-

import codecs
import os
import struct
import subprocess
import sys
import tempfile
import unittest
try:
    from shutil import which
except ImportError:
    from distutils.spawn import find_executable as which  # Deprecated in 3.10, removed in 3.12.

sys.path.insert(1, os.path.abspath('.'))

import polib

from polib import u

class TestFunctions(unittest.TestCase):

    def test_pofile_and_mofile1(self):
        """
        Test bad usage of pofile/mofile.
        """
        data = u('''# test for pofile/mofile with string buffer
msgid ""
msgstr ""
"Project-Id-Version: django"

msgid "foo"
msgstr "bar"
''')
        po = polib.pofile(data)
        self.assertTrue(isinstance(po, polib.POFile))
        self.assertEqual(po.encoding, 'utf-8')
        self.assertEqual(po[0].msgstr, u("bar"))

    def test_indented_pofile(self):
        """
        Test that an indented pofile returns a POFile instance.
        """
        po = polib.pofile('tests/test_indented.po')
        self.assertTrue(isinstance(po, polib.POFile))

    def test_pofile_and_mofile2(self):
        """
        Test that the pofile function returns a POFile instance.
        """
        po = polib.pofile('tests/test_utf8.po')
        self.assertTrue(isinstance(po, polib.POFile))

    def test_pofile_and_mofile3(self):
        """
        Test  that the mofile function returns a MOFile instance.
        """
        mo = polib.mofile('tests/test_utf8.mo')
        self.assertTrue(isinstance(mo, polib.MOFile))

    def test_pofile_and_mofile4(self):
        """
        Test that check_for_duplicates is passed to the instance.
        """
        po = polib.pofile('tests/test_iso-8859-15.po', check_for_duplicates=True,
                          autodetect_encoding=False, encoding='iso-8859-15')
        self.assertTrue(po.check_for_duplicates == True)

    def test_pofile_and_mofile5(self):
        """
        Test that detect_encoding works as expected.
        """
        po = polib.pofile('tests/test_iso-8859-15.po')
        self.assertTrue(po.encoding == 'ISO_8859-15')

    def test_pofile_and_mofile6(self):
        """
        Test that encoding is default_encoding when detect_encoding is False.
        """
        po = polib.pofile('tests/test_noencoding.po')
        self.assertTrue(po.encoding == 'utf-8')

    def test_pofile_and_mofile7(self):
        """
        Test that encoding is ok when encoding is explicitly given.
        """
        po = polib.pofile('tests/test_iso-8859-15.po', encoding='iso-8859-15')
        self.assertTrue(po.encoding == 'iso-8859-15')

    def test_pofile_and_mofile8(self):
        """
        Test that weird occurrences are correctly parsed.
        """
        po = polib.pofile('tests/test_weird_occurrences.po')
        self.assertEqual(len(po), 47)

    def test_pofile_and_mofile9(self):
        """
        Test that obsolete previous msgid are ignored
        """
        po = polib.pofile('tests/test_obsolete_previousmsgid.po')
        self.assertTrue(isinstance(po, polib.POFile))

    def test_pofile_and_mofile10(self):
        """
        Test mofile with a bytes object.
        """
        mo_bytes = b"\xde\x12\x04\x95\x00\x00\x00\x00\x05\x00\x00\x00\x1c\x00\x00\x00D\x00\x00\x00\x07\x00\x00\x00l\x00\x00\x00\x00\x00\x00\x00\x88\x00\x00\x00\x10\x00\x00\x00\x89\x00\x00\x00\x07\x00\x00\x00\x9a\x00\x00\x00\x0c\x00\x00\x00\xa2\x00\x00\x00\x0b\x00\x00\x00\xaf\x00\x00\x00(\x00\x00\x00\xbb\x00\x00\x00\n\x00\x00\x00\xe4\x00\x00\x00\x10\x00\x00\x00\xef\x00\x00\x00\x0f\x00\x00\x00\x00\x01\x00\x00\x16\x00\x00\x00\x10\x01\x00\x00\x01\x00\x00\x00\x03\x00\x00\x00\x05\x00\x00\x00\x00\x00\x00\x00\x04\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x00SNAR_BEZEICHNUNG\x00conform\x00inconclusive\x00non-conform\x00Content-Type: text/plain; charset=UTF-8\n\x00Feldfrucht\x00\xc3\x9cbereinstimmung\x00Nicht eindeutig\x00Keine \xc3\x9cbereinstimmung\x00"
        mo = polib.mofile(mo_bytes)
        self.assertTrue(isinstance(mo, polib.MOFile))

    def test_ufeff_data_pofile(self):
        """
        Test that an ufeff prefixed pofile returns a POFile instance.
        """
        data = u('''\ufeff# test for pofile/mofile with ufeff
msgid ""
msgstr ""
"Project-Id-Version: django"

msgid "foo"
msgstr "bar"
''')
        po = polib.pofile(data)
        self.assertTrue(isinstance(po, polib.POFile))

    def test_ufeff_pofile(self):
        """
        Test that an ufeff prefixed pofile returns a POFile instance.
        """
        po = polib.pofile('tests/test_ufeff.po')
        self.assertTrue(isinstance(po, polib.POFile))

    def test_previous_msgid_1(self):
        """
        Test previous msgid multiline.
        """
        po = polib.pofile('tests/test_previous_msgid.po')
        expected = "\nPartition table entries are not in disk order\n"
        self.assertEqual(
            po[0].previous_msgid,
            expected
        )

    def test_previous_msgid_2(self):
        """
        Test previous msgid single line.
        """
        po = polib.pofile('tests/test_previous_msgid.po')
        expected = "Partition table entries are not in disk order2\n"
        self.assertEqual(
            po[1].previous_msgid,
            expected
        )

    def test_previous_msgid_3(self):
        """
        Test saving empty previous msgid.
        """
        po = polib.pofile('tests/test_previous_msgid.po')
        fd, tmpfile = tempfile.mkstemp()
        os.close(fd)
        po.save(tmpfile)
        po = polib.pofile(tmpfile)
        os.remove(tmpfile)

        expected = ""
        self.assertEqual(
            po[2].previous_msgid,
            expected
        )

    def test_previous_msgctxt_1(self):
        """
        Test previous msgctxt multiline.
        """
        po = polib.pofile('tests/test_previous_msgid.po')
        expected = "\nSome message context"
        self.assertEqual(
            po[0].previous_msgctxt,
            expected
        )

    def test_previous_msgctxt_2(self):
        """
        Test previous msgctxt single line.
        """
        po = polib.pofile('tests/test_previous_msgid.po')
        expected = "Some message context"
        self.assertEqual(
            po[1].previous_msgctxt,
            expected
        )

    def test_unescaped_double_quote1(self):
        """
        Test that polib reports an error when unescaped double quote is found.
        """
        data = r'''
msgid "Some msgid with \"double\" quotes"
msgid "Some msgstr with "double\" quotes"
'''
        try:
            po = polib.pofile(data)
            self.fail("Unescaped quote not detected")
        except IOError:
            exc = sys.exc_info()[1]
            msg = 'Syntax error in po file (line 3): unescaped double quote found'
            self.assertEqual(str(exc), msg)

    def test_unescaped_double_quote2(self):
        """
        Test that polib reports an error when unescaped double quote is found.
        """
        data = r'''
msgid "Some msgid with \"double\" quotes"
msgstr ""
"Some msgstr with "double\" quotes"
'''
        try:
            po = polib.pofile(data)
            self.fail("Unescaped quote not detected")
        except IOError:
            exc = sys.exc_info()[1]
            msg = 'Syntax error in po file (line 4): unescaped double quote found'
            self.assertEqual(str(exc), msg)

    def test_unescaped_double_quote3(self):
        """
        Test that polib reports an error when unescaped double quote is found at the beginning of the string.
        """
        data = r'''
msgid "Some msgid with \"double\" quotes"
msgid ""Some msgstr with double\" quotes"
'''
        try:
            po = polib.pofile(data)
            self.fail("Unescaped quote not detected")
        except IOError:
            exc = sys.exc_info()[1]
            msg = 'Syntax error in po file (line 3): unescaped double quote found'
            self.assertEqual(str(exc), msg)

    def test_unescaped_double_quote4(self):
        """
        Test that polib reports an error when unescaped double quote is found at the beginning of the string.
        """
        data = r'''
msgid "Some msgid with \"double\" quotes"
msgstr ""
""Some msgstr with double\" quotes"
'''
        try:
            po = polib.pofile(data)
            self.fail("Unescaped quote not detected")
        except IOError:
            exc = sys.exc_info()[1]
            msg = 'Syntax error in po file (line 4): unescaped double quote found'
            self.assertEqual(str(exc), msg)

    def test_no_double_quote_delimiters(self):
        """
        Test that polib reports an error when a string is not delimited by double quotes.
        """
        invalid_msgstr = r'''
msgid "A"
msgstr *B"
'''
        invalid_msgid = r'''
msgid "A/
msgstr "B"
'''
        invalid_msgid_plural = r'''
msgid_plural A
msgstr "B"
'''
        invalid_msgstr_continuation = r'''
msgid "A"
msgstr ""
"B
'''
        for data in (invalid_msgid, invalid_msgid_plural, invalid_msgstr, invalid_msgstr_continuation):
            try:
                polib.pofile(data)
                self.fail("Strings not delimited by double quotes not detected")
            except IOError as ex:
                msg = 'string not delimited by double quotes'
                self.assertIn(msg, str(ex))

    def test_syntax_error1(self):
        """
        Test that syntax error is raised while processing a symbol parsing.
        """
        try:
            polib.pofile('tests/test_syntax_error1.po')
            self.fail("Syntax error not detected")
        except IOError:
            exc = sys.exc_info()[1]
            msg = "Syntax error in po file tests/test_syntax_error1.po (line 5)"
            self.assertEqual(str(exc), msg)

    def test_detect_encoding1(self):
        """
        Test that given encoding is returned when file has no encoding defined.
        """
        self.assertEqual(polib.detect_encoding('tests/test_noencoding.po'), 'utf-8')

    def test_detect_encoding2(self):
        """
        Test with a .pot file.
        """
        self.assertEqual(polib.detect_encoding('tests/test_merge.pot'), 'utf-8')

    def test_detect_encoding3(self):
        """
        Test with an utf8 .po file.
        """
        self.assertEqual(polib.detect_encoding('tests/test_utf8.po'), 'UTF-8')

    def test_detect_encoding4(self):
        """
        Test with utf8 data (no file).
        """
        if polib.PY3:
            f = open('tests/test_utf8.po', 'rb')
            data = str(f.read(), 'utf-8')
        else:
            f = open('tests/test_utf8.po', 'r')
            data = f.read()
        try:
            self.assertEqual(polib.detect_encoding(data), 'UTF-8')
        finally:
            f.close()

    def test_detect_encoding5(self):
        """
        Test with utf8 .mo file.
        """
        self.assertEqual(polib.detect_encoding('tests/test_utf8.mo', True), 'UTF-8')

    def test_detect_encoding6(self):
        """
        Test with iso-8859-15 .po file.
        """
        self.assertEqual(polib.detect_encoding('tests/test_iso-8859-15.po'), 'ISO_8859-15')

    def test_detect_encoding7(self):
        """
        Test with iso-8859-15 .mo file.
        """
        self.assertEqual(polib.detect_encoding('tests/test_iso-8859-15.mo', True), 'ISO_8859-15')

    def test_escape(self):
        """
        Tests the escape function.
        """
        self.assertEqual(
            polib.escape('\\t and \\n and \\r and " and \\ and \\\\ and \\v and \\b and \\f and \\a'),
            '\\\\t and \\\\n and \\\\r and \\" and \\\\ and \\\\\\\\ and \\\\v and \\\\b and \\\\f and \\\\a'
        )

    def test_unescape(self):
        """
        Tests the unescape function.
        """
        self.assertEqual(
            polib.unescape('\\t and \\n and \\r and \\" and \\\\ and \\v and \\b and \\f and \\a'),
            '\t and \n and \r and \" and \\ and \v and \b and \f and \a'
        )

    def test_pofile_with_subclass(self):
        """
        Test that the pofile function correctly returns an instance of the
        passed in class
        """
        class CustomPOFile(polib.POFile):
            pass

        pofile = polib.pofile('tests/test_indented.po', klass=CustomPOFile)
        self.assertEqual(pofile.__class__, CustomPOFile)

    def test_mofile_with_subclass(self):
        """
        Test that the mofile function correctly returns an instance of the
        passed in class
        """
        class CustomMOFile(polib.MOFile):
            pass

        mofile = polib.mofile('tests/test_utf8.mo', klass=CustomMOFile)
        self.assertEqual(mofile.__class__, CustomMOFile)

    def test_empty(self):
        po = polib.pofile('')
        self.assertEqual(po.__unicode__(), '#\nmsgid ""\nmsgstr ""\n')

    def test_linenum_1(self):
        po = polib.pofile('tests/test_utf8.po')
        self.assertEqual(po[0].linenum, 17)

    def test_linenum_2(self):
        po = polib.pofile('tests/test_utf8.po')
        self.assertEqual(po.find('XML text').linenum, 1798)

    def test_linenum_3(self):
        po = polib.pofile('tests/test_utf8.po')
        self.assertEqual(po[-1].linenum, 3477)

    def test_windows_path_occurrences(self):
        po = polib.pofile('tests/test_weird_occurrences.po')
        self.assertEqual(po[0].occurrences, [('C:\\foo\\bar.py', '12')])

    def test_is_file(self):
        self.assertTrue(polib._is_file(os.path.abspath(__file__)))
        self.assertFalse(polib._is_file('This is not a file !!!!'))
        self.assertFalse(polib._is_file(True))


class TestBaseFile(unittest.TestCase):
    """
    Tests for the _BaseFile class.
    """

    def test_append1(self):
        pofile = polib.pofile('tests/test_pofile_helpers.po')
        entry = polib.POEntry(msgid="Foo", msgstr="Bar", msgctxt="Some context")
        pofile.append(entry)
        self.assertTrue(entry in pofile)

    def test_append2(self):
        def add_duplicate():
            pofile = polib.pofile('tests/test_pofile_helpers.po', check_for_duplicates=True)
            pofile.append(polib.POEntry(msgid="and"))
        self.assertRaises(ValueError, add_duplicate)

    def test_append3(self):
        def add_duplicate():
            pofile = polib.pofile('tests/test_pofile_helpers.po', check_for_duplicates=True)
            pofile.append(polib.POEntry(msgid="and", msgctxt="some context"))
        self.assertRaises(ValueError, add_duplicate)

    def test_append4(self):
        pofile = polib.pofile('tests/test_pofile_helpers.po', check_for_duplicates=True)
        entry = polib.POEntry(msgid="and", msgctxt="some different context")
        pofile.append(entry)
        self.assertTrue(entry in pofile)

    def test_insert1(self):
        pofile = polib.pofile('tests/test_pofile_helpers.po')
        entry = polib.POEntry(msgid="Foo", msgstr="Bar", msgctxt="Some context")
        pofile.insert(0, entry)
        self.assertEqual(pofile[0], entry)

    def test_insert2(self):
        def add_duplicate():
            pofile = polib.pofile('tests/test_pofile_helpers.po', check_for_duplicates=True)
            pofile.insert(0, polib.POEntry(msgid="and", msgstr="y"))
        self.assertRaises(ValueError, add_duplicate)

    def test_metadata_as_entry(self):
        pofile = polib.pofile('tests/test_fuzzy_header.po')
        f = open('tests/test_fuzzy_header.po')
        lines  = f.readlines()[2:]
        f.close()
        self.assertEqual(pofile.metadata_as_entry().__unicode__(), "".join(lines))

    def test_find1(self):
        pofile = polib.pofile('tests/test_pofile_helpers.po')
        entry = pofile.find('and')
        self.assertEqual(entry.msgstr, u('y'))

    def test_find2(self):
        pofile = polib.pofile('tests/test_pofile_helpers.po')
        entry = pofile.find('pacote', by="msgstr")
        self.assertEqual(entry, None)

    def test_find3(self):
        pofile = polib.pofile('tests/test_pofile_helpers.po')
        entry = pofile.find('package', include_obsolete_entries=True)
        self.assertEqual(entry.msgstr, u('pacote'))

    def test_find4(self):
        pofile = polib.pofile('tests/test_utf8.po')
        entry1 = pofile.find('test context', msgctxt='@context1')
        entry2 = pofile.find('test context', msgctxt='@context2')
        self.assertEqual(entry1.msgstr, u('test context 1'))
        self.assertEqual(entry2.msgstr, u('test context 2'))

    def test_find5(self):
        pofile = polib.pofile('tests/test_msgctxt.po')
        entry1 = pofile.find('some string')
        entry2 = pofile.find('some string', msgctxt='Some message context')
        self.assertEqual(entry1.msgstr, u('une cha\u00eene sans contexte'))
        self.assertEqual(entry2.msgstr, u('une cha\u00eene avec contexte'))

    def test_save1(self):
        pofile = polib.POFile()
        self.assertRaises(IOError, pofile.save)

    def test_save2(self):
        fd, tmpfile = tempfile.mkstemp()
        os.close(fd)
        try:
            pofile = polib.POFile()
            pofile.save(tmpfile)
            pofile.save()
            self.assertTrue(os.path.isfile(tmpfile))
        finally:
            os.remove(tmpfile)

    def test_ordered_metadata(self):
        pofile = polib.pofile('tests/test_fuzzy_header.po')
        f = open('tests/test_fuzzy_header.po')
        lines  = f.readlines()[2:]
        f.close()
        mdata  = [
            ('Project-Id-Version', u('PACKAGE VERSION')),
            ('Report-Msgid-Bugs-To', u('')),
            ('POT-Creation-Date', u('2010-02-08 16:57+0100')),
            ('PO-Revision-Date', u('YEAR-MO-DA HO:MI+ZONE')),
            ('Last-Translator', u('FULL NAME <EMAIL@ADDRESS>')),
            ('Language-Team', u('LANGUAGE <LL@li.org>')),
            ('MIME-Version', u('1.0')),
            ('Content-Type', u('text/plain; charset=UTF-8')),
            ('Content-Transfer-Encoding', u('8bit')),
            ('X-Poedit-SearchPath-1', u('Foo')),
            ('X-Poedit-SearchPath-2', u('Bar')),
            ('X-Poedit-SearchPath-10', u('Baz')),
        ]
        self.assertEqual(pofile.ordered_metadata(), mdata)

    def test_unicode1(self):
        pofile  = polib.pofile('tests/test_merge_after.po')
        f = codecs.open('tests/test_merge_after.po', encoding='utf8')
        expected = f.read()
        f.close()
        self.assertEqual(pofile.__unicode__(), expected)

    def test_unicode2(self):
        pofile  = polib.pofile('tests/test_iso-8859-15.po')
        f = codecs.open('tests/test_iso-8859-15.po', encoding='iso-8859-15')
        expected = f.read()
        f.close()
        self.assertEqual(pofile.__unicode__(), expected)

    def test_str(self):
        pofile  = polib.pofile('tests/test_iso-8859-15.po')
        if polib.PY3:
            f = codecs.open('tests/test_iso-8859-15.po', encoding='iso-8859-15')
        else:
            f = open('tests/test_iso-8859-15.po')
        expected = f.read()
        f.close()
        self.assertEqual(str(pofile), expected)

    def test_wrapping(self):
        pofile  = polib.pofile('tests/test_wrap.po', wrapwidth=50)
        expected = r'''# test wrapping
msgid ""
msgstr ""

msgid "This line will not be wrapped"
msgstr ""

msgid ""
"Some line that contain special characters \" and"
" that \t is very, very, very long...: %s \n"
msgstr ""

msgid ""
"Some line that contain special characters "
"\"foobar\" and that contains whitespace at the "
"end          "
msgstr ""
'''
        self.assertEqual(str(pofile), expected)

    def test_sort(self):
        a1 = polib.POEntry(msgid='a1', occurrences=[('b.py', 1), ('b.py', 3)])
        a2 = polib.POEntry(msgid='a2')
        a3 = polib.POEntry(msgid='a1', occurrences=[('b.py', 1), ('b.py', 3)], obsolete=True)
        b1 = polib.POEntry(msgid='b1', occurrences=[('b.py', 1), ('b.py', 3)])
        b2 = polib.POEntry(msgid='b2', occurrences=[('d.py', 3), ('b.py', 1)])
        c1 = polib.POEntry(msgid='c1', occurrences=[('a.py', 1), ('b.py', 1)])
        c2 = polib.POEntry(msgid='c2', occurrences=[('a.py', 1), ('a.py', 3)])
        pofile = polib.POFile()
        pofile.append(b1)
        pofile.append(a3)
        pofile.append(a2)
        pofile.append(a1)
        pofile.append(b2)
        pofile.append(c1)
        pofile.append(c2)
        pofile.sort()
        expected = u('''#
msgid ""
msgstr ""

msgid "a2"
msgstr ""

#: a.py:1 a.py:3
msgid "c2"
msgstr ""

#: a.py:1 b.py:1
msgid "c1"
msgstr ""

#: b.py:1 b.py:3
msgid "a1"
msgstr ""

#: b.py:1 b.py:3
msgid "b1"
msgstr ""

#: d.py:3 b.py:1
msgid "b2"
msgstr ""

#~ msgid "a1"
#~ msgstr ""
''')
        self.assertEqual(pofile.__unicode__(), expected)

    def test_trailing_comment(self):
        pofile  = polib.pofile('tests/test_trailing_comment.po')
        expected = r'''#
msgid ""
msgstr "Content-Type: text/plain; charset=UTF-8\n"

msgid "foo"
msgstr "oof"
'''
        self.assertEqual(str(pofile), expected)

    def test_comment_ordering(self):
        """
        Test that "# " comments appear before "#. " comments when writing.
        """
        pofile  = polib.pofile('tests/test_comment_ordering.po')
        expected = r'''#
msgid ""
msgstr "Content-Type: text/plain; charset=UTF-8\n"

# First comment line
#. Second comment line
msgid "foo"
msgstr "oof"
'''
        self.assertEqual(str(pofile), expected)

class TestPoFile(unittest.TestCase):
    """
    Tests for PoFile class.
    """

    def test_save_as_mofile(self):
        """
        Test for the POFile.save_as_mofile() method.
        """
        msgfmt = which('msgfmt')
        if msgfmt is None:
            try:
                return unittest.skip('msgfmt is not installed')
            except AttributeError:
                return
        reffiles = ['tests/test_utf8.po', 'tests/test_iso-8859-15.po']
        encodings = ['utf-8', 'iso-8859-15']
        for reffile, encoding in zip(reffiles, encodings):
            fd, tmpfile1 = tempfile.mkstemp()
            os.close(fd)
            fd, tmpfile2 = tempfile.mkstemp()
            os.close(fd)
            po = polib.pofile(reffile, autodetect_encoding=False, encoding=encoding)
            po.save_as_mofile(tmpfile1)
            subprocess.call([msgfmt, '--no-hash', '--endianness=%s' % sys.byteorder, '-o', tmpfile2, reffile])
            try:
                f = open(tmpfile1, 'rb')
                s1 = f.read()
                f.close()
                f = open(tmpfile2, 'rb')
                s2 = f.read()
                f.close()
                self.assertEqual(s1, s2)
            finally:
                os.remove(tmpfile1)
                os.remove(tmpfile2)

    def test_merge(self):
        refpot = polib.pofile('tests/test_merge.pot')
        po = polib.pofile('tests/test_merge_before.po')
        po.merge(refpot)
        expected_po = polib.pofile('tests/test_merge_after.po')
        self.assertEqual(po, expected_po)

    def test_percent_translated(self):
        po = polib.pofile('tests/test_pofile_helpers.po')
        self.assertEqual(po.percent_translated(), 53)
        po = polib.POFile()
        self.assertEqual(po.percent_translated(), 100)

    def test_translated_entries(self):
        po = polib.pofile('tests/test_pofile_helpers.po')
        self.assertEqual(len(po.translated_entries()), 7)

    def test_untranslated_entries(self):
        po = polib.pofile('tests/test_pofile_helpers.po')
        self.assertEqual(len(po.untranslated_entries()), 4)

    def test_fuzzy_entries(self):
        po = polib.pofile('tests/test_pofile_helpers.po')
        self.assertEqual(len(po.fuzzy_entries()), 2)

    def test_obsolete_entries(self):
        po = polib.pofile('tests/test_pofile_helpers.po')
        self.assertEqual(len(po.obsolete_entries()), 4)

    def test_unusual_metadata_location(self):
        po = polib.pofile('tests/test_unusual_metadata_location.po')
        self.assertNotEqual(po.metadata, {})
        self.assertEqual(po.metadata['Content-Type'], 'text/plain; charset=UTF-8')

    def test_comment_starting_with_two_hashes(self):
        po = polib.pofile('tests/test_utf8.po')
        e = po.find("Some comment starting with two '#'", by='tcomment')
        self.assertTrue(isinstance(e, polib.POEntry))

    def test_word_garbage(self):
        po = polib.pofile('tests/test_word_garbage.po')
        e = po.find("Whatever", by='msgid')
        self.assertTrue(isinstance(e, polib.POEntry))

    def test_compare1(self):
        entry = polib.POEntry(msgid="foo")
        other = polib.POEntry(msgctxt="Some context", msgid="foo")
        self.assertNotEqual(entry, other)

    def test_compare2(self):
        entry = polib.POEntry(msgid="foo")
        other = polib.POEntry(msgid_plural="Plural", msgid="foo")
        self.assertNotEqual(entry, other)

    def test_compare3(self):
        entry = polib.POEntry(
            msgid_plural="Plural", msgid="foo"
        )
        other = polib.POEntry(
            msgid_plural="Plural", msgstr_plural={0: "Value", 1: "Values"}, msgid="foo"
        )
        self.assertNotEqual(entry, other)

    def test_compare4(self):
        entry = polib.POEntry(msgctxt="Some context", msgid="foo")
        other = polib.POEntry(msgctxt="Other context", msgid="foo")
        self.assertNotEqual(entry, other)

    def test_compare5(self):
        entry = polib.POEntry(msgid_plural="Some plural", msgid="foo")
        other = polib.POEntry(msgid_plural="Other plural", msgid="foo")
        self.assertNotEqual(entry, other)

    def test_compare6(self):
        entry = polib.POEntry(msgstr_plural={0: "Value", 1: "Values"}, msgid="foo")
        other = polib.POEntry(msgstr_plural={0: "Other value", 1: "Other values"}, msgid="foo")
        self.assertTrue(entry > other)

    def test_compare7(self):
        entry = polib.POEntry(msgstr="Some msgstr", msgid="foo")
        other = polib.POEntry(msgstr="Other msgstr", msgid="foo")
        self.assertTrue(entry >= other)
        self.assertNotEqual(entry, other)
        entry = polib.POEntry(msgstr="A msgstr", msgid="foo")
        other = polib.POEntry(msgstr="Other msgstr", msgid="foo")
        self.assertTrue(entry <= other)
        self.assertNotEqual(entry, other)

    def test_compare8(self):
        entry = polib.POEntry(
            msgctxt="Some context", msgid_plural="Plural",
            msgstr_plural={0: "Value", 1: "Values"}, msgid="foo"
        )
        other = polib.POEntry(
            msgctxt="Some context", msgid_plural="Plural",
            msgstr_plural={0: "Value", 1: "Values"}, msgid="foo"
        )
        self.assertEqual(entry, other)

    def test_fuzzy_obsolete(self):
        pofile = polib.pofile('tests/test_obsolete_is_not_fuzzy.po')
        self.assertEqual(len(pofile.fuzzy_entries()), 1)

    def test_set_fuzzy(self):
        pofile = polib.pofile('tests/test_set_fuzzy.po')
        pofile[0].fuzzy = True
        pofile[1].fuzzy = False
        self.assertTrue(pofile[0].fuzzy)
        self.assertFalse(pofile[1].fuzzy)

class TestMoFile(unittest.TestCase):
    """
    Tests for MoFile class.
    """
    def test_dummy_methods(self):
        """
        This is stupid and just here for code coverage.
        """
        mo = polib.MOFile()
        self.assertEqual(mo.percent_translated(), 100)
        self.assertEqual(mo.translated_entries(), mo)
        self.assertEqual(mo.untranslated_entries(), [])
        self.assertEqual(mo.fuzzy_entries(), [])
        self.assertEqual(mo.obsolete_entries(), [])

    def test_save_as_pofile(self):
        """
        Test for the MOFile.save_as_pofile() method.
        """
        fd, tmpfile = tempfile.mkstemp()
        os.close(fd)
        mo = polib.mofile('tests/test_utf8.mo', wrapwidth=78)
        mo.save_as_pofile(tmpfile)
        try:
            if polib.PY3:
                f = open(tmpfile, encoding='utf-8')
            else:
                f = open(tmpfile)
            s1 = f.read()
            f.close()
            if polib.PY3:
                f = open('tests/test_save_as_pofile.po', encoding='utf-8')
            else:
                f = open('tests/test_save_as_pofile.po')
            s2 = f.read()
            f.close()
            self.assertEqual(s1, s2)
        finally:
            os.remove(tmpfile)

    def test_msgctxt(self):
        #import pdb; pdb.set_trace()
        mo = polib.mofile('tests/test_msgctxt.mo')
        expected = u('''msgid ""
msgstr "Content-Type: text/plain; charset=UTF-8\u005cn"

msgctxt "Some message context"
msgid "some string"
msgstr "une cha\u00eene avec contexte"

msgctxt "Some other message context"
msgid "singular"
msgid_plural "plural"
msgstr[0] "singulier"
msgstr[1] "pluriel"

msgid "some string"
msgstr "une cha\u00eene sans contexte"
''')
        self.assertEqual(mo.__unicode__(), expected)

    def test_invalid_version(self):
        self.assertRaises(IOError, polib.mofile, 'tests/test_invalid_version.mo')
        polib.mofile('tests/test_version_1.1.mo')

    def test_no_header(self):
        mo = polib.mofile('tests/test_no_header.mo')
        expected = u('''msgid ""
msgstr ""

msgid "bar"
msgstr "rab"

msgid "foo"
msgstr "oof"
''')
        self.assertEqual(mo.__unicode__(), expected)


def _swap_mo_endianness(binary):
    _version, count = struct.unpack('<II', binary[4:12])
    out = struct.pack('>I', polib.MOFile.MAGIC)
    out += struct.pack('>iiiiii', *struct.unpack('<iiiiii', binary[4:28]))
    out += struct.pack('>%di' % (4 * count),
                       *struct.unpack('<%di' % (4 * count),
                                      binary[28:28 + 16 * count]))
    out += binary[28 + 16 * count:]
    return out


class TestPofileMofileMismatches(unittest.TestCase):
    """
    Tests for the runtime catalog verification helper.
    """

    def _source_po(self):
        po = polib.POFile()
        po.metadata = {
            'Content-Type': 'text/plain; charset=UTF-8',
            'Plural-Forms': 'nplurals=2; plural=(n != 1);',
        }
        po.append(polib.POEntry(
            msgid='plain', msgstr='simple',
            comment='translator comment', occurrences=[('src/app.py', 10)]))
        po.append(polib.POEntry(msgctxt='ctx-a', msgid='same', msgstr='ta'))
        po.append(polib.POEntry(msgctxt='ctx-b', msgid='same', msgstr='tb'))
        po.append(polib.POEntry(msgid='fuzzy', msgstr='stale',
                                flags=['fuzzy']))
        po.append(polib.POEntry(msgid='obsolete', msgstr='gone',
                                obsolete=True))
        po.append(polib.POEntry(msgid='untranslated', msgstr=''))
        po.append(polib.POEntry(
            msgid='apple', msgid_plural='apples',
            msgstr_plural={0: 'one apple', 1: 'many apples'}))
        return po

    def test_matching_catalogs_have_no_mismatch(self):
        po = self._source_po()
        mo = polib.mofile(po.to_binary())
        self.assertEqual(polib.pofile_mofile_mismatches(po, mo), [])

    def test_comments_occurrences_and_wrapping_are_ignored(self):
        po = self._source_po()
        mo = polib.mofile(po.to_binary())
        other = self._source_po()
        entry = other.find('plain')
        entry.comment = 'a brand new comment'
        entry.occurrences = [('elsewhere.py', 99)]
        # a wrapped msgstr parses back to the same logical string
        wrapped = polib.pofile(u('''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

msgid "long"
msgstr ""
"part one "
"part two"
'''))
        self.assertEqual(wrapped.find('long').msgstr, 'part one part two')
        self.assertEqual(polib.pofile_mofile_mismatches(other, mo), [])

    def test_fuzzy_and_obsolete_are_not_reported_missing(self):
        po = self._source_po()
        mo = polib.mofile(po.to_binary())
        keys = [m.key for m in polib.pofile_mofile_mismatches(po, mo)]
        self.assertNotIn('fuzzy', keys)
        self.assertNotIn('obsolete', keys)

    def test_contexts_are_distinct_keys(self):
        po = self._source_po()
        mo = polib.MOFile()
        for entry in polib.mofile(po.to_binary()):
            if entry.msgctxt != 'ctx-b':
                mo.append(entry)
        mismatches = polib.pofile_mofile_mismatches(po, mo)
        self.assertEqual(len(mismatches), 1)
        mismatch = mismatches[0]
        self.assertEqual(mismatch.kind, 'missing')
        self.assertEqual(mismatch.key, 'ctx-b\x04same')
        self.assertEqual(mismatch.msgctxt, 'ctx-b')

    def test_missing_runtime_key(self):
        po = self._source_po()
        po.append(polib.POEntry(msgid='later', msgstr='future'))
        mo = polib.mofile(self._source_po().to_binary())
        mismatches = polib.pofile_mofile_mismatches(po, mo)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].kind, 'missing')
        self.assertEqual(mismatches[0].key, 'later')

    def test_unexpected_runtime_key(self):
        po = self._source_po()
        mo = polib.mofile(po.to_binary())
        mo.append(polib.MOEntry(msgid='leftover', msgstr='still shipped'))
        mismatches = polib.pofile_mofile_mismatches(po, mo)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].kind, 'unexpected')
        self.assertEqual(mismatches[0].key, 'leftover')

    def test_translation_differs(self):
        po = self._source_po()
        po.find('plain').msgstr = 'changed'
        mo = polib.mofile(self._source_po().to_binary())
        mismatches = polib.pofile_mofile_mismatches(po, mo)
        self.assertEqual(len(mismatches), 1)
        mismatch = mismatches[0]
        self.assertEqual(mismatch.kind, 'translation')
        self.assertEqual(mismatch.expected, 'changed')
        self.assertEqual(mismatch.actual, 'simple')

    def test_missing_plural_branch_is_pinpointed(self):
        po = self._source_po()
        mo = polib.mofile(po.to_binary())
        del mo.find('apple').msgstr_plural[1]
        mismatches = polib.pofile_mofile_mismatches(
            po, polib.mofile(mo.to_binary()))
        self.assertEqual(len(mismatches), 1)
        mismatch = mismatches[0]
        self.assertEqual(mismatch.kind, 'plural_branch')
        self.assertEqual(mismatch.key, 'apple')
        self.assertEqual(mismatch.plural_index, 1)
        self.assertEqual(mismatch.expected, 'many apples')
        self.assertEqual(mismatch.actual, '')

    def test_changed_and_extra_plural_branches(self):
        po = self._source_po()
        mo = polib.mofile(po.to_binary())
        mo.find('apple').msgstr_plural[0] = 'UNE pomme'
        mismatches = polib.pofile_mofile_mismatches(
            po, polib.mofile(mo.to_binary()))
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].plural_index, 0)
        self.assertEqual(mismatches[0].actual, 'UNE pomme')

        mo = polib.mofile(self._source_po().to_binary())
        mo.find('apple').msgstr_plural[2] = 'extra branch'
        mismatches = polib.pofile_mofile_mismatches(
            self._source_po(), polib.mofile(mo.to_binary()))
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].plural_index, 2)
        self.assertEqual(mismatches[0].expected, '')
        self.assertEqual(mismatches[0].actual, 'extra branch')

    def test_byte_different_equivalent_packages_agree(self):
        po = self._source_po()
        mo_le = polib.mofile(po.to_binary())
        mo_be = polib.mofile(_swap_mo_endianness(po.to_binary()))
        self.assertNotEqual(po.to_binary(), _swap_mo_endianness(
            po.to_binary()))
        self.assertEqual(
            polib.pofile_mofile_mismatches(po, mo_le),
            polib.pofile_mofile_mismatches(po, mo_be))

        rebuilt = self._source_po()
        rebuilt.metadata['X-Rebuilt-By'] = 'another toolchain'
        mo_rebuilt = polib.mofile(rebuilt.to_binary())
        self.assertNotEqual(po.to_binary(), rebuilt.to_binary())
        self.assertEqual(polib.pofile_mofile_mismatches(po, mo_rebuilt), [])

    def test_empty_translation_kept_or_dropped_agrees(self):
        po = self._source_po()
        dropped = polib.mofile(po.to_binary())
        kept = polib.MOFile()
        for entry in po:
            if entry.obsolete or entry.fuzzy:
                continue
            kept.append(polib.MOEntry(
                msgid=entry.msgid, msgstr=entry.msgstr,
                msgid_plural=entry.msgid_plural,
                msgstr_plural=dict(entry.msgstr_plural),
                msgctxt=entry.msgctxt))
        self.assertNotEqual(len(kept), len(dropped))
        self.assertEqual(
            polib.pofile_mofile_mismatches(po, kept),
            polib.pofile_mofile_mismatches(po, dropped))

    def test_accepts_strings_and_instances(self):
        po_text = u('''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

msgid "foo"
msgstr "bar"
''')
        po = polib.pofile(po_text)
        self.assertEqual(
            polib.pofile_mofile_mismatches(po_text, po.to_binary()), [])
        self.assertEqual(
            polib.pofile_mofile_mismatches(po, polib.mofile(po.to_binary())),
            [])

    def test_mismatch_representation(self):
        mismatch = polib.POFileEntryMismatch(
            'apple', 'plural_branch', expected='many apples', actual='',
            plural_index=1)
        self.assertEqual(
            mismatch,
            polib.POFileEntryMismatch(
                'apple', 'plural_branch', expected='many apples', actual='',
                plural_index=1))
        self.assertNotEqual(mismatch, polib.POFileEntryMismatch(
            'apple', 'plural_branch', actual='x', plural_index=1))
        self.assertIn('plural branch differs', str(mismatch))
        self.assertIn("'apple'", repr(mismatch))
        self.assertEqual(hash(mismatch), hash(polib.POFileEntryMismatch(
            'apple', 'plural_branch', plural_index=1)))


if __name__ == '__main__':
    unittest.main()

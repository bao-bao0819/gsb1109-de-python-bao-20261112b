# -*- coding: utf-8 -*-
# Record Order in BIFF8
#   Workbook Globals Substream
#       BOF Type = workbook globals
#       Interface Header
#       MMS
#       Interface End
#       WRITEACCESS
#       CODEPAGE
#       DSF
#       TABID
#       FNGROUPCOUNT
#       Workbook Protection Block
#             WINDOWPROTECT
#             PROTECT
#             PASSWORD
#             PROT4REV
#             PROT4REVPASS
#       BACKUP
#       HIDEOBJ
#       WINDOW1
#       DATEMODE
#       PRECISION
#       REFRESHALL
#       BOOKBOOL
#       FONT +
#       FORMAT *
#       XF +
#       STYLE +
#     ? PALETTE
#       USESELFS
#
#       BOUNDSHEET +
#
#       COUNTRY
#     ? Link Table
#       SST
#       ExtSST
#       EOF

import six

from . import BIFFRecords, Style


class Workbook(object):
    """
    This is a class representing a workbook and all its contents. When creating
    Excel files with xlwt, you will normally start by instantiating an
    object of this class.
    """

    #################################################################
    ## Constructor
    #################################################################
    def __init__(self, encoding='ascii', style_compression=0):
        self.encoding = encoding
        self.__owner = 'None'
        self.__country_code = None # 0x07 is Russia :-)
        self.__wnd_protect = 0
        self.__obj_protect = 0
        self.__protect = 0
        self.__backup_on_save = 0
        # for WINDOW1 record
        self.__hpos_twips = 0x01E0
        self.__vpos_twips = 0x005A
        self.__width_twips = 0x3FCF
        self.__height_twips = 0x2A4E
        self.__custom_palette_b8 = None

        self.__active_sheet = 0
        self.__first_tab_index = 0
        self.__selected_tabs = 0x01
        self.__tab_width_twips = 0x0258

        self.__wnd_hidden = 0
        self.__wnd_mini = 0
        self.__hscroll_visible = 1
        self.__vscroll_visible = 1
        self.__tabs_visible = 1

        self.__styles = Style.StyleCollection(style_compression)

        self.__dates_1904 = 0
        self.__use_cell_values = 1

        self.__sst = BIFFRecords.SharedStringTable(self.encoding)

        self.__worksheets = []
        self.__worksheet_idx_from_name = {}
        self.__last_valid_sheet_names = []
        self.__sheet_refs = {}
        self._supbook_xref = {}
        self._xcall_xref = {}
        self._ownbook_supbookx = None
        self._ownbook_supbook_ref = None
        self._xcall_supbookx = None
        self._xcall_supbook_ref = None



    #################################################################
    ## Properties, "getters", "setters"
    #################################################################

    def get_style_stats(self):
        return self.__styles.stats[:]

    def set_owner(self, value):
        self.__owner = value

    def get_owner(self):
        return self.__owner

    owner = property(get_owner, set_owner)

    #################################################################

    def set_country_code(self, value):
        self.__country_code = value

    def get_country_code(self):
        return self.__country_code

    country_code = property(get_country_code, set_country_code)

    #################################################################

    def set_wnd_protect(self, value):
        self.__wnd_protect = int(value)

    def get_wnd_protect(self):
        return bool(self.__wnd_protect)

    wnd_protect = property(get_wnd_protect, set_wnd_protect)

    #################################################################

    def set_obj_protect(self, value):
        self.__obj_protect = int(value)

    def get_obj_protect(self):
        return bool(self.__obj_protect)

    obj_protect = property(get_obj_protect, set_obj_protect)

    #################################################################

    def set_protect(self, value):
        self.__protect = int(value)

    def get_protect(self):
        return bool(self.__protect)

    protect = property(get_protect, set_protect)

    #################################################################

    def set_backup_on_save(self, value):
        self.__backup_on_save = int(value)

    def get_backup_on_save(self):
        return bool(self.__backup_on_save)

    backup_on_save = property(get_backup_on_save, set_backup_on_save)

    #################################################################

    def set_hpos(self, value):
        self.__hpos_twips = value & 0xFFFF

    def get_hpos(self):
        return self.__hpos_twips

    hpos = property(get_hpos, set_hpos)

    #################################################################

    def set_vpos(self, value):
        self.__vpos_twips = value & 0xFFFF

    def get_vpos(self):
        return self.__vpos_twips

    vpos = property(get_vpos, set_vpos)

    #################################################################

    def set_width(self, value):
        self.__width_twips = value & 0xFFFF

    def get_width(self):
        return self.__width_twips

    width = property(get_width, set_width)

    #################################################################

    def set_height(self, value):
        self.__height_twips = value & 0xFFFF

    def get_height(self):
        return self.__height_twips

    height = property(get_height, set_height)

    #################################################################

    def set_active_sheet(self, value):
        self.__active_sheet = value & 0xFFFF
        self.__first_tab_index = self.__active_sheet

    def get_active_sheet(self):
        return self.__active_sheet

    active_sheet = property(get_active_sheet, set_active_sheet)

    #################################################################

    def set_tab_width(self, value):
        self.__tab_width_twips = value & 0xFFFF

    def get_tab_width(self):
        return self.__tab_width_twips

    tab_width = property(get_tab_width, set_tab_width)

    #################################################################

    def set_wnd_visible(self, value):
        self.__wnd_hidden = int(not value)

    def get_wnd_visible(self):
        return not bool(self.__wnd_hidden)

    wnd_visible = property(get_wnd_visible, set_wnd_visible)

    #################################################################

    def set_wnd_mini(self, value):
        self.__wnd_mini = int(value)

    def get_wnd_mini(self):
        return bool(self.__wnd_mini)

    wnd_mini = property(get_wnd_mini, set_wnd_mini)

    #################################################################

    def set_hscroll_visible(self, value):
        self.__hscroll_visible = int(value)

    def get_hscroll_visible(self):
        return bool(self.__hscroll_visible)

    hscroll_visible = property(get_hscroll_visible, set_hscroll_visible)

    #################################################################

    def set_vscroll_visible(self, value):
        self.__vscroll_visible = int(value)

    def get_vscroll_visible(self):
        return bool(self.__vscroll_visible)

    vscroll_visible = property(get_vscroll_visible, set_vscroll_visible)

    #################################################################

    def set_tabs_visible(self, value):
        self.__tabs_visible = int(value)

    def get_tabs_visible(self):
        return bool(self.__tabs_visible)

    tabs_visible = property(get_tabs_visible, set_tabs_visible)

    #################################################################

    def set_dates_1904(self, value):
        self.__dates_1904 = int(value)

    def get_dates_1904(self):
        return bool(self.__dates_1904)

    dates_1904 = property(get_dates_1904, set_dates_1904)

    #################################################################

    def set_use_cell_values(self, value):
        self.__use_cell_values = int(value)

    def get_use_cell_values(self):
        return bool(self.__use_cell_values)

    use_cell_values = property(get_use_cell_values, set_use_cell_values)

    #################################################################

    def get_default_style(self):
        return self.__styles.default_style

    default_style = property(get_default_style)

    #################################################################

    def set_colour_RGB(self, colour_index, red, green, blue):
        if not(8 <= colour_index <= 63):
            raise Exception("set_colour_RGB: colour_index (%d) not in range(8, 64)" %
                    colour_index)
        if min(red, green, blue) < 0 or max(red, green, blue) > 255:
            raise Exception("set_colour_RGB: colour values (%d,%d,%d) must be in range(0, 256)"
                    % (red, green, blue))
        if self.__custom_palette_b8 is None:
            self.__custom_palette_b8 = list(Style.excel_default_palette_b8)
        # User-defined Palette starts at colour index 8,
        # so subtract 8 from colour_index when placing in palette
        palette_index = colour_index - 8
        self.__custom_palette_b8[palette_index] = red << 24 | green << 16 | blue << 8

    ##################################################################
    ## Methods
    ##################################################################

    def add_style(self, style):
        return self.__styles.add(style)

    def add_font(self, font):
        return self.__styles.add_font(font)

    def add_str(self, s):
        return self.__sst.add_str(s)

    def del_str(self, sst_idx):
        self.__sst.del_str(sst_idx)

    def str_index(self, s):
        return self.__sst.str_index(s)

    def add_rt(self, rt):
        return self.__sst.add_rt(rt)

    def rt_index(self, rt):
        return self.__sst.rt_index(rt)

    def add_sheet(self, sheetname, cell_overwrite_ok=False):
        """
        This method is used to create Worksheets in a Workbook.

        :param sheetname:

          The name to use for this sheet, as it will appear in the
          tabs at the bottom of the Excel application.

        :param cell_overwrite_ok:

          If ``True``, cells in the added worksheet will not raise an
          exception if written to more than once.

        :return:

          The :class:`~xlwt.Worksheet.Worksheet` that was added.

        """
        from . import Utils
        from .Worksheet import Worksheet
        if not isinstance(sheetname, six.text_type):
            sheetname = sheetname.decode(self.encoding)
        if not Utils.valid_sheet_name(sheetname):
            raise Exception("invalid worksheet name %r" % sheetname)
        lower_name = sheetname.lower()
        if lower_name in self.__worksheet_idx_from_name:
            raise Exception("duplicate worksheet name %r" % sheetname)
        self.__worksheet_idx_from_name[lower_name] = len(self.__worksheets)
        self.__worksheets.append(Worksheet(sheetname, self, cell_overwrite_ok))
        self._refresh_valid_sheet_names()
        return self.__worksheets[-1]

    def get_sheet(self, sheet):
        if isinstance(sheet, six.integer_types):
            return self.__worksheets[sheet]
        elif isinstance(sheet, six.string_types):
            sheetnum = self.sheet_index(sheet)
            return self.__worksheets[sheetnum]
        else:
            raise Exception("sheet must be integer or string")

    def sheet_index(self, sheetname):
        try:
            sheetnum = self.__worksheet_idx_from_name[sheetname.lower()]
        except KeyError:
            self.raise_bad_sheetname(sheetname)

        return sheetnum

    def _rebuild_sheet_name_index(self):
        self.__worksheet_idx_from_name = {}
        for idx, sheet in enumerate(self.__worksheets):
            self.__worksheet_idx_from_name[sheet.name.lower()] = idx

    def _refresh_valid_sheet_names(self):
        names = [sheet.name for sheet in self.__worksheets]
        if not self._valid_sheet_names(names):
            return
        self.__last_valid_sheet_names = names

    def _valid_sheet_names(self, names):
        if self._sheet_name_conflict(names) is not None:
            return False
        try:
            self._validate_sheet_names(names)
        except ValueError:
            return False
        return True

    def _biff_sheet_names(self):
        """
        Names to write into BOUNDSHEET records.

        If the live names clash in Excel's view (a name made valid by
        bypassing :meth:`rename_sheets` validation), fall back to the last
        set of names that validated, so the workbook remains openable under
        its pre-rename names.
        """
        names = [sheet.name for sheet in self.__worksheets]
        if self._valid_sheet_names(names):
            return names
        if len(self.__last_valid_sheet_names) == len(names):
            return list(self.__last_valid_sheet_names)
        return names

    def _validate_sheet_names(self, names):
        from . import Utils

        for name in names:
            if not isinstance(name, six.text_type):
                raise ValueError("worksheet names must be strings")
            if not Utils.valid_sheet_name(name):
                raise ValueError("invalid worksheet name %r" % name)

    def _sheet_name_conflict(self, names):
        lowered = {}
        for idx, name in enumerate(names):
            key = name.lower()
            if key in lowered:
                return ("duplicate worksheet name %r (case-insensitive clash "
                        "with worksheet %d)" % (name, lowered[key]))
            lowered[key] = idx
        return None

    def _apply_sheet_rename(self, targets, strict=False):
        """
        Validate ``{worksheet_index: new_name}`` as one group, then apply it.
        Nothing is changed if validation fails. Returns ``True`` when the
        group was applied.

        Malformed names always raise. A result that Excel would reject as a
        case-insensitive duplicate is reported by returning ``False`` (or
        raised when ``strict`` is true); in either case the workbook keeps
        its pre-rename names and remains saveable.
        """
        result_names = [sheet.name for sheet in self.__worksheets]
        for idx, new_name in targets.items():
            result_names[idx] = new_name
        self._validate_sheet_names(result_names)
        conflict = self._sheet_name_conflict(result_names)
        if conflict is not None:
            if strict:
                raise ValueError(conflict)
            return False
        for idx, new_name in targets.items():
            self.__worksheets[idx]._set_name(new_name)
        self._rebuild_sheet_name_index()
        self._refresh_valid_sheet_names()
        return True

    def _rename_one_sheet(self, sheet, new_name):
        if not isinstance(new_name, six.text_type):
            new_name = new_name.decode(self.encoding)
        for idx, candidate in enumerate(self.__worksheets):
            if candidate is sheet:
                self._apply_sheet_rename({idx: new_name}, strict=True)
                return
        raise ValueError("worksheet does not belong to this workbook")

    def _resolve_rename_targets(self, names):
        from .Worksheet import Worksheet

        worksheets = self.__worksheets
        by_lower_name = {}
        for idx, sheet in enumerate(worksheets):
            by_lower_name[sheet.name.lower()] = idx

        targets = {}
        seen_sources = set()
        for pair in names:
            if isinstance(pair, six.string_types) or not hasattr(pair, '__len__'):
                raise ValueError(
                    "each rename must be an (old_name, new_name) pair")
            if len(pair) != 2:
                raise ValueError(
                    "each rename must be an (old_name, new_name) pair")
            source, new_name = pair

            if isinstance(source, Worksheet):
                idx = None
                for candidate_idx, candidate in enumerate(worksheets):
                    if candidate is source:
                        idx = candidate_idx
                        break
                if idx is None:
                    raise ValueError("worksheet does not belong to this workbook")
                source_key = ('worksheet', idx)
            elif isinstance(source, six.integer_types):
                idx = source
                if not 0 <= idx < len(worksheets):
                    raise ValueError("worksheet index out of range %r" % source)
                source_key = ('worksheet', idx)
            elif isinstance(source, six.string_types):
                key = source.lower()
                if key not in by_lower_name:
                    self.raise_bad_sheetname(source)
                idx = by_lower_name[key]
                source_key = ('name', key)
            else:
                raise TypeError("worksheet must be a name, index or Worksheet")

            if source_key in seen_sources:
                raise ValueError("worksheet %r is renamed more than once" % source)
            seen_sources.add(source_key)
            if idx in targets:
                raise ValueError("worksheet %r is renamed more than once" % source)
            if not isinstance(new_name, six.string_types):
                raise TypeError("worksheet names must be strings")
            targets[idx] = new_name

        return targets

    def rename_sheets(self, names, new_name=None, validate=True, strict=True):
        """
        Rename a group of worksheets in a single, atomic operation.

        :param names:

          A mapping of ``{old_name: new_name}``, a sequence of
          ``(old_name, new_name)`` pairs, or a single ``(old_name,
          new_name)`` pair. The old worksheet may be given by name (matched
          case-insensitively, as formulas do), by zero-based index, or by the
          :class:`~xlwt.Worksheet.Worksheet` object itself. A sequence of new
          names, one for each worksheet in workbook order, is also accepted.

        :param validate:

          When ``True`` (the default), the resulting set of names is checked
          against Excel's worksheet naming rules and for case-insensitive
          duplicates. All renames are applied together only after the whole
          group validates. If any name is invalid or the group clashes, the
          workbook is left untouched and can still be saved under its old
          names.

        :param strict:

          When ``True`` (the default), an Excel-style duplicate clash raises
          :class:`ValueError` before anything is applied. Invalid names
          always raise.

        Renaming is resolved against the worksheets as they were before the
        call, so swaps and rotation chains such as
        ``{'Jan': 'Feb', 'Feb': 'Mar', 'Mar': 'Jan'}`` work without temporary
        names. Formula references keep pointing at the worksheet they were
        written against, because xlwt binds them to worksheet indexes rather
        than to name text. Text inside formula string constants and external
        workbook/sheet names are never rewritten.

        :return: ``True`` if the group was applied. With ``strict=False`` a
          duplicate clash returns ``False`` and leaves the workbook untouched.
        """
        if isinstance(new_name, bool) and validate is True:
            validate = new_name
            new_name = None
        if new_name is not None:
            if isinstance(new_name, (list, tuple)):
                entries = list(zip(names, new_name))
                if len(entries) != len(new_name):
                    raise ValueError(
                        "old and new names must have equal length")
            elif isinstance(new_name, six.string_types):
                entries = [(names, new_name)]
            else:
                raise TypeError("worksheet names must be strings")
        elif isinstance(names, six.string_types):
            raise ValueError("a new name is required")
        elif hasattr(names, 'items'):
            entries = list(names.items())
        else:
            entries = list(names)
            if not entries:
                return
            if isinstance(entries[0], six.string_types):
                if not all(isinstance(item, six.string_types)
                           for item in entries):
                    raise ValueError("mixed worksheet name types")
                if len(entries) == len(self.__worksheets):
                    entries = list(zip(self.__worksheets, entries))
                else:
                    raise ValueError(
                        "expected one new name for each worksheet; pass "
                        "(old, new) pairs to rename individual worksheets")

        if not entries:
            return

        targets = self._resolve_rename_targets(entries)
        if validate:
            return self._apply_sheet_rename(targets, strict=strict)
        for idx, new_name in targets.items():
            self.__worksheets[idx]._set_name(new_name)
        self._rebuild_sheet_name_index()
        self._refresh_valid_sheet_names()
        return True

    def rename_sheet(self, old_name, new_name=None, validate=True,
                     strict=True):
        """
        Rename one worksheet (or, when ``old_name`` is a mapping or a
        sequence of pairs, a whole group). This is a convenience wrapper
        around :meth:`rename_sheets`.
        """
        if new_name is None and not isinstance(old_name, six.string_types):
            if hasattr(old_name, 'items') or (
                    isinstance(old_name, (list, tuple)) and old_name and
                    not isinstance(old_name[0], six.string_types)):
                strict = False
        return self.rename_sheets(old_name, new_name, validate=validate,
                                  strict=strict)

    rename_worksheet = rename_sheet

    rename_worksheets = rename_sheets

    rename_sheet_group = rename_sheets

    def swap_sheets(self, name1, name2, strict=False):
        """
        Swap the names of two worksheets in one atomic step.
        """
        return self.rename_sheets({name1: name2, name2: name1}, strict=strict)

    def get_sheet_names(self):
        """
        Return the worksheet names in workbook order.
        """
        return [sheet.name for sheet in self.__worksheets]

    def set_sheet_names(self, names):
        """
        Rename every worksheet at once from a sequence of new names given
        in workbook order. Equivalent to
        ``rename_sheets(list_of_names)``.
        """
        self.rename_sheets(names)

    sheet_names = property(get_sheet_names, set_sheet_names)

    def raise_bad_sheetname(self, sheetname):
        raise Exception("Formula: unknown sheet name %s" % sheetname)

    def convert_sheetindex(self, strg_ref, n_sheets):
        idx = int(strg_ref)
        if 0 <= idx < n_sheets:
            return idx
        msg = "Formula: sheet index (%s) >= number of sheets (%d)" % (strg_ref, n_sheets)
        raise Exception(msg)

    def _get_supbook_index(self, tag):
        if tag in self._supbook_xref:
            return self._supbook_xref[tag]
        self._supbook_xref[tag] = idx = len(self._supbook_xref)
        return idx

    def setup_ownbook(self):
        self._ownbook_supbookx = self._get_supbook_index(('ownbook', 0))
        self._ownbook_supbook_ref = None
        reference = (self._ownbook_supbookx, 0xFFFE, 0xFFFE)
        if reference in self.__sheet_refs:
            raise Exception("can't happen")
        self.__sheet_refs[reference] = self._ownbook_supbook_ref = len(self.__sheet_refs)

    def setup_xcall(self):
        self._xcall_supbookx = self._get_supbook_index(('xcall', 0))
        self._xcall_supbook_ref = None
        reference = (self._xcall_supbookx, 0xFFFE, 0xFFFE)
        if reference in self.__sheet_refs:
            raise Exception("can't happen")
        self.__sheet_refs[reference] = self._xcall_supbook_ref = len(self.__sheet_refs)

    def add_sheet_reference(self, formula):
        patches = []
        n_sheets = len(self.__worksheets)
        sheet_refs, xcall_refs = formula.get_references()

        for ref0, ref1, offset in sheet_refs:
            if not ref0.isdigit():
                try:
                    ref0n = self.__worksheet_idx_from_name[ref0.lower()]
                except KeyError:
                    self.raise_bad_sheetname(ref0)
            else:
                ref0n = self.convert_sheetindex(ref0, n_sheets)
            if ref1 == ref0:
                ref1n = ref0n
            elif not ref1.isdigit():
                try:
                    ref1n = self.__worksheet_idx_from_name[ref1.lower()]
                except KeyError:
                    self.raise_bad_sheetname(ref1)
            else:
                ref1n = self.convert_sheetindex(ref1, n_sheets)
            if ref1n < ref0n:
                msg = "Formula: sheets out of order; %r:%r -> (%d, %d)" \
                    % (ref0, ref1, ref0n, ref1n)
                raise Exception(msg)
            if self._ownbook_supbookx is None:
                self.setup_ownbook()
            reference = (self._ownbook_supbookx, ref0n, ref1n)
            if reference in self.__sheet_refs:
                patches.append((offset, self.__sheet_refs[reference]))
            else:
                nrefs = len(self.__sheet_refs)
                if nrefs > 65535:
                    raise Exception('More than 65536 inter-sheet references')
                self.__sheet_refs[reference] = nrefs
                patches.append((offset, nrefs))

        for funcname, offset in xcall_refs:
            if self._ownbook_supbookx is None:
                self.setup_ownbook()
            if self._xcall_supbookx is None:
                self.setup_xcall()
            # print funcname, self._supbook_xref
            patches.append((offset, self._xcall_supbook_ref))
            if not isinstance(funcname, six.text_type):
                funcname = funcname.decode(self.encoding)
            if funcname in self._xcall_xref:
                idx = self._xcall_xref[funcname]
            else:
                self._xcall_xref[funcname] = idx = len(self._xcall_xref)
            patches.append((offset + 2, idx + 1))

        formula.patch_references(patches)

    ##################################################################
    ## BIFF records generation
    ##################################################################

    def __bof_rec(self):
        return BIFFRecords.Biff8BOFRecord(BIFFRecords.Biff8BOFRecord.BOOK_GLOBAL).get()

    def __eof_rec(self):
        return BIFFRecords.EOFRecord().get()

    def __intf_hdr_rec(self):
        return BIFFRecords.InteraceHdrRecord().get()

    def __intf_end_rec(self):
        return BIFFRecords.InteraceEndRecord().get()

    def __intf_mms_rec(self):
        return BIFFRecords.MMSRecord().get()

    def __write_access_rec(self):
        return BIFFRecords.WriteAccessRecord(self.__owner).get()

    def __wnd_protect_rec(self):
        return BIFFRecords.WindowProtectRecord(self.__wnd_protect).get()

    def __obj_protect_rec(self):
        return BIFFRecords.ObjectProtectRecord(self.__obj_protect).get()

    def __protect_rec(self):
        return BIFFRecords.ProtectRecord(self.__protect).get()

    def __password_rec(self):
        return BIFFRecords.PasswordRecord().get()

    def __prot4rev_rec(self):
        return BIFFRecords.Prot4RevRecord().get()

    def __prot4rev_pass_rec(self):
        return BIFFRecords.Prot4RevPassRecord().get()

    def __backup_rec(self):
        return BIFFRecords.BackupRecord(self.__backup_on_save).get()

    def __hide_obj_rec(self):
        return BIFFRecords.HideObjRecord().get()

    def __window1_rec(self):
        flags = 0
        flags |= (self.__wnd_hidden) << 0
        flags |= (self.__wnd_mini) << 1
        flags |= (self.__hscroll_visible) << 3
        flags |= (self.__vscroll_visible) << 4
        flags |= (self.__tabs_visible) << 5

        return BIFFRecords.Window1Record(self.__hpos_twips, self.__vpos_twips,
                                self.__width_twips, self.__height_twips,
                                flags,
                                self.__active_sheet, self.__first_tab_index,
                                self.__selected_tabs, self.__tab_width_twips).get()

    def __codepage_rec(self):
        return BIFFRecords.CodepageBiff8Record().get()

    def __country_rec(self):
        if not self.__country_code:
            return b''
        return BIFFRecords.CountryRecord(self.__country_code, self.__country_code).get()

    def __dsf_rec(self):
        return BIFFRecords.DSFRecord().get()

    def __tabid_rec(self):
        return BIFFRecords.TabIDRecord(len(self.__worksheets)).get()

    def __fngroupcount_rec(self):
        return BIFFRecords.FnGroupCountRecord().get()

    def __datemode_rec(self):
        return BIFFRecords.DateModeRecord(self.__dates_1904).get()

    def __precision_rec(self):
        return BIFFRecords.PrecisionRecord(self.__use_cell_values).get()

    def __refresh_all_rec(self):
        return BIFFRecords.RefreshAllRecord().get()

    def __bookbool_rec(self):
        return BIFFRecords.BookBoolRecord().get()

    def __all_fonts_num_formats_xf_styles_rec(self):
        return self.__styles.get_biff_data()

    def __palette_rec(self):
        if self.__custom_palette_b8 is None:
            return b''
        info = BIFFRecords.PaletteRecord(self.__custom_palette_b8).get()
        return info

    def __useselfs_rec(self):
        return BIFFRecords.UseSelfsRecord().get()

    def __boundsheets_rec(self, data_len_before, data_len_after, sheet_biff_lens):
        #  .................................
        # BOUNDSEHEET0
        # BOUNDSEHEET1
        # BOUNDSEHEET2
        # ..................................
        # WORKSHEET0
        # WORKSHEET1
        # WORKSHEET2
        boundsheets_len = 0
        biff_names = self._biff_sheet_names()
        for sheet, sheet_name in zip(self.__worksheets, biff_names):
            boundsheets_len += len(BIFFRecords.BoundSheetRecord(
                0x00, sheet.visibility, sheet_name, self.encoding
                ).get())

        start = data_len_before + boundsheets_len + data_len_after

        result = b''
        for sheet_biff_len, sheet, sheet_name in zip(
                sheet_biff_lens, self.__worksheets, biff_names):
            result += BIFFRecords.BoundSheetRecord(
                start, sheet.visibility, sheet_name, self.encoding
                ).get()
            start += sheet_biff_len
        return result

    def __all_links_rec(self):
        pieces = []
        temp = [(idx, tag) for tag, idx in self._supbook_xref.items()]
        temp.sort()
        for idx, tag in temp:
            stype, snum = tag
            if stype == 'ownbook':
                rec = BIFFRecords.InternalReferenceSupBookRecord(len(self.__worksheets)).get()
                pieces.append(rec)
            elif stype == 'xcall':
                rec = BIFFRecords.XcallSupBookRecord().get()
                pieces.append(rec)
                temp = [(idx, name) for name, idx in self._xcall_xref.items()]
                temp.sort()
                for idx, name in temp:
                    rec = BIFFRecords.ExternnameRecord(
                        options=0, index=0, name=name, fmla='\x02\x00\x1c\x17').get()
                    pieces.append(rec)
            else:
                raise Exception('unknown supbook stype %r' % stype)
        if len(self.__sheet_refs) > 0:
            # get references in index order
            temp = [(idx, ref) for ref, idx in self.__sheet_refs.items()]
            temp.sort()
            temp = [ref for idx, ref in temp]
            externsheet_record = BIFFRecords.ExternSheetRecord(temp).get()
            pieces.append(externsheet_record)
        return b''.join(pieces)

    def __sst_rec(self):
        return self.__sst.get_biff_record()

    def __ext_sst_rec(self, abs_stream_pos):
        return b''
        #return BIFFRecords.ExtSSTRecord(abs_stream_pos, self.sst_record.str_placement,
        #self.sst_record.portions_len).get()

    def get_biff_data(self):
        before = b''
        before += self.__bof_rec()
        before += self.__intf_hdr_rec()
        before += self.__intf_mms_rec()
        before += self.__intf_end_rec()
        before += self.__write_access_rec()
        before += self.__codepage_rec()
        before += self.__dsf_rec()
        before += self.__tabid_rec()
        before += self.__fngroupcount_rec()
        before += self.__wnd_protect_rec()
        before += self.__protect_rec()
        before += self.__obj_protect_rec()
        before += self.__password_rec()
        before += self.__prot4rev_rec()
        before += self.__prot4rev_pass_rec()
        before += self.__backup_rec()
        before += self.__hide_obj_rec()
        before += self.__window1_rec()
        before += self.__datemode_rec()
        before += self.__precision_rec()
        before += self.__refresh_all_rec()
        before += self.__bookbool_rec()
        before += self.__all_fonts_num_formats_xf_styles_rec()
        before += self.__palette_rec()
        before += self.__useselfs_rec()

        country            = self.__country_rec()
        all_links          = self.__all_links_rec()

        shared_str_table   = self.__sst_rec()
        after = country + all_links + shared_str_table

        ext_sst = self.__ext_sst_rec(0) # need fake cause we need calc stream pos
        eof = self.__eof_rec()

        self.__worksheets[self.__active_sheet].selected = True
        sheets = b''
        sheet_biff_lens = []
        for sheet in self.__worksheets:
            data = sheet.get_biff_data()
            sheets += data
            sheet_biff_lens.append(len(data))

        bundlesheets = self.__boundsheets_rec(len(before), len(after)+len(ext_sst)+len(eof), sheet_biff_lens)

        sst_stream_pos = len(before) + len(bundlesheets) + len(country)  + len(all_links)
        ext_sst = self.__ext_sst_rec(sst_stream_pos)

        return before + bundlesheets + after + ext_sst + eof + sheets

    def save(self, filename_or_stream):
        """
        This method is used to save the Workbook to a file in native Excel
        format.

        :param filename_or_stream:
          This can be a string containing a filename of
          the file, in which case the excel file is saved to disk using the name
          provided. It can also be a stream object with a write method, such as
          a :class:`~io.StringIO`, in which case the data for the excel
          file is written to the stream.
        """
        from . import CompoundDoc

        doc = CompoundDoc.XlsDoc()
        doc.save(filename_or_stream, self.get_biff_data())

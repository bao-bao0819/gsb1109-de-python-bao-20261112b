# -*- coding: utf-8 -*-
from __future__ import print_function

import six

from . import Formatting
from .BIFFRecords import FontRecord, NumberFormatRecord, StyleRecord, XFRecord

FIRST_USER_DEFINED_NUM_FORMAT_IDX = 164


class _FrozenObject(object):
    # Read-only container: once a style has been registered, its emitted
    # definition must not change even if the caller mutates the original
    # formatting object.
    __slots__ = ()

    def __setattr__(self, name, value):
        raise AttributeError("frozen style component is read-only")

    def _search_key(self):
        return tuple(getattr(self, name) for name in self.__slots__)


class FrozenFont(_FrozenObject):
    __slots__ = (
        'height', 'italic', 'struck_out', 'outline', 'shadow',
        'colour_index', 'bold', '_weight', 'escapement', 'underline',
        'family', 'charset', 'name',
    )

    def __init__(self, font):
        for name in self.__slots__:
            object.__setattr__(self, name, getattr(font, name))

    def get_biff_record(self):
        # Mirrors Formatting.Font.get_biff_record without mutating state.
        options = 0x00
        if self.bold:
            options |= 0x01
            weight = 0x02BC
        else:
            weight = self._weight
        if self.italic:
            options |= 0x02
        if self.underline != Formatting.Font.UNDERLINE_NONE:
            options |= 0x04
        if self.struck_out:
            options |= 0x08
        if self.outline:
            options |= 0x10
        if self.shadow:
            options |= 0x20
        return FontRecord(
            self.height, options, self.colour_index, weight,
            self.escapement, self.underline, self.family, self.charset,
            self.name,
        )


class FrozenAlignment(_FrozenObject):
    __slots__ = ('horz', 'vert', 'dire', 'orie', 'rota',
                 'wrap', 'shri', 'inde', 'merg')

    def __init__(self, alignment):
        for name in self.__slots__:
            object.__setattr__(self, name, getattr(alignment, name))


class FrozenBorders(_FrozenObject):
    __slots__ = (
        'left', 'right', 'top', 'bottom', 'diag',
        'left_colour', 'right_colour', 'top_colour',
        'bottom_colour', 'diag_colour',
        'need_diag1', 'need_diag2',
    )

    NO_LINE = Formatting.Borders.NO_LINE

    def __init__(self, borders):
        for name in self.__slots__:
            object.__setattr__(self, name, getattr(borders, name))
        # XFRecord drops the colour of sides without a line. Apply the same
        # normalization up front so the emitted bytes are identical.
        for line_attr, colour_attr in (
                ('left', 'left_colour'), ('right', 'right_colour'),
                ('top', 'top_colour'), ('bottom', 'bottom_colour'),
                ('diag', 'diag_colour')):
            if getattr(self, line_attr) == self.NO_LINE:
                object.__setattr__(self, colour_attr, 0x00)


class FrozenPattern(_FrozenObject):
    __slots__ = ('pattern', 'pattern_fore_colour', 'pattern_back_colour')

    def __init__(self, pattern):
        for name in self.__slots__:
            object.__setattr__(self, name, getattr(pattern, name))


class FrozenProtection(_FrozenObject):
    __slots__ = ('cell_locked', 'formula_hidden')

    def __init__(self, protection):
        for name in self.__slots__:
            object.__setattr__(self, name, getattr(protection, name))


def _freeze_gof(gof):
    alignment, borders, pattern, protection = gof
    return (
        FrozenAlignment(alignment),
        FrozenBorders(borders),
        FrozenPattern(pattern),
        FrozenProtection(protection),
    )

class XFStyle(object):

    def __init__(self):
        self.num_format_str  = 'General'
        self.font            = Formatting.Font()
        self.alignment       = Formatting.Alignment()
        self.borders         = Formatting.Borders()
        self.pattern         = Formatting.Pattern()
        self.protection      = Formatting.Protection()

default_style = XFStyle()

class StyleCollection(object):
    _std_num_fmt_list = [
            'general',
            '0',
            '0.00',
            '#,##0',
            '#,##0.00',
            '"$"#,##0_);("$"#,##0)',
            '"$"#,##0_);[Red]("$"#,##0)',
            '"$"#,##0.00_);("$"#,##0.00)',
            '"$"#,##0.00_);[Red]("$"#,##0.00)',
            '0%',
            '0.00%',
            '0.00E+00',
            '# ?/?',
            '# ??/??',
            'M/D/YY',
            'D-MMM-YY',
            'D-MMM',
            'MMM-YY',
            'h:mm AM/PM',
            'h:mm:ss AM/PM',
            'h:mm',
            'h:mm:ss',
            'M/D/YY h:mm',
            '_(#,##0_);(#,##0)',
            '_(#,##0_);[Red](#,##0)',
            '_(#,##0.00_);(#,##0.00)',
            '_(#,##0.00_);[Red](#,##0.00)',
            '_("$"* #,##0_);_("$"* (#,##0);_("$"* "-"_);_(@_)',
            '_(* #,##0_);_(* (#,##0);_(* "-"_);_(@_)',
            '_("$"* #,##0.00_);_("$"* (#,##0.00);_("$"* "-"??_);_(@_)',
            '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)',
            'mm:ss',
            '[h]:mm:ss',
            'mm:ss.0',
            '##0.0E+0',
            '@'
    ]

    def __init__(self, style_compression=0):
        self.style_compression = style_compression
        self.stats = [0, 0, 0, 0, 0, 0]
        self._font_id2x = {}
        self._font_x2id = {}
        self._font_val2x = {}
        self._font_x2frozen = {}

        for x in (0, 1, 2, 3, 5): # The font with index 4 is omitted in all BIFF versions
            font = Formatting.Font()
            search_key = font._search_key()
            self._font_id2x[font] = x
            self._font_x2id[x] = font
            self._font_val2x[search_key] = x
            self._font_x2frozen[x] = FrozenFont(font)

        self._xf_id2x = {}
        self._xf_x2id = {}
        self._xf_x2key = {}
        self._xf_snapshot2x = {}
        self._unmerged_diffs = []
        self._unmerged_keys = set()
        self._track_diffs = False

        self._num_formats = {}
        for fmtidx, fmtstr in zip(range(0, 23), StyleCollection._std_num_fmt_list[0:23]):
            self._num_formats[fmtstr] = fmtidx
        for fmtidx, fmtstr in zip(range(37, 50), StyleCollection._std_num_fmt_list[23:]):
            self._num_formats[fmtstr] = fmtidx

        self.default_style = XFStyle()
        self._default_xf = self._add_style(self.default_style)[0]
        self._track_diffs = True

    def add(self, style):
        if style == None:
            return 0x10
        return self._add_style(style)[1]

    @staticmethod
    def _xf_snapshot(font_idx, num_format_idx, gof):
        # Snapshot the whole XF definition, including the number format
        # string, so that styles which look identical on a sample of zero
        # values (e.g. money vs. percentage formats) are never merged.
        return (font_idx, num_format_idx) + tuple(obj._search_key() for obj in gof)

    def _register_xf(self, live_xf, frozen_xf, xf_key):
        # _xf_id2x keeps the caller's tuple so identity-based stats keep
        # their original meaning; _xf_x2id keeps the immutable snapshot that
        # is actually emitted.
        xf_index = self._xf_snapshot2x.get(xf_key)
        if xf_index is not None:
            self._xf_id2x[live_xf] = xf_index
            return xf_index, True
        xf_index = 0x10 + len(self._xf_x2id)
        self._xf_id2x[live_xf] = xf_index
        self._xf_snapshot2x[xf_key] = xf_index
        self._xf_x2id[xf_index] = frozen_xf
        self._xf_x2key[xf_index] = xf_key
        return xf_index, False

    def _record_unmerged_diff(self, xf_key):
        if not self._track_diffs or xf_key in self._unmerged_keys:
            return
        self._unmerged_keys.add(xf_key)
        font_idx, num_format_idx, alignment_key, borders_key, pattern_key, protection_key = xf_key
        num_format_strs = [
            fmt_str for fmt_str, fmt_idx in self._num_formats.items()
            if fmt_idx == num_format_idx
        ]
        self._unmerged_diffs.append({
            'num_format_idx': num_format_idx,
            'num_format_str': num_format_strs[0] if num_format_strs else None,
            'font_idx': font_idx,
            'alignment': alignment_key,
            'borders': borders_key,
            'pattern': pattern_key,
            'protection': protection_key,
        })

    def unmerged_style_diffs(self):
        # Definitions that produced a new XF record even though style
        # compression was enabled; each entry names the component that
        # prevented reuse. Entries are ordered by first occurrence.
        return [dict(item) for item in self._unmerged_diffs]

    def _resolve_font(self, font):
        # Identity reuse is only valid while the object still matches the
        # snapshot taken at registration. A mutated font falls through to
        # value-based lookup so it gets a new index instead of rewriting the
        # style already used by earlier rows.
        if font in self._font_id2x:
            font_idx = self._font_id2x[font]
            frozen = self._font_x2frozen.get(font_idx)
            if frozen is None or frozen._search_key() == font._search_key():
                self.stats[0] += 1
                return font_idx
        if self.style_compression:
            search_key = font._search_key()
            font_idx = self._font_val2x.get(search_key)
            if font_idx is not None:
                self._font_id2x[font] = font_idx
                self.stats[1] += 1
                return font_idx
            font_idx = len(self._font_x2id) + 1 # Why plus 1? Font 4 is missing
            self._font_id2x[font] = font_idx
            self._font_val2x[search_key] = font_idx
            self._font_x2id[font_idx] = font
            self._font_x2frozen[font_idx] = FrozenFont(font)
            self.stats[2] += 1
            return font_idx
        font_idx = len(self._font_id2x) + 1
        self._font_id2x[font] = font_idx
        self.stats[2] += 1
        return font_idx

    def _add_style(self, style):
        num_format_str = style.num_format_str
        if num_format_str in self._num_formats:
            num_format_idx = self._num_formats[num_format_str]
        else:
            num_format_idx = (
                FIRST_USER_DEFINED_NUM_FORMAT_IDX +
                len(self._num_formats) -
                len(StyleCollection._std_num_fmt_list)
            )
            self._num_formats[num_format_str] = num_format_idx

        font_idx = self._resolve_font(style.font)

        gof = (style.alignment, style.borders, style.pattern, style.protection)
        xf = (font_idx, num_format_idx) + gof
        identity_index = self._xf_id2x.get(xf)
        if self.style_compression == 2 and identity_index is not None:
            # The tuple was registered before; trust the identity shortcut
            # only while its snapshot still matches the current objects.
            identity_valid = (
                self._xf_x2key.get(identity_index)
                == self._xf_snapshot(font_idx, num_format_idx, gof)
            )
        else:
            identity_valid = identity_index is not None

        if identity_index is not None and identity_valid:
            xf_index = identity_index
            self.stats[3] += 1
        elif self.style_compression == 2:
            xf_key = self._xf_snapshot(font_idx, num_format_idx, gof)
            frozen_xf = (font_idx, num_format_idx) + _freeze_gof(gof)
            xf_index, reused = self._register_xf(xf, frozen_xf, xf_key)
            if reused:
                self.stats[4] += 1
            else:
                self._record_unmerged_diff(xf_key)
                self.stats[5] += 1
        else:
            xf_index = 0x10 + len(self._xf_id2x)
            self._xf_id2x[xf] = xf_index
            self.stats[5] += 1

        if xf_index >= 0xFFF:
            # 12 bits allowed, 0xFFF is a sentinel value
            raise ValueError("More than 4094 XFs (styles)")

        return xf, xf_index

    def add_font(self, font):
        return self._add_font(font)

    def _add_font(self, font):
        return self._resolve_font(font)


    def get_biff_data(self):
        result = b''
        result += self._all_fonts()
        result += self._all_num_formats()
        result += self._all_cell_styles()
        result += self._all_styles()
        return result

    def _all_fonts(self):
        result = b''
        if self.style_compression:
            fonts = self._font_x2frozen.items()
        else:
            fonts = [(x, o) for o, x in self._font_id2x.items()]
        for font_idx, font in sorted(fonts):
            result += font.get_biff_record().get()
        return result

    def _all_num_formats(self):
        result = b''
        alist = [
            (v, k)
            for k, v in self._num_formats.items()
            if v >= FIRST_USER_DEFINED_NUM_FORMAT_IDX
            ]
        alist.sort()
        for fmtidx, fmtstr in alist:
            result += NumberFormatRecord(fmtidx, fmtstr).get()
        return result

    def _all_cell_styles(self):
        result = b''
        for i in range(0, 16):
            result += XFRecord(self._default_xf, 'style').get()
        if self.style_compression == 2:
            styles = self._xf_x2id.items()
        else:
            styles = [(x, o) for o, x in self._xf_id2x.items()]
        for xf_idx, xf in sorted(styles):
            result += XFRecord(xf).get()
        return result

    def _all_styles(self):
        return StyleRecord().get()

# easyxf and its supporting objects ###################################

class EasyXFException(Exception):
    pass

class EasyXFCallerError(EasyXFException):
    pass

class EasyXFAuthorError(EasyXFException):
    pass

class IntULim(object):
    # If astring represents a valid unsigned integer ('123', '0xabcd', etc)
    # and it is <= limit, return the int value; otherwise return None.

    def __init__(self, limit):
        self.limit = limit

    def __call__(self, astring):
        try:
            value = int(astring, 0)
        except ValueError:
            return None
        if not 0 <= value <= self.limit:
            return None
        return value

bool_map = {
    # Text values for all Boolean attributes
    '1': 1, 'yes': 1, 'true':  1, 'on':  1,
    '0': 0, 'no':  0, 'false': 0, 'off': 0,
    }

border_line_map = {
    # Text values for these borders attributes:
    # left, right, top, bottom and diag
    'no_line':  0x00,
    'thin':     0x01,
    'medium':   0x02,
    'dashed':   0x03,
    'dotted':   0x04,
    'thick':    0x05,
    'double':   0x06,
    'hair':     0x07,
    'medium_dashed':                0x08,
    'thin_dash_dotted':             0x09,
    'medium_dash_dotted':           0x0a,
    'thin_dash_dot_dotted':         0x0b,
    'medium_dash_dot_dotted':       0x0c,
    'slanted_medium_dash_dotted':   0x0d,
    }

charset_map = {
    # Text values for font.charset
    'ansi_latin':           0x00,
    'sys_default':          0x01,
    'symbol':               0x02,
    'apple_roman':          0x4d,
    'ansi_jap_shift_jis':   0x80,
    'ansi_kor_hangul':      0x81,
    'ansi_kor_johab':       0x82,
    'ansi_chinese_gbk':     0x86,
    'ansi_chinese_big5':    0x88,
    'ansi_greek':           0xa1,
    'ansi_turkish':         0xa2,
    'ansi_vietnamese':      0xa3,
    'ansi_hebrew':          0xb1,
    'ansi_arabic':          0xb2,
    'ansi_baltic':          0xba,
    'ansi_cyrillic':        0xcc,
    'ansi_thai':            0xde,
    'ansi_latin_ii':        0xee,
    'oem_latin_i':          0xff,
    }


# Text values for colour indices. "grey" is a synonym of "gray".
# The names are those given by Microsoft Excel 2003 to the colours
# in the default palette. There is no great correspondence with
# any W3C name-to-RGB mapping.
_colour_map_text = """\
aqua 0x31
black 0x08
blue 0x0C
blue_gray 0x36
bright_green 0x0B
brown 0x3C
coral 0x1D
cyan_ega 0x0F
dark_blue 0x12
dark_blue_ega 0x12
dark_green 0x3A
dark_green_ega 0x11
dark_purple 0x1C
dark_red 0x10
dark_red_ega 0x10
dark_teal 0x38
dark_yellow 0x13
gold 0x33
gray_ega 0x17
gray25 0x16
gray40 0x37
gray50 0x17
gray80 0x3F
green 0x11
ice_blue 0x1F
indigo 0x3E
ivory 0x1A
lavender 0x2E
light_blue 0x30
light_green 0x2A
light_orange 0x34
light_turquoise 0x29
light_yellow 0x2B
lime 0x32
magenta_ega 0x0E
ocean_blue 0x1E
olive_ega 0x13
olive_green 0x3B
orange 0x35
pale_blue 0x2C
periwinkle 0x18
pink 0x0E
plum 0x3D
purple_ega 0x14
red 0x0A
rose 0x2D
sea_green 0x39
silver_ega 0x16
sky_blue 0x28
tan 0x2F
teal 0x15
teal_ega 0x15
turquoise 0x0F
violet 0x14
white 0x09
yellow 0x0D"""

colour_map = {}
for _line in _colour_map_text.splitlines():
    _name, _num = _line.split()
    _num = int(_num, 0)
    colour_map[_name] = _num
    if 'gray' in _name:
        colour_map[_name.replace('gray', 'grey')] = _num
del _colour_map_text, _line, _name, _num

def add_palette_colour(colour_str, colour_index):
    if not (8 <= colour_index <= 63):
        raise Exception("add_palette_colour: colour_index (%d) not in range(8, 64)" %
                (colour_index))
    colour_map[colour_str] = colour_index

# user-defined palette defines 56 RGB colors from entry 8 - 64
#excel_default_palette_b8 = [ # (red, green, blue)
#    (  0,  0,  0), (255,255,255), (255,  0,  0), (  0,255,  0),
#    (  0,  0,255), (255,255,  0), (255,  0,255), (  0,255,255),
#    (128,  0,  0), (  0,128,  0), (  0,  0,128), (128,128,  0),
#    (128,  0,128), (  0,128,128), (192,192,192), (128,128,128),
#    (153,153,255), (153, 51,102), (255,255,204), (204,255,255),
#    (102,  0,102), (255,128,128), (  0,102,204), (204,204,255),
#    (  0,  0,128), (255,  0,255), (255,255,  0), (  0,255,255),
#    (128,  0,128), (128,  0,  0), (  0,128,128), (  0,  0,255),
#    (  0,204,255), (204,255,255), (204,255,204), (255,255,153),
#    (153,204,255), (255,153,204), (204,153,255), (255,204,153),
#    ( 51,102,255), ( 51,204,204), (153,204,  0), (255,204,  0),
#    (255,153,  0), (255,102,  0), (102,102,153), (150,150,150),
#    (  0, 51,102), ( 51,153,102), (  0, 51,  0), ( 51, 51,  0),
#    (153, 51,  0), (153, 51,102), ( 51, 51,153), ( 51, 51, 51),
#    ]

# Default colour table for BIFF8 copied from
# OpenOffice.org's Documentation of the Microsoft Excel File Format, Excel Version 2003
# Note palette has LSB padded with 2 bytes 0x00
excel_default_palette_b8 = (
0x00000000,
0xFFFFFF00,
0xFF000000,
0x00FF0000,
0x0000FF00,
0xFFFF0000,
0xFF00FF00,
0x00FFFF00,
0x80000000,
0x00800000,
0x00008000,
0x80800000,
0x80008000,
0x00808000,
0xC0C0C000,
0x80808000,
0x9999FF00,
0x99336600,
0xFFFFCC00,
0xCCFFFF00,
0x66006600,
0xFF808000,
0x0066CC00,
0xCCCCFF00,
0x00008000,
0xFF00FF00,
0xFFFF0000,
0x00FFFF00,
0x80008000,
0x80000000,
0x00808000,
0x0000FF00,
0x00CCFF00,
0xCCFFFF00,
0xCCFFCC00,
0xFFFF9900,
0x99CCFF00,
0xFF99CC00,
0xCC99FF00,
0xFFCC9900,
0x3366FF00,
0x33CCCC00,
0x99CC0000,
0xFFCC0000,
0xFF990000,
0xFF660000,
0x66669900,
0x96969600,
0x00336600,
0x33996600,
0x00330000,
0x33330000,
0x99330000,
0x99336600,
0x33339900,
0x33333300)

assert len(excel_default_palette_b8) == 56

pattern_map = {
    # Text values for pattern.pattern
    # xlwt/doc/pattern_examples.xls showcases all of these patterns.
    'no_fill':              0,
    'none':                 0,
    'solid':                1,
    'solid_fill':           1,
    'solid_pattern':        1,
    'fine_dots':            2,
    'alt_bars':             3,
    'sparse_dots':          4,
    'thick_horz_bands':     5,
    'thick_vert_bands':     6,
    'thick_backward_diag':  7,
    'thick_forward_diag':   8,
    'big_spots':            9,
    'bricks':               10,
    'thin_horz_bands':      11,
    'thin_vert_bands':      12,
    'thin_backward_diag':   13,
    'thin_forward_diag':    14,
    'squares':              15,
    'diamonds':             16,
    }

def any_str_func(s):
    return s.strip()

def colour_index_func(s, maxval=0x7F):
    try:
        value = int(s, 0)
    except ValueError:
        return None
    if not (0 <= value <= maxval):
        return None
    return value

colour_index_func_7 = colour_index_func

def colour_index_func_15(s):
    return colour_index_func(s, maxval=0x7FFF)

def rotation_func(s):
    try:
        value = int(s, 0)
    except ValueError:
        return None
    if not (-90 <= value <= 90):
        raise EasyXFCallerError("rotation %d: should be -90 to +90 degrees" % value)
    if value < 0:
        value = 90 - value # encode as 91 to 180 (clockwise)
    return value

xf_dict = {
    'align': 'alignment', # synonym
    'alignment': {
        'dire': {
            'general': 0,
            'lr': 1,
            'rl': 2,
            },
        'direction': 'dire',
        'horiz': 'horz',
        'horizontal': 'horz',
        'horz': {
            'general': 0,
            'left': 1,
            'center': 2,
            'centre': 2, # "align: horiz centre" means xf.alignment.horz is set to 2
            'right': 3,
            'filled': 4,
            'justified': 5,
            'center_across_selection': 6,
            'centre_across_selection': 6,
            'distributed': 7,
            },
        'inde': IntULim(15), # restriction: 0 <= value <= 15
        'indent': 'inde',
        'rota': [{'stacked': 255, 'none': 0, }, rotation_func],
        'rotation': 'rota',
        'shri': bool_map,
        'shrink': 'shri',
        'shrink_to_fit': 'shri',
        'vert': {
            'top': 0,
            'center': 1,
            'centre': 1,
            'bottom': 2,
            'justified': 3,
            'distributed': 4,
            },
         'vertical': 'vert',
         'wrap': bool_map,
         },
    'border': 'borders',
    'borders': {
        'left':     [border_line_map, IntULim(0x0d)],
        'right':    [border_line_map, IntULim(0x0d)],
        'top':      [border_line_map, IntULim(0x0d)],
        'bottom':   [border_line_map, IntULim(0x0d)],
        'diag':     [border_line_map, IntULim(0x0d)],
        'top_colour':       [colour_map, colour_index_func_7],
        'bottom_colour':    [colour_map, colour_index_func_7],
        'left_colour':      [colour_map, colour_index_func_7],
        'right_colour':     [colour_map, colour_index_func_7],
        'diag_colour':      [colour_map, colour_index_func_7],
        'top_color':        'top_colour',
        'bottom_color':     'bottom_colour',
        'left_color':       'left_colour',
        'right_color':      'right_colour',
        'diag_color':       'diag_colour',
        'need_diag1':  bool_map,
        'need_diag2':  bool_map,
        },
    'font': {
        'bold': bool_map,
        'charset': charset_map,
        'color':  'colour_index',
        'color_index':  'colour_index',
        'colour':  'colour_index',
        'colour_index': [colour_map, colour_index_func_15],
        'escapement': {'none': 0, 'superscript': 1, 'subscript': 2},
        'family': {'none': 0, 'roman': 1, 'swiss': 2, 'modern': 3, 'script': 4, 'decorative': 5, },
        'height': IntULim(0xFFFF), # practical limits are much narrower e.g. 160 to 1440 (8pt to 72pt)
        'italic': bool_map,
        'name': any_str_func,
        'outline': bool_map,
        'shadow': bool_map,
        'struck_out': bool_map,
        'underline': [bool_map, {'none': 0, 'single': 1, 'single_acc': 0x21, 'double': 2, 'double_acc': 0x22, }],
        },
    'pattern': {
        'back_color':   'pattern_back_colour',
        'back_colour':  'pattern_back_colour',
        'fore_color':   'pattern_fore_colour',
        'fore_colour':  'pattern_fore_colour',
        'pattern': [pattern_map, IntULim(16)],
        'pattern_back_color':   'pattern_back_colour',
        'pattern_back_colour':  [colour_map, colour_index_func_7],
        'pattern_fore_color':   'pattern_fore_colour',
        'pattern_fore_colour':  [colour_map, colour_index_func_7],
        },
    'protection': {
        'cell_locked' :   bool_map,
        'formula_hidden': bool_map,
        },
    }

def _esplit(s, split_char, esc_char="\\"):
    escaped = False
    olist = ['']
    for c in s:
        if escaped:
            olist[-1] += c
            escaped = False
        elif c == esc_char:
            escaped = True
        elif c == split_char:
            olist.append('')
        else:
            olist[-1] += c
    return olist

def _parse_strg_to_obj(strg, obj, parse_dict,
    field_sep=",", line_sep=";", intro_sep=":", esc_char="\\", debug=False):
    for line in _esplit(strg, line_sep, esc_char):
        line = line.strip()
        if not line:
            break
        split_line = _esplit(line, intro_sep, esc_char)
        if len(split_line) != 2:
            raise EasyXFCallerError('line %r should have exactly 1 "%c"' % (line, intro_sep))
        section, item_str = split_line
        section = section.strip().lower()
        for counter in range(2):
            result = parse_dict.get(section)
            if result is None:
                raise EasyXFCallerError('section %r is unknown' % section)
            if isinstance(result, dict):
                break
            if not isinstance(result, str):
                raise EasyXFAuthorError(
                    'section %r should map to dict or str object; found %r' % (section, type(result)))
            # synonym
            old_section = section
            section = result
        else:
            raise EasyXFAuthorError('Attempt to define synonym of synonym (%r: %r)' % (old_section, result))
        section_dict = result
        section_obj = getattr(obj, section, None)
        if section_obj is None:
            raise EasyXFAuthorError('instance of %s class has no attribute named %s' % (obj.__class__.__name__, section))
        for kv_str in _esplit(item_str, field_sep, esc_char):
            guff = kv_str.split()
            if not guff:
                continue
            k = guff[0].lower().replace('-', '_')
            v = ' '.join(guff[1:])
            if not v:
                raise EasyXFCallerError("no value supplied for %s.%s" % (section, k))
            for counter in range(2):
                result = section_dict.get(k)
                if result is None:
                    raise EasyXFCallerError('%s.%s is not a known attribute' % (section, k))
                if not isinstance(result, six.string_types):
                    break
                # synonym
                old_k = k
                k = result
            else:
                raise EasyXFAuthorError('Attempt to define synonym of synonym (%r: %r)' % (old_k, result))
            value_info = result
            if not isinstance(value_info, list):
                value_info = [value_info]
            for value_rule in value_info:
                if isinstance(value_rule, dict):
                    # dict maps strings to integer field values
                    vl = v.lower().replace('-', '_')
                    if vl in value_rule:
                        value = value_rule[vl]
                        break
                elif callable(value_rule):
                    value = value_rule(v)
                    if value is not None:
                        break
                else:
                    raise EasyXFAuthorError("unknown value rule for attribute %r: %r" % (k, value_rule))
            else:
                raise EasyXFCallerError("unexpected value %r for %s.%s" % (v, section, k))
            try:
                orig = getattr(section_obj, k)
            except AttributeError:
                raise EasyXFAuthorError('%s.%s in dictionary but not in supplied object' % (section, k))
            if debug: print("+++ %s.%s = %r # %s; was %r" % (section, k, value, v, orig))
            setattr(section_obj, k, value)

def easyxf(strg_to_parse="", num_format_str=None,
           field_sep=",", line_sep=";", intro_sep=":", esc_char="\\", debug=False):
    """
    This function is used to create and configure
    :class:`XFStyle` objects for use with (for example) the
    :meth:`Worksheet.write` method.

    It takes a string to be parsed to obtain attribute values for
    :class:`Alignment`, :class:`Borders`, :class:`Font`, :class:`Pattern` and
    :class:`Protection` objects.

    Refer to the examples in the file `examples/xlwt_easyxf_simple_demo.py`
    and to the `xf_dict` dictionary in :mod:`xlwt.Style`.

    Various synonyms including color/colour, center/centre and gray/grey are
    allowed. Case is irrelevant (except maybe in font names). ``-`` may be used
    instead of ``_``.

    Example: ``font: bold on; align: wrap on, vert centre, horiz center``

    :param num_format_str:

      To get the "number format string" of an existing
      cell whose format you want to reproduce, select the cell and click on
      Format/Cells/Number/Custom. Otherwise, refer to Excel help.

      Examples: ``"#,##0.00"``, ``"dd/mm/yyyy"``

    :return: An :class:`XFstyle` object.

    """
    xfobj = XFStyle()
    if num_format_str is not None:
        xfobj.num_format_str = num_format_str
    if strg_to_parse:
        _parse_strg_to_obj(strg_to_parse, xfobj, xf_dict,
            field_sep=field_sep, line_sep=line_sep, intro_sep=intro_sep, esc_char=esc_char, debug=debug)
    return xfobj

def easyfont(strg_to_parse="", field_sep=",", esc_char="\\", debug=False):
    xfobj = XFStyle()
    if strg_to_parse:
        _parse_strg_to_obj("font: " + strg_to_parse, xfobj, xf_dict,
            field_sep=field_sep, line_sep=";", intro_sep=":", esc_char=esc_char, debug=debug)
    return xfobj.font

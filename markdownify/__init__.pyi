from _typeshed import Incomplete
from typing import Any, Callable, Union

ATX: str
ATX_CLOSED: str
UNDERLINED: str
SETEXT = UNDERLINED
SPACES: str
BACKSLASH: str
ASTERISK: str
UNDERSCORE: str
LSTRIP: str
RSTRIP: str
STRIP: str
STRIP_ONE: str
PREFORMATTED_TAGS: frozenset[str]


def strip1_pre(text: str) -> str: ...
def strip_pre(text: str) -> str: ...
def chomp(text: str) -> tuple[str, str, str]: ...
def extract_pre_text(
    el: Incomplete,
    is_decoration: Union[Callable[[Any], bool], None] = ...,
) -> str: ...


def markdownify(
    html: str,
    autolinks: bool = ...,
    bs4_options: str = ...,
    bullets: str = ...,
    code_language: str = ...,
    code_language_callback: Union[Callable[[Incomplete], Union[str, None]], None] = ...,
    pre_text_callback: Union[Callable[[Incomplete], bool], None] = ...,
    convert: Union[list[str], None] = ...,
    default_title: bool = ...,
    escape_asterisks: bool = ...,
    escape_underscores: bool = ...,
    escape_misc: bool = ...,
    heading_style: str = ...,
    keep_inline_images_in: list[str] = ...,
    newline_style: str = ...,
    strip: Union[list[str], None] = ...,
    strip_document: Union[str, None] = ...,
    strip_pre: Union[str, None] = ...,
    strong_em_symbol: str = ...,
    sub_symbol: str = ...,
    sup_symbol: str = ...,
    table_infer_header: bool = ...,
    wrap: bool = ...,
    wrap_width: Union[int, None] = ...,
) -> str: ...


class MarkdownConverter:
    def __init__(
        self,
        autolinks: bool = ...,
        bs4_options: str = ...,
        bullets: str = ...,
        code_language: str = ...,
        code_language_callback: Union[Callable[[Incomplete], Union[str, None]], None] = ...,
        pre_text_callback: Union[Callable[[Incomplete], bool], None] = ...,
        convert: Union[list[str], None] = ...,
        default_title: bool = ...,
        escape_asterisks: bool = ...,
        escape_underscores: bool = ...,
        escape_misc: bool = ...,
        heading_style: str = ...,
        keep_inline_images_in: list[str] = ...,
        newline_style: str = ...,
        strip: Union[list[str], None] = ...,
        strip_document: Union[str, None] = ...,
        strip_pre: Union[str, None] = ...,
        strong_em_symbol: str = ...,
        sub_symbol: str = ...,
        sup_symbol: str = ...,
        table_infer_header: bool = ...,
        wrap: bool = ...,
        wrap_width: Union[int, None] = ...,
    ) -> None:
        ...
  
    def convert(self, html: str) -> str:
        ...

    def convert_soup(self, soup: Incomplete) -> str:
        ...

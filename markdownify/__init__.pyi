from _typeshed import Incomplete
from typing import Callable, Iterator, Union

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


class FigureCatalogEntry:
    caption: str
    candidates: tuple[tuple[str, Union[str, None]], ...]
    explicit: bool
    ambiguous: bool
    def __init__(
        self,
        caption: str,
        candidates: tuple[tuple[str, Union[str, None]], ...],
        explicit: bool = ...,
        ambiguous: bool = ...,
    ) -> None: ...
    @property
    def plates(self) -> list[str]: ...
    def to_dict(self) -> dict: ...


class FigureCatalog:
    figures: dict[str, list[str]]
    entries: list[FigureCatalogEntry]
    def __init__(
        self,
        figures: dict[str, list[str]],
        entries: list[FigureCatalogEntry],
    ) -> None: ...
    def __iter__(self) -> Iterator[FigureCatalogEntry]: ...
    def __len__(self) -> int: ...
    def plates(self) -> list[str]: ...
    def lookup(self, plate_ref: str) -> list[FigureCatalogEntry]: ...
    def captions_for(self, plate_ref: str) -> list[str]: ...
    def to_dict(self) -> dict: ...
    def to_markdown(self) -> str: ...


def markdownify(
    html: str,
    autolinks: bool = ...,
    bs4_options: str = ...,
    bullets: str = ...,
    code_language: str = ...,
    code_language_callback: Union[Callable[[Incomplete], Union[str, None]], None] = ...,
    convert: Union[list[str], None] = ...,
    default_title: bool = ...,
    escape_asterisks: bool = ...,
    escape_underscores: bool = ...,
    escape_misc: bool = ...,
    figure_catalog: bool = ...,
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


def markdownify_with_catalog(
    html: str,
    autolinks: bool = ...,
    bs4_options: str = ...,
    bullets: str = ...,
    code_language: str = ...,
    code_language_callback: Union[Callable[[Incomplete], Union[str, None]], None] = ...,
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
) -> tuple[str, FigureCatalog]: ...


class MarkdownConverter:
    figure_catalog: Union[FigureCatalog, None]
    def __init__(
        self,
        autolinks: bool = ...,
        bs4_options: str = ...,
        bullets: str = ...,
        code_language: str = ...,
        code_language_callback: Union[Callable[[Incomplete], Union[str, None]], None] = ...,
        convert: Union[list[str], None] = ...,
        default_title: bool = ...,
        escape_asterisks: bool = ...,
        escape_underscores: bool = ...,
        escape_misc: bool = ...,
        figure_catalog: bool = ...,
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

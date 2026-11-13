"""销售表遮盖格反推唯一性核查工具。"""

from .hidden_cells import (
    CONFLICT,
    MULTIPLE,
    UNIQUE,
    AuditResult,
    NeedBoundsError,
    SheetModel,
    SpecError,
    audit_sheet,
    format_report,
    load_workbook,
    main,
    parse_sheet,
    run_spec,
)

__all__ = [
    "CONFLICT",
    "MULTIPLE",
    "UNIQUE",
    "AuditResult",
    "NeedBoundsError",
    "SheetModel",
    "SpecError",
    "audit_sheet",
    "format_report",
    "load_workbook",
    "main",
    "parse_sheet",
    "run_spec",
]

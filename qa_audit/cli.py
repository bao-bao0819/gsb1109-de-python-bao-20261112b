"""命令行入口：python -m qa_audit.cli audit <csvz> <manifest.json> [-o 报告前缀]。

manifest 首次可由 init 生成草稿，再人工补产线总量、允许层、每层最少件数：
    python -m qa_audit.cli init <csvz> > manifest.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa_audit import audit_csvz


def cmd_init(args):
    manifest = audit_csvz.init_manifest(args.csvz)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def cmd_audit(args):
    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    colmap = manifest.get("columns", {})
    sheets = audit_csvz.read_zip_sheets(args.csvz)
    report = audit_csvz.evaluate(manifest, sheets, colmap)
    markdown = audit_csvz.to_markdown(report)
    if args.output:
        with open(f"{args.output}.md", "w", encoding="utf-8") as handle:
            handle.write(markdown)
        with open(f"{args.output}.json", "w", encoding="utf-8") as handle:
            handle.write(audit_csvz.to_json(report))
        print(f"已写出 {args.output}.md / {args.output}.json")
    else:
        print(markdown)


def main(argv=None):
    parser = argparse.ArgumentParser(description="CSVZ 质检抽检覆盖核对")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="从 csvz 生成 manifest 草稿")
    init_parser.add_argument("csvz")
    init_parser.set_defaults(func=cmd_init)

    audit_parser = sub.add_parser("audit", help="按 manifest 核对覆盖并出报告")
    audit_parser.add_argument("csvz")
    audit_parser.add_argument("manifest")
    audit_parser.add_argument("-o", "--output", help="报告前缀，缺省打印 Markdown 到标准输出")
    audit_parser.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

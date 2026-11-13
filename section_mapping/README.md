# section_mapping：手册更新后的章节对照工具

手册改版后，把**已有 Markdown 分节结果**对应到新版 HTML，只重采真正改动的章节。

## 用法

```bash
python -m section_mapping.remap old.html new.html manifest.json \
    --json-out report.json --md-out report.md
```

- `old.html` / `new.html`：改版前后的两份手册 HTML。
- `manifest.json`：已有分节结果清单，阅读记录挂在各条目上：

```json
{
  "sections": [
    {
      "key": "limits-a",
      "id": "limits-a",
      "title": "限制",
      "markdown_file": "md/limits-a.md",
      "reading_record": {"status": "done", "reader": "carol"}
    }
  ]
}
```

## 匹配规则（优先级从高到低）

1. **id 即身份**：保留 id 的章节是同一节，即使标题改了、顺序变了、父节变了。
2. **完整内容指纹**：id 缺失/变更时，整节内容（转 Markdown 归一化后的 SHA1）
   完全一致才自动配对，可识别整节迁移。
3. **块级分析**：剩余章节逐块比对，识别拆分、合并、段落移动、删除，
   一律不自动配对。

标题**从不**作为匹配键。两处都叫「限制」的章节互换顺序时，配对严格按
id/内容，阅读记录不会随出现序号对调；报告中会标注「存在同名章节」。

## 输出三类清单

- **可沿用**：内容逐块一致，Markdown 与阅读记录直接沿用（附标题变更、
  父节变更、顺序变化等备注）。
- **要重采**：内容有改动（含段落移入/移出的来源与去向）或全新章节。
- **人工认领**：拆分（列出各候选新章节及命中块数）、合并、疑似删除。

示例见 `examples/`（`old.html` / `new.html` / `manifest.json` → `report.md`）。
测试：`pytest section_mapping/tests/`。

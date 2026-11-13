# CSVZ 质检抽检覆盖核对

纯标准库实现（`zipfile` + `csv` + `json`），不依赖 `chardet` 等第三方包。

## 输入

- `book.csvz`：zip 包，每个内部 `.csv` 页签 = 一班的抽检单。
- `manifest.json`：随包口径，声明班别、当班产线总量、允许抽样的层、每层最少件数。

### manifest 结构

```json
{
  "min_per_layer_default": 2,
  "columns": {},
  "shifts": [
    {
      "shift_id": "早班",
      "sheet_name": "早班.csv",
      "production_total": 480,
      "allowed_layers": ["A", "B", "C"],
      "min_per_layer": {"A": 2, "B": 2, "C": 2}
    }
  ]
}
```

- `production_total`：当班产线总量；缺页时若该值 >0，按“确有产出但页签缺失”处理，绝不当零产量。
- `allowed_layers`：本班允许抽样的层；落到层外的记录进“数据异常”，不计入覆盖。
- `min_per_layer`：每层最少件数；缺省回退到 `min_per_layer_default`。
- `columns`：列名自定义映射，键为 `item_id/layer/record_type/origin_item_id/origin_layer/origin_shift/candidate_layers`。

### 抽检单列名约定（默认别名，可被 `columns` 覆盖）

| 逻辑字段 | 默认表头别名 |
|---|---|
| 件号 | 件号 / 编号 / 产品编号 / item_id / id |
| 层 | 层 / 层级 / 抽样层 / layer |
| 类型 | 类型 / 单据类型 / record_type / type（值为 返修/重检/repair/rework 视为返修） |
| 原始件号 | 原始件号 / 原编号 / origin_item_id |
| 原始层 | 原始层 / 原层级 / origin_layer |
| 原始班别 | 原始班别 / 原班别 / origin_shift |
| 候选层 | 候选层 / 可能来源层 / candidate_layers（多值用 `; , ， 、 / |` 分隔） |

## 用法

```bash
# 1) 由 csvz 生成 manifest 草稿，再人工补产线总量/允许层/最少件数
python -m qa_audit.cli init book.csvz > manifest.json

# 2) 核对并打印 Markdown 报告
python -m qa_audit.cli audit book.csvz manifest.json

# 3) 同时落 Markdown 与机器可读 JSON
python -m qa_audit.cli audit book.csvz manifest.json -o report
```

## 口径落实

- **整包读取覆盖核对**：按“班别 × 层”单元与整包分层两级汇总，对照每层最少件数。
- **返修跨班重检只归一个原始层**：以原始件号去重，显式原始层/原始班强制归位，整包只计一次，重检班不重复计数。
- **多来源不任选**：同一件候选多个单元时保持歧义，只计入“最好覆盖”，不计入“最坏（保底）覆盖”，绝不擅自选定后宣称达标。
- **最好/最坏区间**：保底即足额=“保证达标”；最好仍不足=“确定不足”；介于之间=“能达标但无保证”，同时给出两端覆盖量。
- **缺页≠零产量**：manifest 有班别而 zip 无页签，且产线总量 >0 时记为真实缺口。
- **补采待办**：只列仍可能改变达标结论的缺页/层；已保底足额的层不再催办。

## 自测

```bash
python -m pytest tests/test_audit_synthetic.py -v
```

合成数据仅用于验证逻辑，不代表任何真实质检结论。

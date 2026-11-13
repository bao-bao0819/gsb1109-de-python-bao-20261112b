# 日结作业（daily_settlement）

输出每家店各品类销量**相对于上一份已公布日报的增减行**。

## 输入

JSON Lines，每行一条记录，用 `type` 区分。销售、退款、更正、上一日报
可以混在任意多个文件里、以任意顺序到达（退款允许先于销售到达）。

销售（销售文件）：

```json
{"type": "sale", "sale_id": "S001", "biz_date": "2026-11-12", "store": "ST01", "category": "饮料", "qty": 3}
```

退款（补数文件，引用原销售号；同一销售单可分几次退）：

```json
{"type": "refund", "refund_id": "R001", "sale_id": "S001", "qty": 1}
```

更正（更正销售的门店/品类/业务日，可多次，`ts` 越大越新）：

```json
{"type": "correction", "sale_id": "S001", "store": "ST02", "category": "零食", "ts": "2026-11-12T20:00:00"}
```

上一份已公布日报（全量快照，`sale_ids` 可选）：

```json
{"type": "prev", "biz_date": "2026-11-11", "store": "ST01", "category": "饮料", "qty": 10, "sale_ids": ["S000"]}
```

## 输出

JSON Lines，每行一条，用 `record` 区分：

- `delta`：增减行。`delta` 为新快照减上一份日报的差值（正增负减），
  只输出差值非零的行。`sale_ids` 为新旧两侧相关的销售单号——
  一个商品改类/改店时，旧值的冲减行与新值的增加行带有同一个
  `sale_id`，可追到同一张销售单。
- `unverified`：未核退款。`reason=over_refund` 表示超过原售数量的
  部分；`reason=unknown_sale` 表示引用的销售单始终未到达。
- `snapshot`：仅加 `--emit-snapshot` 时输出，为本次全量新快照，
  可作为下一次运行的 `prev` 输入。

```json
{"record": "delta", "biz_date": "2026-11-12", "store": "ST01", "category": "饮料", "delta": -1, "sale_ids": ["S001"]}
{"record": "unverified", "refund_id": "R009", "sale_id": "S001", "qty": 2, "reason": "over_refund"}
```

## 业务规则

- 退款引用原销售号，可分多次部分退款，允许先于销售记录到达。
- 退款算到销售**最终归属**的业务日/门店/品类上（即应用全部更正之后）。
- 同一笔退款（同一 `refund_id`）重复传送只认一次。
- 同一销售单累计退款超过原售数量的部分保持未核，不计入销量。
- 销售的门店、品类（及业务日）允许更正，多次更正按 `ts` 后者覆盖前者。
- 同一销售单重复传送且内容一致时去重；内容冲突时取排序后的第一条，
  保证结果确定。

## 运行

```sh
python settlement/daily_settlement.py -r inline 输入目录/ -o 输出目录/
# 同时产出全量新快照：
python settlement/daily_settlement.py -r inline --emit-snapshot 输入目录/ -o 输出目录/
```

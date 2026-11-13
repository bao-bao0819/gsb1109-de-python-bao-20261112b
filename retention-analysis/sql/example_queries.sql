-- 常用查询示例。q 不支持 WITH/CTE（会把 CTE 名误判为文件名），
-- 以下全部使用派生表写法，可直接交给 q 执行。
-- 把 DB 替换为实际的分析库路径（默认 work/retention_analysis.sqlite）。

-- 1. 每类客户数
q -H -O "SELECT category, customer_count
FROM work/retention_analysis.sqlite:::category_summary ORDER BY category";

-- 2. 无法缩小的不确定区间（流失/留存的上下界）
q -H -O "SELECT metric, lower_bound, upper_bound, note
FROM work/retention_analysis.sqlite:::uncertainty_bounds ORDER BY metric";

-- 3. 某一类客户的完整清单
q -H -O "SELECT customer_id, possible_labels, prev_stores, curr_stores, reason
FROM work/retention_analysis.sqlite:::customer_classification
WHERE category = 'insufficient_evidence' ORDER BY customer_id";

-- 4. 从分类一路追到原始 CSV 记录（文件名 + 物理行号）
q -H -O "SELECT category, customer_id, month, store_id, source_file, source_rowid, amount
FROM work/retention_analysis.sqlite:::customer_record_trace
WHERE customer_id = 'C001' ORDER BY month, source_rowid";

-- 5. 按门店看当月覆盖完整度，定位缺交门店
q -H -O "SELECT store_id, status_values, is_complete
FROM work/retention_analysis.sqlite:::coverage_status
WHERE month = '2026-10' ORDER BY store_id";

-- 6. 数据质量：覆盖缺口与冲突登记
q -H -O "SELECT * FROM work/retention_analysis.sqlite:::dq_missing_coverage";
q -H -O "SELECT * FROM work/retention_analysis.sqlite:::dq_duplicate_coverage";

-- 7. 每个证据不足客户的“缺交门店”，即不确定性的来源
q -H -O "SELECT customer_id, prev_stores_incomplete_curr, prev_stores
FROM work/retention_analysis.sqlite:::customer_classification
WHERE category = 'insufficient_evidence' ORDER BY customer_id";

-- 8. 跨店留存：上月与本月门店不同的留存客户
q -H -O "SELECT customer_id, prev_stores, curr_stores
FROM work/retention_analysis.sqlite:::customer_classification
WHERE category = 'retained_confirmed' AND prev_stores <> curr_stores";

-- 问题：当前数据质量检查有哪些警告或失败，影响边界是什么？
SELECT
    severity,
    status,
    check_name,
    detail,
    observed_at_utc
FROM fact_data_quality_event
WHERE status <> 'passed'
ORDER BY CASE severity WHEN 'error' THEN 1 ELSE 2 END, check_name;


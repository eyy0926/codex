import io
import unittest

import pandas as pd

from nl2sql_core import (
    MAX_RESULT_ROWS,
    fallback_sql,
    make_safe_columns,
    read_uploaded_file,
    run_query,
    sample_sales_data,
    schema_text,
    suggest_questions,
    validate_sql,
)


class NamedBytesIO(io.BytesIO):
    name = "test.csv"


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.data, self.mapping = make_safe_columns(sample_sales_data())

    def test_sample_data_is_consistent(self):
        self.assertEqual(len(self.data), 60)
        self.assertTrue((self.data["amount"] == self.data["quantity"] * self.data["unit_price"]).all())

    def test_chinese_and_duplicate_columns_are_safe(self):
        frame = pd.DataFrame([[1, 2, 3]], columns=["销售额", "name", "name"])
        safe, mapping = make_safe_columns(frame)
        self.assertEqual(list(safe.columns), ["col_1", "name", "name_2"])
        self.assertEqual(mapping["col_1"], "销售额")

    def test_csv_upload(self):
        uploaded = NamedBytesIO("城市,金额\n北京,10\n上海,20\n".encode("utf-8-sig"))
        result = read_uploaded_file(uploaded)
        self.assertEqual(result.shape, (2, 2))

    def test_unknown_file_extension_is_rejected(self):
        uploaded = NamedBytesIO(b"a,b\n1,2\n")
        uploaded.name = "test.txt"
        with self.assertRaises(ValueError):
            read_uploaded_file(uploaded)

    def test_schema_excludes_values_by_default(self):
        text = schema_text(self.data, self.mapping)
        self.assertNotIn("公众号", text)
        self.assertIn("channel", text)

    def test_dangerous_sql_is_blocked(self):
        cases = [
            "DROP TABLE uploaded_data",
            "WITH x AS (SELECT 1) DELETE FROM uploaded_data",
            "SELECT * FROM sqlite_master",
            "SELECT * FROM sqlite_temp_master",
        ]
        for sql in cases:
            self.assertFalse(validate_sql(sql)[0], sql)

    def test_expensive_sql_function_is_blocked_at_execution(self):
        with self.assertRaises(Exception):
            run_query("SELECT randomblob(10) FROM uploaded_data", self.data)

    def test_only_uploaded_table_can_be_read(self):
        # Validation rejects system tables too, but the authorizer should still
        # protect execution if a caller reaches it through another code path.
        with self.assertRaises(Exception):
            run_query("SELECT name FROM sqlite_master", self.data)

    def test_paid_count_filters_refunds_when_requested(self):
        sql = fallback_sql("已支付订单数是多少？", self.data, self.mapping)
        result, _ = run_query(sql, self.data)
        self.assertEqual(int(result.iloc[0]["记录数"]), 57)

    def test_query_result_is_capped(self):
        large = pd.DataFrame({"value": range(MAX_RESULT_ROWS + 20)})
        result, truncated = run_query("SELECT * FROM uploaded_data", large)
        self.assertEqual(len(result), MAX_RESULT_ROWS)
        self.assertTrue(truncated)

    def test_fallback_uses_question_dimension(self):
        sql = fallback_sql("各渠道的销售额是多少？", self.data, self.mapping)
        result, _ = run_query(sql, self.data)
        self.assertIn("渠道", result.columns)
        self.assertEqual(len(result), 4)

    def test_count_question_does_not_silently_filter_paid_rows(self):
        sql = fallback_sql("这份数据一共有多少条记录？", self.data, self.mapping)
        result, _ = run_query(sql, self.data)
        self.assertEqual(int(result.iloc[0]["记录数"]), 60)

    def test_month_trend_is_chronological(self):
        sql = fallback_sql("按月份查看销售额趋势。", self.data, self.mapping)
        result, _ = run_query(sql, self.data)
        self.assertEqual(result["月份"].tolist(), sorted(result["月份"].tolist()))

    def test_empty_numeric_insight_is_safe(self):
        from nl2sql_core import fallback_insight
        empty = pd.DataFrame({"分组": ["A"], "数值": [float("nan")]})
        self.assertIn("没有有效数据", fallback_insight("测试", empty))

    def test_suggestions_match_schema(self):
        questions = suggest_questions(self.data, self.mapping)
        self.assertTrue(any("渠道" in q for q in questions))
        self.assertTrue(any("月份" in q for q in questions))


if __name__ == "__main__":
    unittest.main()

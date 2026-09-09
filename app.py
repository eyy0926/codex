"""NL2SQL 智能查询系统

一个可直接运行的 Streamlit MVP：
- 内置电商销售示例库，开箱即用
- 有 OpenAI API Key 时调用大语言模型生成 SQL
- 没有 API Key 时提供可演示的规则兜底
- 只允许只读查询，并对危险 SQL 做拦截
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "demo_sales.db"


def init_db() -> None:
    """创建一份稳定的演示数据，便于面试现场直接演示。"""
    if DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            customer_name TEXT NOT NULL,
            city TEXT NOT NULL,
            signup_date TEXT NOT NULL
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL,
            unit_price REAL NOT NULL
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            order_date TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            amount REAL NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(customer_id),
            FOREIGN KEY(product_id) REFERENCES products(product_id)
        );
        """
    )
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", [
        (1, "张晨", "北京", "2024-01-12"), (2, "李欣", "上海", "2024-02-03"),
        (3, "王磊", "深圳", "2024-02-18"), (4, "赵敏", "杭州", "2024-03-08"),
        (5, "陈宇", "北京", "2024-03-21"), (6, "周婷", "广州", "2024-04-01"),
    ])
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", [
        (1, "AI 入门课", "课程", 299.0), (2, "数据分析课", "课程", 499.0),
        (3, "无线键盘", "硬件", 199.0), (4, "降噪耳机", "硬件", 699.0),
        (5, "Python 实战课", "课程", 399.0),
    ])
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (1001, 1, 1, "2024-04-03", 1, 299, "公众号", "已支付"),
        (1002, 2, 2, "2024-04-05", 1, 499, "搜索广告", "已支付"),
        (1003, 1, 3, "2024-04-09", 2, 398, "公众号", "已支付"),
        (1004, 3, 4, "2024-04-11", 1, 699, "短视频", "已支付"),
        (1005, 4, 5, "2024-04-15", 1, 399, "搜索广告", "已支付"),
        (1006, 5, 2, "2024-04-18", 1, 499, "短视频", "退款"),
        (1007, 6, 1, "2024-05-02", 1, 299, "公众号", "已支付"),
        (1008, 2, 4, "2024-05-04", 1, 699, "搜索广告", "已支付"),
        (1009, 3, 5, "2024-05-07", 2, 798, "短视频", "已支付"),
        (1010, 4, 3, "2024-05-10", 1, 199, "公众号", "已支付"),
        (1011, 5, 1, "2024-05-13", 1, 299, "搜索广告", "已支付"),
        (1012, 6, 2, "2024-05-20", 1, 499, "短视频", "已支付"),
        (1013, 1, 4, "2024-06-01", 1, 699, "公众号", "已支付"),
        (1014, 2, 5, "2024-06-03", 1, 399, "搜索广告", "已支付"),
        (1015, 3, 3, "2024-06-06", 3, 597, "短视频", "已支付"),
    ])
    conn.commit()
    conn.close()


def schema_text() -> str:
    return """数据库包含三张表：
customers(customer_id INTEGER 主键, customer_name TEXT, city TEXT, signup_date TEXT)
products(product_id INTEGER 主键, product_name TEXT, category TEXT, unit_price REAL)
orders(order_id INTEGER 主键, customer_id INTEGER, product_id INTEGER, order_date TEXT, quantity INTEGER, amount REAL, channel TEXT, status TEXT)
关联关系：orders.customer_id = customers.customer_id；orders.product_id = products.product_id。
仅统计 status = '已支付' 的订单时，请在 WHERE 中过滤；日期字段格式为 YYYY-MM-DD。"""


def get_llm_sql(question: str, api_key: str, model: str) -> tuple[str, str]:
    """调用 OpenAI 兼容接口；导入失败或请求失败时由上层走规则兜底。"""
    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=api_key)
    prompt = f"""你是资深数据分析师。根据用户问题生成 SQLite SQL。
要求：只输出一条 SELECT 或 WITH 查询，不要 Markdown，不要解释；必须使用真实表和字段；默认限制最多 200 行。
{schema_text()}
用户问题：{question}"""
    response = client.chat.completions.create(
        model=model, temperature=0, messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content or ""
    sql = re.sub(r"```(?:sql)?", "", raw, flags=re.I).replace("```", "").strip()
    return sql, "大语言模型"


def fallback_sql(question: str) -> tuple[str, str]:
    q = question.lower()
    if any(x in q for x in ["渠道", "channel"]):
        return ("SELECT channel AS 渠道, ROUND(SUM(amount), 2) AS 销售额, "
                "COUNT(*) AS 订单数 FROM orders WHERE status='已支付' "
                "GROUP BY channel ORDER BY 销售额 DESC LIMIT 200", "规则演示")
    if any(x in q for x in ["商品", "产品", "product"]):
        return ("SELECT p.product_name AS 商品, ROUND(SUM(o.amount), 2) AS 销售额, "
                "SUM(o.quantity) AS 销量 FROM orders o JOIN products p ON o.product_id=p.product_id "
                "WHERE o.status='已支付' GROUP BY p.product_id ORDER BY 销售额 DESC LIMIT 200", "规则演示")
    if any(x in q for x in ["城市", "地区", "city"]):
        return ("SELECT c.city AS 城市, ROUND(SUM(o.amount), 2) AS 销售额, COUNT(DISTINCT c.customer_id) AS 客户数 "
                "FROM orders o JOIN customers c ON o.customer_id=c.customer_id WHERE o.status='已支付' "
                "GROUP BY c.city ORDER BY 销售额 DESC LIMIT 200", "规则演示")
    if any(x in q for x in ["月份", "月度", "趋势", "month"]):
        return ("SELECT substr(order_date,1,7) AS 月份, ROUND(SUM(amount), 2) AS 销售额 "
                "FROM orders WHERE status='已支付' GROUP BY 月份 ORDER BY 月份 LIMIT 200", "规则演示")
    return ("SELECT COUNT(*) AS 已支付订单数, ROUND(SUM(amount), 2) AS 总销售额, "
            "ROUND(AVG(amount), 2) AS 平均客单价 FROM orders WHERE status='已支付' LIMIT 200", "规则演示")


def validate_sql(sql: str) -> tuple[bool, str]:
    normalized = re.sub(r"\s+", " ", sql.strip()).lower()
    if not normalized:
        return False, "未生成 SQL。"
    if ";" in normalized.rstrip(";"):
        return False, "仅允许执行一条 SQL。"
    if not re.match(r"^(select|with)\b", normalized):
        return False, "出于安全考虑，仅允许 SELECT/WITH 查询。"
    forbidden = r"\b(insert|update|delete|drop|alter|create|attach|pragma|replace|vacuum)\b"
    if re.search(forbidden, normalized):
        return False, "检测到可能修改数据的关键字，已拦截。"
    return True, ""


def run_query(sql: str) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    try:
        return pd.read_sql_query(sql, conn)
    finally:
        conn.close()


st.set_page_config(page_title="NL2SQL 智能查询", page_icon="🔎", layout="wide")
init_db()

st.markdown("# 🔎 NL2SQL 智能查询系统")
st.caption("用自然语言查询业务数据 · 自动生成 SQL · 安全只读执行 · 支持结果可视化")

with st.sidebar:
    st.header("连接设置")
    api_key = st.text_input("OpenAI API Key（可选）", type="password", value=os.getenv("OPENAI_API_KEY", ""))
    model = st.text_input("模型", value=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    st.info("未填写 Key 时使用规则演示模式，适合快速体验。")
    st.divider()
    st.subheader("示例问题")
    examples = ["各渠道的销售额和订单数是多少？", "销售额最高的商品有哪些？", "按月份看销售额趋势", "哪个城市的销售额最高？"]
    for example in examples:
        if st.button(example, use_container_width=True):
            st.session_state.question = example

question = st.text_area("输入你的问题", value=st.session_state.get("question", ""),
                        placeholder="例如：各渠道的销售额和订单数是多少？", height=90)
run = st.button("生成 SQL 并执行", type="primary", use_container_width=False)

if run:
    if not question.strip():
        st.warning("请先输入一个问题。")
    else:
        with st.spinner("正在理解问题并生成查询…"):
            try:
                if api_key.strip():
                    sql, source = get_llm_sql(question.strip(), api_key.strip(), model.strip())
                else:
                    sql, source = fallback_sql(question.strip())
            except Exception as exc:
                st.warning(f"模型调用失败，已切换到规则演示：{exc}")
                sql, source = fallback_sql(question.strip())
        valid, error = validate_sql(sql)
        if not valid:
            st.error(error)
        else:
            try:
                result = run_query(sql)
                st.session_state.history = st.session_state.get("history", [])
                st.session_state.history.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "question": question, "sql": sql, "rows": len(result)})
                st.success(f"查询完成 · {source} · 返回 {len(result)} 行")
                left, right = st.columns([1.2, 1])
                with left:
                    st.subheader("查询结果")
                    st.dataframe(result, use_container_width=True, hide_index=True)
                    st.download_button("下载 CSV", result.to_csv(index=False).encode("utf-8-sig"), "query_result.csv", "text/csv")
                with right:
                    st.subheader("快速图表")
                    numeric = result.select_dtypes(include="number").columns.tolist()
                    categorical = [c for c in result.columns if c not in numeric]
                    if categorical and numeric:
                        chart_df = result.set_index(categorical[0])[[numeric[0]]]
                        st.bar_chart(chart_df)
                    else:
                        st.caption("结果包含可视化所需的分类列和数值列后，图表会自动出现。")
                with st.expander("查看生成的 SQL", expanded=True):
                    st.code(sql, language="sql")
            except Exception as exc:
                st.error(f"SQL 执行失败：{exc}")

tab_schema, tab_history = st.tabs(["数据字典", "查询历史"])
with tab_schema:
    st.code(schema_text(), language="text")
with tab_history:
    history = st.session_state.get("history", [])
    if history:
        st.dataframe(pd.DataFrame(history), use_container_width=True, hide_index=True)
    else:
        st.caption("本次会话还没有查询记录。")


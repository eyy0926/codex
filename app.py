"""NL2SQL 智能查询系统 Streamlit 界面。"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import streamlit as st

from nl2sql_core import (
    MAX_UPLOAD_BYTES,
    fallback_insight,
    fallback_sql,
    generate_insight,
    generate_sql,
    make_safe_columns,
    read_uploaded_file,
    repair_sql,
    run_query,
    sample_sales_data,
    schema_text,
    suggest_questions,
    validate_sql,
)


def secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, os.getenv(name, default))
        return str(value)
    except Exception:
        return os.getenv(name, default)


def set_question(value: str) -> None:
    st.session_state.question_input = value


def get_data() -> tuple[pd.DataFrame | None, dict[str, str], str]:
    if st.session_state.data_mode == "一键示例数据":
        raw = sample_sales_data()
        data, mapping = make_safe_columns(raw)
        return data, mapping, "虚拟销售数据"
    uploaded = st.session_state.get("uploaded_file")
    if uploaded is None:
        return None, {}, "等待上传"
    raw = read_uploaded_file(uploaded)
    data, mapping = make_safe_columns(raw)
    return data, mapping, uploaded.name


st.set_page_config(page_title="NL2SQL 智能查询", page_icon="🔎", layout="wide")
st.markdown("# 🔎 NL2SQL 智能查询系统")
st.caption("上传业务数据，用自然语言完成查询、图表和结论生成")

with st.sidebar:
    st.header("数据源")
    st.radio("选择数据", ["一键示例数据", "上传 CSV / Excel"], key="data_mode")
    if st.session_state.data_mode == "上传 CSV / Excel":
        st.file_uploader(
            "选择文件",
            type=["csv", "xlsx", "xls"],
            key="uploaded_file",
            help=f"最大 {MAX_UPLOAD_BYTES // 1024 // 1024} MB、最多 100,000 行。",
        )
        st.caption("隐私提示：默认只向模型发送字段名和类型，不发送原始数据行。")
    else:
        st.caption("已准备 60 行虚拟销售数据，适合直接体验。")

    st.divider()
    st.header("模型设置")
    api_key = st.text_input("DeepSeek API Key（可选）", type="password", value=secret("DEEPSEEK_API_KEY"))
    base_url = st.text_input("API Base URL", value=secret("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = st.text_input("模型 ID", value=secret("DEEPSEEK_MODEL", "deepseek-v4-pro"), help="已按项目配置预填 deepseek-v4-pro；请以服务商控制台显示的模型 ID 为准。")
    include_samples = st.toggle("允许向模型发送前三行样例", value=False, help="字段含义不明确时可提高生成准确率；请勿用于敏感数据。")
    use_ai_insight = st.toggle("使用模型生成业务洞察", value=False, help="开启后会将查询结果前 20 行发送给模型；关闭则使用本地规则生成摘要。")
    st.caption("未配置 Key 时自动使用本地规则模式，所有数据留在本机。")

try:
    active_data, active_mapping, source_name = get_data()
except Exception as exc:
    active_data, active_mapping, source_name = None, {}, "读取失败"
    st.error(f"数据读取失败：{exc}")

if active_data is None:
    st.info("请在左侧上传 CSV 或 Excel，或切换到“一键示例数据”。")
    st.stop()

fingerprint = int(pd.util.hash_pandas_object(active_data, index=True).sum()) if not active_data.empty else 0
source_signature = f"{source_name}|{active_data.shape}|{fingerprint}|{','.join(map(str, active_data.columns))}"
if st.session_state.get("result_source_signature") != source_signature:
    for key in ("last_result", "last_sql", "last_insight", "last_source", "last_truncated", "last_repaired"):
        st.session_state.pop(key, None)
    st.session_state.result_source_signature = source_signature

suggestions = suggest_questions(active_data, active_mapping)
schema = schema_text(active_data, active_mapping, include_samples=include_samples)

m1, m2, m3 = st.columns(3)
m1.metric("数据行数", f"{len(active_data):,}")
m2.metric("字段数量", len(active_data.columns))
m3.metric("当前数据源", source_name)

st.subheader("推荐问题")
question_columns = st.columns(min(len(suggestions), 3))
for index, suggestion in enumerate(suggestions):
    with question_columns[index % len(question_columns)]:
        st.button(
            suggestion,
            key=f"suggestion_{index}_{source_name}",
            width="stretch",
            on_click=set_question,
            args=(suggestion,),
        )

question = st.text_area("输入你的问题", key="question_input", placeholder="例如：各渠道的销售额和订单数是多少？", height=90)
run = st.button("生成 SQL 并执行", type="primary")

if run:
    if not question.strip():
        st.warning("请先输入一个问题。")
    else:
        sql = ""
        source = "本地规则"
        with st.spinner("正在理解问题并查询数据…"):
            if api_key.strip():
                try:
                    sql = generate_sql(question, schema, api_key.strip(), model.strip(), base_url.strip())
                    source = "大语言模型"
                except Exception as exc:
                    st.warning(f"模型调用失败，已切换到本地规则：{exc}")
            if not sql:
                sql = fallback_sql(question, active_data, active_mapping)

            valid, validation_error = validate_sql(sql)
            if not valid:
                if api_key.strip():
                    try:
                        sql = repair_sql(question, schema, sql, validation_error, api_key.strip(), model.strip(), base_url.strip())
                        valid, validation_error = validate_sql(sql)
                        if valid:
                            source = "大语言模型自动修复"
                    except Exception:
                        valid = False
                if not valid:
                    st.error(validation_error)
                    st.stop()

            repaired = False
            try:
                result, truncated = run_query(sql, active_data)
            except Exception as first_error:
                if api_key.strip():
                    try:
                        repaired_sql = repair_sql(question, schema, sql, str(first_error), api_key.strip(), model.strip(), base_url.strip())
                        valid, validation_error = validate_sql(repaired_sql)
                        if not valid:
                            raise ValueError(validation_error)
                        result, truncated = run_query(repaired_sql, active_data)
                        sql, repaired = repaired_sql, True
                        source = "大语言模型自动修复"
                    except Exception as repair_error:
                        st.error(f"SQL 执行失败，自动修复也未成功：{repair_error}")
                        st.stop()
                else:
                    st.error(f"SQL 执行失败：{first_error}")
                    st.stop()

            try:
                insight = generate_insight(
                    question,
                    sql,
                    result,
                    api_key.strip() if use_ai_insight else "",
                    model.strip(),
                    base_url.strip(),
                )
            except Exception:
                insight = fallback_insight(question, result)

        st.session_state.last_result = result
        st.session_state.last_sql = sql
        st.session_state.last_insight = insight
        st.session_state.last_source = source
        st.session_state.last_truncated = truncated
        st.session_state.last_repaired = repaired
        st.session_state.history = st.session_state.get("history", [])
        st.session_state.history.insert(0, {"时间": datetime.now().strftime("%H:%M:%S"), "数据源": source_name, "问题": question, "模式": source, "返回行数": len(result), "SQL": sql})
        st.session_state.history = st.session_state.history[:20]

if st.session_state.get("last_result") is not None:
    result = st.session_state.last_result
    sql = st.session_state.last_sql
    insight = st.session_state.last_insight
    source = st.session_state.last_source
    truncated = st.session_state.last_truncated
    repaired = st.session_state.last_repaired
    st.success(f"查询完成 · {source} · 返回 {len(result)} 行" + (" · 已自动修复" if repaired else ""))
    if truncated:
        st.warning("结果超过 200 行，页面仅展示前 200 行。")
    st.subheader("数据洞察")
    st.write(insight)
    left, right = st.columns([1.2, 1])
    with left:
        st.subheader("查询结果")
        st.dataframe(result, width="stretch", hide_index=True)
        st.download_button("下载查询结果 CSV", result.to_csv(index=False).encode("utf-8-sig"), "query_result.csv", "text/csv")
    with right:
        st.subheader("快速图表")
        numeric = result.select_dtypes(include="number").columns.tolist()
        categorical = [c for c in result.columns if c not in numeric]
        if categorical and numeric and 1 < len(result) <= 50:
            st.bar_chart(result.set_index(categorical[0])[[numeric[0]]])
        else:
            st.caption("结果包含分类列和数值列，且不超过 50 行时自动生成图表。")
    with st.expander("查看生成的 SQL", expanded=True):
        st.code(sql, language="sql")

tab_preview, tab_schema, tab_history, tab_eval, tab_about = st.tabs(["数据预览", "数据字典", "查询历史", "能力评测", "安全说明"])
with tab_preview:
    st.dataframe(active_data.head(20), width="stretch", hide_index=True)
with tab_schema:
    st.code(schema, language="text")
with tab_history:
    history = st.session_state.get("history", [])
    if history:
        st.dataframe(pd.DataFrame(history), width="stretch", hide_index=True)
    else:
        st.caption("本次会话还没有查询记录。")
with tab_eval:
    st.caption("以下是本地规则模式的回归检查，用于验证换数据后核心流程仍可运行。接入模型后的 SQL 质量还应结合业务标注集评估。")
    evaluation_cases = [
        ("按渠道汇总", "各渠道的销售额分别是多少？", "渠道"),
        ("按月份趋势", "按月份查看销售额趋势。", "月份"),
        ("退款统计", "退款订单有多少笔，退款金额是多少？", "记录数"),
        ("总量汇总", "这份数据一共有多少条记录？", "记录数"),
    ]
    eval_rows = []
    for case_name, eval_question, expected_column in evaluation_cases:
        try:
            eval_sql = fallback_sql(eval_question, active_data, active_mapping)
            eval_result, _ = run_query(eval_sql, active_data)
            passed = expected_column in eval_result.columns and len(eval_result) > 0
            eval_rows.append({"检查项": case_name, "结果": "通过" if passed else "未通过", "返回行数": len(eval_result)})
        except Exception as exc:
            eval_rows.append({"检查项": case_name, "结果": f"失败：{exc}", "返回行数": 0})
    passed_count = sum(row["结果"] == "通过" for row in eval_rows)
    st.metric("回归检查通过率", f"{passed_count}/{len(eval_rows)}")
    st.dataframe(pd.DataFrame(eval_rows), width="stretch", hide_index=True)
with tab_about:
    st.markdown("""
- 仅允许执行 `SELECT` / `WITH` 查询，并拦截写入、DDL、系统表和危险关键字。
- 查询运行在临时 SQLite 数据库中，限制执行时间和返回行数。
- 默认只向模型发送字段名、原始列名和数据类型；开启样例选项后才发送前三行。
- 上传文件只在当前应用会话中处理，不写入项目目录。
""")

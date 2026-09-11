"""NL2SQL 的可测试核心逻辑。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from io import BytesIO
from typing import Any

import pandas as pd


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_ROWS = 100_000
MAX_RESULT_ROWS = 200
QUERY_TIMEOUT_SECONDS = 4


def sample_sales_data() -> pd.DataFrame:
    """生成一份适合现场演示的虚拟销售数据。"""
    channels = ["公众号", "搜索广告", "短视频", "社群"]
    cities = ["北京", "上海", "深圳", "杭州", "广州"]
    products = [
        ("AI入门课", "课程", 299),
        ("数据分析课", "课程", 499),
        ("Python实战课", "课程", 399),
        ("无线键盘", "硬件", 199),
        ("降噪耳机", "硬件", 699),
    ]
    campaigns = ["新年学习季", "春季上新", "五一活动", "618预热"]
    rows: list[dict[str, Any]] = []
    dates = pd.date_range("2024-01-03", periods=60, freq="3D")
    for index, date in enumerate(dates, start=1):
        product, category, price = products[(index - 1) % len(products)]
        quantity = 1 + int(index % 3 == 0) + int(index % 11 == 0)
        rows.append(
            {
                "order_id": f"SO{1000 + index}",
                "order_date": date.strftime("%Y-%m-%d"),
                "channel": channels[(index - 1) % len(channels)],
                "city": cities[(index - 1) % len(cities)],
                "product_name": product,
                "category": category,
                "customer_type": "新客" if index % 3 else "老客",
                "quantity": quantity,
                "unit_price": price,
                "amount": quantity * price,
                "status": "退款" if index in {10, 30, 55} else "已支付",
                "campaign": campaigns[min((index - 1) // 15, 3)],
            }
        )
    return pd.DataFrame(rows)


def make_safe_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """将列名转换为稳定的 SQLite 标识符，返回安全列名到原列名的映射。"""
    safe_names: list[str] = []
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for index, original_value in enumerate(df.columns, start=1):
        original = str(original_value).strip() or f"未命名列{index}"
        candidate = original if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", original) else f"col_{index}"
        base = candidate
        suffix = 2
        while candidate.lower() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate.lower())
        safe_names.append(candidate)
        mapping[candidate] = original
    result = df.copy()
    result.columns = safe_names
    return result, mapping


def read_uploaded_file(uploaded_file: Any) -> pd.DataFrame:
    """读取 CSV/Excel，限制体积和行数，并处理常见中文编码。"""
    raw = uploaded_file.getvalue()
    if not raw:
        raise ValueError("文件为空。")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 20 MB，请精简后重新上传。")
    name = uploaded_file.name.lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(BytesIO(raw))
    else:
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "gb18030", "utf-8"):
            try:
                df = pd.read_csv(BytesIO(raw), encoding=encoding)
                break
            except UnicodeDecodeError as exc:
                last_error = exc
        else:
            raise ValueError("无法识别 CSV 编码，请另存为 UTF-8 CSV。") from last_error
    if df.empty or len(df.columns) == 0:
        raise ValueError("文件没有可查询的数据。")
    if len(df) > MAX_UPLOAD_ROWS:
        raise ValueError(f"文件超过 {MAX_UPLOAD_ROWS:,} 行，请先抽样或汇总。")
    return df


def schema_text(df: pd.DataFrame, mapping: dict[str, str], include_samples: bool = False) -> str:
    """构造发送给模型的数据字典；默认不发送真实单元格值。"""
    lines = [
        "SQLite 数据库只有一张表 uploaded_data。",
        "只能使用下面列出的安全列名；原始列名仅用于理解业务含义。",
        "字段（安全列名 | 原始列名 | 类型）：",
    ]
    for safe in df.columns:
        lines.append(f"- {safe} | {mapping.get(str(safe), str(safe))} | {df[safe].dtype}")
    if include_samples:
        sample = df.head(3).where(pd.notna(df.head(3)), None).to_dict(orient="records")
        lines.append("用户已允许发送的前三行样例：" + json.dumps(sample, ensure_ascii=False, default=str))
    return "\n".join(lines)


def extract_sql(raw: str) -> str:
    text = re.sub(r"```(?:sql)?", "", raw, flags=re.I).replace("```", "").strip()
    match = re.search(r"(?is)\b(SELECT|WITH)\b.*", text)
    return match.group(0).strip() if match else text


def _client(api_key: str, base_url: str):
    from openai import OpenAI  # type: ignore

    return OpenAI(api_key=api_key, base_url=base_url.rstrip("/"), timeout=25.0, max_retries=1)


def generate_sql(question: str, schema: str, api_key: str, model: str, base_url: str) -> str:
    prompt = f"""你是数据分析师，请把用户问题转换为一条 SQLite 查询。
规则：
1. 只输出 SQL，不要 Markdown 或解释。
2. 只能生成 SELECT 或 WITH 查询，禁止修改数据。
3. 只能使用数据字典中存在的表和安全列名。
4. 默认最多返回 {MAX_RESULT_ROWS} 行。
5. 如果数据中存在订单状态字段，销售额问题默认排除退款，除非用户明确询问退款。

数据字典：
{schema}

用户问题：{question}"""
    response = _client(api_key, base_url).chat.completions.create(
        model=model, temperature=0, messages=[{"role": "user", "content": prompt}]
    )
    return extract_sql(response.choices[0].message.content or "")


def repair_sql(question: str, schema: str, failed_sql: str, error: str, api_key: str, model: str, base_url: str) -> str:
    prompt = f"""下面的 SQLite 查询执行失败。根据数据字典和错误信息修复它。
只输出一条修复后的 SELECT 或 WITH SQL，不要解释，不要使用未列出的字段。

数据字典：
{schema}

用户问题：{question}
失败 SQL：{failed_sql}
错误信息：{error}"""
    response = _client(api_key, base_url).chat.completions.create(
        model=model, temperature=0, messages=[{"role": "user", "content": prompt}]
    )
    return extract_sql(response.choices[0].message.content or "")


def _find_column(mapping: dict[str, str], terms: list[str]) -> str | None:
    for safe, original in mapping.items():
        haystack = f"{safe} {original}".lower()
        if any(term.lower() in haystack for term in terms):
            return safe
    return None


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def suggest_questions(df: pd.DataFrame, mapping: dict[str, str]) -> list[str]:
    questions: list[str] = []
    amount = _find_column(mapping, ["amount", "sales", "revenue", "销售额", "金额", "收入"])
    date = _find_column(mapping, ["date", "日期", "时间"])
    dimensions = [
        ("渠道", ["channel", "渠道"]),
        ("商品", ["product", "商品", "产品"]),
        ("城市", ["city", "城市", "地区"]),
        ("品类", ["category", "品类", "类别"]),
    ]
    for label, terms in dimensions:
        if _find_column(mapping, terms) and amount:
            questions.append(f"各{label}的销售额分别是多少？")
    if date and amount:
        questions.append("按月份查看销售额趋势。")
    if _find_column(mapping, ["status", "状态"]):
        questions.append("退款订单有多少笔，退款金额是多少？")
    if not questions:
        numeric = df.select_dtypes(include="number").columns.tolist()
        categorical = [c for c in df.columns if c not in numeric]
        if numeric and categorical:
            questions.append(f"按{mapping.get(str(categorical[0]), str(categorical[0]))}汇总{mapping.get(str(numeric[0]), str(numeric[0]))}。")
        questions.append("这份数据一共有多少条记录？")
    return questions[:5]


def fallback_sql(question: str, df: pd.DataFrame, mapping: dict[str, str]) -> str:
    """无模型时，针对常见统计问题生成与字段相关的查询。"""
    q = question.lower()
    numeric = df.select_dtypes(include="number").columns.tolist()
    amount = _find_column(mapping, ["amount", "sales", "revenue", "销售额", "金额", "收入"])
    quantity = _find_column(mapping, ["quantity", "qty", "数量", "销量"])
    date = _find_column(mapping, ["date", "日期", "时间"])
    status = _find_column(mapping, ["status", "状态"])
    id_col = _find_column(mapping, ["order_id", "订单号", "id"])
    count_requested = any(term in q for term in ["多少条", "多少笔", "订单数", "记录数", "有多少订单", "count"])
    dimensions = [
        (["渠道", "channel"], _find_column(mapping, ["channel", "渠道"]), "渠道"),
        (["商品", "产品", "product"], _find_column(mapping, ["product", "商品", "产品"]), "商品"),
        (["城市", "地区", "city"], _find_column(mapping, ["city", "城市", "地区"]), "城市"),
        (["品类", "类别", "category"], _find_column(mapping, ["category", "品类", "类别"]), "品类"),
    ]
    dimension, alias = None, "分组"
    for terms, candidate, candidate_alias in dimensions:
        if candidate and any(term in q for term in terms):
            dimension, alias = candidate, candidate_alias
            break
    # 只有用户明确提出分组维度时才分组；“总销售额/平均值”等问题应返回单行汇总。
    group_requested = dimension is not None

    if any(term in q for term in ["月份", "月度", "趋势", "month"]) and date:
        dimension_expr, alias = f"substr({_quoted(date)}, 1, 7)", "月份"
        group_requested = True
    else:
        dimension_expr = _quoted(dimension) if dimension else None

    # 用户明确问“销量/数量”时优先使用数量列，否则默认使用金额列。
    if quantity and any(term in q for term in ["销量", "数量", "件数"]):
        metric = quantity
    else:
        metric = amount or quantity or (str(numeric[0]) if numeric else None)
    where = ""
    if status and amount and "退款" not in q and not (count_requested and not any(term in q for term in ["销售额", "金额", "销售", "revenue", "sales"])):
        values = set(df[status].dropna().astype(str).head(200).tolist())
        if "已支付" in values:
            where = f" WHERE {_quoted(status)} = '已支付'"
    if status and "退款" in q:
        where = f" WHERE {_quoted(status)} = '退款'"

    if group_requested and dimension_expr and metric:
        count_expr = f"COUNT(DISTINCT {_quoted(id_col)})" if id_col else "COUNT(*)"
        order_expr = dimension_expr if alias == "月份" else "数值合计 DESC"
        return (
            f"SELECT {dimension_expr} AS {alias}, ROUND(SUM({_quoted(metric)}), 2) AS 数值合计, "
            f"{count_expr} AS 记录数 FROM uploaded_data{where} GROUP BY {dimension_expr} "
            f"ORDER BY {order_expr} LIMIT 200"
        )
    if count_requested and not any(term in q for term in ["销售额", "金额", "销售", "revenue", "sales"]):
        count_expr = f"COUNT(DISTINCT {_quoted(id_col)})" if id_col else "COUNT(*)"
        return f"SELECT {count_expr} AS 记录数 FROM uploaded_data{where}"
    if metric:
        return (
            f"SELECT COUNT(*) AS 记录数, ROUND(SUM({_quoted(metric)}), 2) AS 数值合计, "
            f"ROUND(AVG({_quoted(metric)}), 2) AS 数值平均值 FROM uploaded_data{where}"
        )
    return "SELECT * FROM uploaded_data LIMIT 200"


def validate_sql(sql: str) -> tuple[bool, str]:
    cleaned = re.sub(r"/\*.*?\*/|--[^\r\n]*", " ", sql, flags=re.S).strip()
    if not cleaned:
        return False, "未生成 SQL。"
    without_trailing = cleaned.rstrip().rstrip(";").strip()
    if ";" in without_trailing:
        return False, "仅允许执行一条 SQL。"
    normalized = re.sub(r"\s+", " ", without_trailing).lower()
    if not re.match(r"^(select|with)\b", normalized):
        return False, "仅允许 SELECT/WITH 只读查询。"
    forbidden = (
        r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|"
        r"vacuum|reindex|analyze|load_extension)\b|sqlite_(master|schema)"
    )
    if re.search(forbidden, normalized):
        return False, "检测到写入、系统表或危险关键字，查询已拦截。"
    return True, ""


def _readonly_authorizer(action: int, _arg1: str, _arg2: str, _db: str, _trigger: str) -> int:
    blocked_names = [
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW",
        "SQLITE_ALTER_TABLE", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_PRAGMA",
        "SQLITE_REINDEX", "SQLITE_ANALYZE",
    ]
    blocked = {getattr(sqlite3, name) for name in blocked_names if hasattr(sqlite3, name)}
    return sqlite3.SQLITE_DENY if action in blocked else sqlite3.SQLITE_OK


def run_query(sql: str, data: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """在内存数据库只读执行，限制运行时间和返回行数。"""
    valid, error = validate_sql(sql)
    if not valid:
        raise ValueError(error)
    conn = sqlite3.connect(":memory:")
    try:
        data.to_sql("uploaded_data", conn, index=False, if_exists="replace")
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_readonly_authorizer)
        started = time.monotonic()
        conn.set_progress_handler(lambda: 1 if time.monotonic() - started > QUERY_TIMEOUT_SECONDS else 0, 10_000)
        cursor = conn.execute(sql.rstrip().rstrip(";"))
        columns = [item[0] for item in (cursor.description or [])]
        rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        truncated = len(rows) > MAX_RESULT_ROWS
        return pd.DataFrame(rows[:MAX_RESULT_ROWS], columns=columns), truncated
    finally:
        conn.close()


def fallback_insight(question: str, result: pd.DataFrame) -> str:
    if result.empty:
        return "本次查询没有返回记录，建议检查筛选条件或数据范围。"
    numeric = result.select_dtypes(include="number").columns.tolist()
    categorical = [c for c in result.columns if c not in numeric]
    if numeric and categorical and len(result) > 1:
        metric = numeric[0]
        valid_values = result[metric].dropna()
        if not valid_values.empty:
            best_index = valid_values.idxmax()
            return f"结果共 {len(result)} 行；{result.loc[best_index, categorical[0]]} 的{metric}最高，为 {result.loc[best_index, metric]:,.2f}。"
    if numeric:
        values = []
        for col in numeric[:3]:
            value = result[col].dropna()
            if not value.empty:
                values.append(f"{col}为 {value.iloc[0]:,.2f}")
        if values:
            return f"查询返回 {len(result)} 行，核心指标：{'，'.join(values)}。"
        return f"查询返回 {len(result)} 行，但数值字段没有有效数据。"
    return f"查询返回 {len(result)} 行，可继续增加时间、地区或类别条件进行细分。"


def generate_insight(question: str, sql: str, result: pd.DataFrame, api_key: str, model: str, base_url: str) -> str:
    if not api_key:
        return fallback_insight(question, result)
    data = result.head(20).where(pd.notna(result.head(20)), None).to_dict(orient="records")
    prompt = f"""你是业务数据分析师。根据问题和查询结果，用 1 至 3 句话给出客观结论。
要求：引用关键数字；不要臆测原因；信息不足时说明还需要什么数据；只输出正文。
问题：{question}
SQL：{sql}
结果：{json.dumps(data, ensure_ascii=False, default=str)}"""
    response = _client(api_key, base_url).chat.completions.create(
        model=model, temperature=0.2, messages=[{"role": "user", "content": prompt}]
    )
    return (response.choices[0].message.content or "").strip()

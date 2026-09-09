"""会议活动运营 Agent

面向会务运营的端到端演示：资料整理、嘉宾邀约、提醒编排、会后总结。
配置 OPENAI_API_KEY 后使用大语言模型；未配置时使用可演示的规则模板。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

import streamlit as st


def call_llm(prompt: str, api_key: str, model: str, base_url: str) -> dict[str, Any]:
    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "你是专业的会议活动运营 Agent，只返回合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def fallback_pre_event(title: str, date: str, location: str, audience: str, agenda: str, guests: str) -> dict[str, Any]:
    guest_lines = [x.strip() for x in re.split(r"[\n,，;；]+", guests) if x.strip()]
    guest_lines = guest_lines or ["待补充嘉宾"]
    return {
        "event_card": {
            "活动名称": title or "AI 主题分享会",
            "时间": date or "待确认",
            "地点/线上链接": location or "待确认",
            "目标人群": audience or "对 AI 感兴趣的开发者",
            "活动目标": "提升目标人群参与度，沉淀可复用的活动内容与线索",
            "流程": agenda or "开场 10 分钟；主题分享 40 分钟；圆桌问答 30 分钟；合影与交流 10 分钟",
        },
        "guest_invites": [
            {"嘉宾": g, "邀约标题": f"邀请您参加「{title or 'AI 主题分享会'}」", "邀约正文": f"您好！我们诚挚邀请您参与本次活动，期待分享您的实践经验。活动时间：{date or '待确认'}，地点：{location or '待确认'}。如您方便，烦请回复确认。", "跟进时间": "发送邀约后 2 天"}
            for g in guest_lines
        ],
        "reminders": [
            {"节点": "活动前 7 天", "对象": "已报名用户", "渠道": "短信/邮件", "内容": "活动预告与日程确认"},
            {"节点": "活动前 1 天", "对象": "嘉宾与工作人员", "渠道": "企业微信", "内容": "彩排、到场时间与物料清单确认"},
            {"节点": "活动前 2 小时", "对象": "已报名用户", "渠道": "短信/社群", "内容": "入场提醒、地址与线上链接"},
            {"节点": "活动开始后 30 分钟", "对象": "未签到用户", "渠道": "短信", "内容": "提醒尽快入场"},
        ],
        "source": "规则演示模式",
    }


def fallback_post_event(title: str, notes: str, attendees: str) -> dict[str, Any]:
    lines = [x.strip(" -•\t") for x in notes.splitlines() if x.strip()]
    highlights = lines[:3] or ["围绕主题完成经验分享与现场交流", "收集参会者反馈，形成后续内容线索"]
    return {
        "summary": f"本次「{title or '会议活动'}」共记录 {attendees or '若干'} 位参会者。活动围绕核心议题展开分享与交流，整体流程顺畅，建议持续沉淀内容并跟进高意向线索。",
        "highlights": highlights,
        "todos": [
            {"事项": "整理并发布活动回顾", "负责人": "运营", "截止时间": "活动后 2 个工作日", "优先级": "高"},
            {"事项": "向参会者发送资料与反馈问卷", "负责人": "运营", "截止时间": "活动后 1 个工作日", "优先级": "高"},
            {"事项": "跟进嘉宾及高意向线索", "负责人": "项目负责人", "截止时间": "活动后 3 个工作日", "优先级": "中"},
        ],
        "metrics": {"参会人数": attendees or "待补充", "内容产出": "活动回顾 + 嘉宾金句", "下一步": "问卷回收与线索跟进"},
        "source": "规则演示模式",
    }


def pre_event(title: str, date: str, location: str, audience: str, agenda: str, guests: str, api_key: str, model: str, base_url: str) -> dict[str, Any]:
    prompt = f"""请为会议活动生成运营执行包。返回 JSON，字段必须为 event_card(对象)、guest_invites(数组)、reminders(数组)。
event_card 包含：活动名称、时间、地点/线上链接、目标人群、活动目标、流程。
guest_invites 每项包含：嘉宾、邀约标题、邀约正文、跟进时间。reminders 每项包含：节点、对象、渠道、内容。
资料：活动名称={title}；时间={date}；地点={location}；目标人群={audience}；流程={agenda}；嘉宾={guests}"""
    if api_key.strip():
        try:
            result = call_llm(prompt, api_key.strip(), model.strip(), base_url.strip())
            result["source"] = "大语言模型"
            return result
        except Exception as exc:
            st.warning(f"模型调用失败，已切换到规则演示：{exc}")
    return fallback_pre_event(title, date, location, audience, agenda, guests)


def post_event(title: str, notes: str, attendees: str, api_key: str, model: str, base_url: str) -> dict[str, Any]:
    prompt = f"""请根据会议纪要生成会后运营复盘。只返回 JSON，字段为 summary(字符串)、highlights(字符串数组)、todos(数组)、metrics(对象)。
todos 每项包含：事项、负责人、截止时间、优先级；metrics 包含：参会人数、内容产出、下一步。
活动名称：{title}\n参会人数：{attendees}\n会议纪要：\n{notes}"""
    if api_key.strip():
        try:
            result = call_llm(prompt, api_key.strip(), model.strip(), base_url.strip())
            result["source"] = "大语言模型"
            return result
        except Exception as exc:
            st.warning(f"模型调用失败，已切换到规则演示：{exc}")
    return fallback_post_event(title, notes, attendees)


def markdown_export(pre: dict[str, Any] | None, post: dict[str, Any] | None) -> str:
    out = ["# 会议活动运营 Agent 输出", f"生成时间：{datetime.now():%Y-%m-%d %H:%M}"]
    if pre:
        out += ["\n## 会前执行包", "### 活动卡片"]
        out += [f"- {k}：{v}" for k, v in pre.get("event_card", {}).items()]
        out += ["\n### 嘉宾邀约"]
        for x in pre.get("guest_invites", []): out += [f"**{x.get('嘉宾','')}**：{x.get('邀约正文','')}（{x.get('跟进时间','')}）"]
        out += ["\n### 提醒计划"]
        out += [f"- {x.get('节点')}｜{x.get('对象')}｜{x.get('渠道')}：{x.get('内容')}" for x in pre.get("reminders", [])]
    if post:
        out += ["\n## 会后总结", post.get("summary", ""), "\n### 亮点"]
        out += [f"- {x}" for x in post.get("highlights", [])]
        out += ["\n### 待办"]
        out += [f"- {x.get('事项')}｜负责人：{x.get('负责人')}｜截止：{x.get('截止时间')}｜优先级：{x.get('优先级')}" for x in post.get("todos", [])]
    return "\n".join(out)


st.set_page_config(page_title="会议活动运营 Agent", page_icon="📅", layout="wide")
st.title("📅 会议活动运营 Agent")
st.caption("一份资料，自动完成会前筹备与会后复盘")

with st.sidebar:
    st.header("模型设置")
    api_key = st.text_input("DeepSeek API Key（可选）", value=os.getenv("DEEPSEEK_API_KEY", ""), type="password")
    base_url = st.text_input("API Base URL", value=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = st.text_input("模型 ID", value=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"), help="已按你的要求预填 deepseek-v4-pro；若接口提示模型不存在，请改成服务商控制台显示的准确模型 ID。")
    st.info("未配置 Key 时使用规则演示模式；接入 DeepSeek Key 后自动切换为大语言模型。")

tab_pre, tab_post, tab_export = st.tabs(["会前筹备", "会后总结", "导出结果"])

with tab_pre:
    st.subheader("1. 填写活动资料")
    c1, c2 = st.columns(2)
    with c1:
        title = st.text_input("活动名称", "AI 产品增长实战分享会", key="pre_title")
        date = st.text_input("时间", "2024-07-20 14:00-16:00", key="pre_date")
        location = st.text_input("地点/线上链接", "北京·科技园 A101 / 腾讯会议", key="pre_location")
    with c2:
        audience = st.text_input("目标人群", "AI 产品经理、开发者和运营同学", key="pre_audience")
        guests = st.text_area("嘉宾名单（每行一位）", "林老师｜AI 产品负责人\n周同学｜增长负责人", height=100, key="pre_guests")
    agenda = st.text_area("活动流程或原始资料", "14:00 开场\n14:10 AI 产品增长案例\n15:00 圆桌问答\n15:40 自由交流", height=110, key="pre_agenda")
    if st.button("生成会前执行包", type="primary", key="pre_run"):
        st.session_state.pre_result = pre_event(title, date, location, audience, agenda, guests, api_key, model, base_url)
    if st.session_state.get("pre_result"):
        result = st.session_state.pre_result
        st.success(f"执行包已生成 · {result.get('source', '')}")
        st.subheader("活动卡片")
        st.json(result.get("event_card", {}), expanded=True)
        st.subheader("嘉宾邀约文案")
        for item in result.get("guest_invites", []):
            with st.expander(item.get("嘉宾", "嘉宾"), expanded=True):
                st.write(f"**{item.get('邀约标题', '')}**")
                st.write(item.get("邀约正文", ""))
                st.caption(f"跟进时间：{item.get('跟进时间', '')}")
        st.subheader("自动提醒计划")
        st.dataframe(result.get("reminders", []), use_container_width=True, hide_index=True)

with tab_post:
    st.subheader("2. 粘贴会议纪要，生成会后复盘")
    post_title = st.text_input("活动名称", "AI 产品增长实战分享会", key="post_title")
    attendees = st.text_input("参会人数", "86", key="post_attendees")
    notes = st.text_area("会议纪要/聊天记录", "- 嘉宾分享了用户增长实验方法\n- 现场提问集中在 AI Agent 落地\n- 多位参会者希望获得案例资料", height=180)
    if st.button("生成会后总结", type="primary", key="post_run"):
        st.session_state.post_result = post_event(post_title, notes, attendees, api_key, model, base_url)
    if st.session_state.get("post_result"):
        result = st.session_state.post_result
        st.success(f"会后总结已生成 · {result.get('source', '')}")
        st.write(result.get("summary", ""))
        st.subheader("活动亮点")
        for item in result.get("highlights", []): st.markdown(f"- {item}")
        st.subheader("跟进待办")
        st.dataframe(result.get("todos", []), use_container_width=True, hide_index=True)
        st.subheader("关键指标")
        st.json(result.get("metrics", {}), expanded=True)

with tab_export:
    export = markdown_export(st.session_state.get("pre_result"), st.session_state.get("post_result"))
    st.code(export, language="markdown")
    st.download_button("下载运营复盘 Markdown", export.encode("utf-8"), "meeting_ops_report.md", "text/markdown", use_container_width=True)

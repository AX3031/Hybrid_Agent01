import json
import uuid
from typing import Iterator

import streamlit as st

from agent.react_agent import AgentEvent, ReactAgent
from memorys.memory_factory import DEFAULT_MEMORY_MODE, MEMORY_MODE_LABELS

DEFAULT_SESSION_ID = "user_001"
GREETING = {"role": "assistant", "content": "你好，我可以查看这台电脑的信息，也能帮你整理文件、执行命令。你想做什么？"}

# 记忆按钮上显示的精简名称
MODE_SHORT_LABELS = {
    "short_term": "短期记忆",
    "file": "本地文件",
    "postgres": "PostgreSQL",
}

# 各记忆方式的存储位置提示，展示在记忆选择弹层里
MEMORY_MODE_HINTS = {
    "short_term": "仅在当前进程内保留，刷新页面或重启即丢失",
    "file": "写入 memorys/chat_history/agent_checkpoints.json",
    "postgres": "写入 PostgreSQL，可跨进程/多实例恢复",
}


def _load_history(agent: ReactAgent | None) -> list[dict]:
    """读取当前会话 ID 在记忆后端中的历史消息；失败时按无历史处理。"""
    if agent is None:
        return []
    try:
        return agent.load_ui_messages(st.session_state["session_id"])
    except Exception:
        return []


def _reset_chat(agent: ReactAgent | None = None, history: list[dict] | None = None) -> None:
    """重建 UI 消息列表：欢迎语 + 已有历史。"""
    if history is None:
        history = _load_history(agent)
    st.session_state["message"] = [dict(GREETING), *history]


def _shorten(text: str, limit: int = 14) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _render_events(event_stream: Iterator[AgentEvent]) -> str:
    """渲染 Agent 事件流：文本直接显示，危险操作暂存到 session_state 等待确认。"""
    placeholder = st.chat_message("assistant").empty()
    buffer = ""

    for event in event_stream:
        if event.type == "text":
            buffer += event.content
            placeholder.markdown(buffer)
        elif event.type == "interrupt":
            st.session_state["pending_hitl"] = {"actions": event.action_requests}

    if not buffer:
        placeholder.empty()
    return buffer.strip()


def _render_hitl_block(agent: ReactAgent) -> None:
    """渲染待确认操作卡片；用户批准或拒绝后继续执行被挂起的工具调用。"""
    pending = st.session_state.get("pending_hitl")
    if not pending:
        return

    actions = pending.get("actions", [])
    session_id = st.session_state["session_id"]

    with st.container(border=True):
        st.warning("以下操作会修改你的电脑，请确认后执行：")
        for action in actions:
            st.code(
                json.dumps(
                    {"tool": action.get("name"), "args": action.get("args")},
                    ensure_ascii=False,
                    indent=2,
                ),
                language="json",
            )

        col_approve, col_reject = st.columns(2)
        approved = col_approve.button("批准执行", type="primary", use_container_width=True)
        rejected = col_reject.button("拒绝执行", use_container_width=True)

    if not (approved or rejected):
        return

    if approved:
        decisions = [{"type": "approve"} for _ in actions]
    else:
        decisions = [{"type": "reject", "message": "用户拒绝执行该操作"} for _ in actions]

    st.session_state.pop("pending_hitl", None)
    content = _render_events(agent.resume_stream(decisions, thread_id=session_id))
    if content:
        st.session_state["message"].append({"role": "assistant", "content": content})
    st.rerun()


# 标题
st.title("电脑助手")
st.divider()

if "session_id" not in st.session_state:
    st.session_state["session_id"] = DEFAULT_SESSION_ID
if "session_input_nonce" not in st.session_state:
    st.session_state["session_input_nonce"] = 0

# ---------------- 底部输入栏：记忆选择按钮 + 对话输入框 ----------------
mode_labels = list(MEMORY_MODE_LABELS.keys())
mode_values = list(MEMORY_MODE_LABELS.values())
current_mode = st.session_state.get("memory_mode", DEFAULT_MEMORY_MODE)
current_label = mode_labels[mode_values.index(current_mode)] if current_mode in mode_values else mode_labels[0]

# 控件值在本次脚本开始前已写入 session_state，据此让按钮文案与弹层选中项保持一致
stored_label = st.session_state.get("memory_mode_selector")
if stored_label in mode_labels:
    current_label = stored_label

with st.bottom:
    composer = st.container(horizontal=True, vertical_alignment="center", gap="small")
    with composer:
        selected_label = current_label
        with st.popover(
            MODE_SHORT_LABELS[MEMORY_MODE_LABELS[current_label]],
            icon=":material/psychology:",
            type="tertiary",
            width="content",
        ):
            picked_label = st.radio(
                "会话记忆方式",
                options=mode_labels,
                index=mode_labels.index(current_label),
                key="memory_mode_selector",
            )
            selected_label = picked_label or current_label
            selected_mode = MEMORY_MODE_LABELS[selected_label]
            st.caption(f"{MEMORY_MODE_HINTS[selected_mode]}")

        prompt = st.chat_input("告诉我你想在这台电脑上做什么", width="stretch")

selected_mode = MEMORY_MODE_LABELS[selected_label]

# ---------------- 侧边栏：会话管理（后续在此列出不同会话记录）----------------
with st.sidebar:
    st.subheader("会话管理")

    st.text_input(
        "会话 ID",
        value=st.session_state["session_id"],
        key=f"session_id_input_{st.session_state['session_input_nonce']}",
        help="相同 ID 在本地文件/PostgreSQL 模式下会恢复历史",
    )

    col_new, col_clear = st.columns(2)
    new_session = col_new.button("新建会话", use_container_width=True)
    clear_memory = col_clear.button("清空记忆", use_container_width=True)

    st.caption("记忆方式在输入框左侧的按钮里切换；这里按会话 ID 保存与恢复记录。")

# 记忆方式变化：销毁旧 Agent，稍后按新模式重建
mode_changed = st.session_state.get("memory_mode") != selected_mode
if mode_changed:
    st.session_state["memory_mode"] = selected_mode
    st.session_state.pop("agent", None)
    st.session_state.pop("pending_hitl", None)

# 新建会话：使用新的随机 ID，并立即刷新界面
if new_session:
    st.session_state["session_id"] = str(uuid.uuid4())
    st.session_state["session_input_nonce"] += 1
    _reset_chat(history=[])
    st.rerun()

# 手动修改会话 ID
entered_session_id = st.session_state[f"session_id_input_{st.session_state['session_input_nonce']}"].strip()
session_changed = bool(entered_session_id) and entered_session_id != st.session_state["session_id"]
if session_changed:
    st.session_state["session_id"] = entered_session_id
    st.session_state.pop("pending_hitl", None)

# 创建 Agent；PostgreSQL 连接失败时回退到短期记忆，避免页面无法使用
if "agent" not in st.session_state:
    try:
        st.session_state["agent"] = ReactAgent(memory_mode=selected_mode)
        st.session_state["agent_mode"] = selected_mode
        st.session_state.pop("memory_error", None)
    except Exception as exc:
        st.session_state["agent"] = ReactAgent(memory_mode=DEFAULT_MEMORY_MODE)
        st.session_state["agent_mode"] = DEFAULT_MEMORY_MODE
        st.session_state["memory_error"] = str(exc)

# 切换记忆方式/会话 ID 后，从对应后端恢复历史
if mode_changed or session_changed or "message" not in st.session_state:
    _reset_chat(st.session_state["agent"])

if clear_memory:
    try:
        st.session_state["agent"].clear_memory(st.session_state["session_id"])
        _reset_chat(history=[])
        st.sidebar.success("已清空当前会话记忆")
    except Exception as exc:
        st.sidebar.error(f"清空记忆失败：{exc}")

# 记忆初始化失败提示（例如 PostgreSQL 不可达时已回退为短期记忆）
if st.session_state.get("memory_error"):
    st.warning(f"「{selected_label}」初始化失败，已回退为短期记忆：{st.session_state['memory_error']}")

for message in st.session_state["message"]:
    st.chat_message(message["role"]).write(message["content"])

# 有待确认的危险操作时，在消息区展示确认卡片
_render_hitl_block(st.session_state["agent"])

if prompt:
    st.chat_message("user").write(prompt)
    st.session_state["message"].append({"role": "user", "content": prompt})

    with st.spinner("正在处理..."):
        try:
            content = _render_events(
                st.session_state["agent"].execute_stream(
                    prompt, thread_id=st.session_state["session_id"]
                )
            )
        except Exception as exc:
            st.error(f"生成回答失败：{exc}")
            content = ""

    if content:
        st.session_state["message"].append({"role": "assistant", "content": content})
    st.rerun()

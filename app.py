import json
import uuid
from typing import Iterator

import streamlit as st

from agent.react_agent import AgentEvent, ReactAgent
from agent.guard import approval, journal
from agent.guard import state as guard_state
from agent.guard.config import read_only_enabled, set_read_only, task_defaults
from agent.guard.lock import TaskBusyError
from agent.guard.policy import requires_second_subject, reversibility_of
from agent.guard.scope import ROLLBACK_BOUNDARY_TEXT, rollback_note
from agent.guard.task_card import ALL_ACTIONS, DEFAULT_FORBIDDEN, TaskCard
from agent.tools.safety import (
    describe_operation,
    expand_path,
    in_workspace,
    measure_path,
    operation_risk,
    operation_target,
    save_workspace_roots,
    summarize_result,
    workspace_roots,
    workspace_text,
)
from memorys.memory_factory import DEFAULT_MEMORY_MODE, MEMORY_MODE_LABELS
from utils import audit_logger
from utils.logger_handler import logger

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


def _render_events(event_stream: Iterator[AgentEvent]) -> tuple[str, list[dict]]:
    """渲染 Agent 事件流，返回（回答文本, 本次操作记录）。"""
    placeholder = st.chat_message("assistant").empty()
    buffer = ""
    operations: list[dict] = []

    for event in event_stream:
        if event.type == "text":
            buffer += event.content
            placeholder.markdown(buffer)
        elif event.type == "interrupt":
            st.session_state["pending_hitl"] = {
                "actions": event.action_requests,
                "batch_id": event.batch_id,
                "task_card": event.task_card,
            }
        elif event.type == "tool" and event.tool_call:
            operations.append(event.tool_call)
        elif event.type == "verification" and event.content:
            buffer += f"\n\n【机器判据】\n{event.content}\n"
            placeholder.markdown(buffer)

    if not buffer:
        placeholder.empty()
    return buffer.strip(), operations


def _render_operations(operations: list[dict], expanded: bool = True) -> None:
    """用大白话展示本次做了哪些操作。"""
    if not operations:
        return
    with st.expander(f"本次操作记录（{len(operations)} 步）", expanded=expanded):
        for index, operation in enumerate(operations, start=1):
            name = operation.get("name", "")
            args = operation.get("args", {}) or {}
            title, detail = describe_operation(name, args)
            status = operation.get("status") or "success"
            status_text = "已完成" if status == "success" else "未完成"
            st.markdown(f"**{index}. {title}（{status_text}）**")
            st.write(detail)
            st.caption(f"结果：{summarize_result(operation.get('result', ''))}")


def _render_hitl_block(agent: ReactAgent) -> None:
    """渲染待确认操作卡片；R2/R3 动作额外要求审批口令。"""
    pending = st.session_state.get("pending_hitl")
    if not pending:
        return

    actions = pending.get("actions", [])
    session_id = st.session_state["session_id"]
    batch_id = pending.get("batch_id") or guard_state.current_batch_id()
    task_card = TaskCard.from_dict(pending.get("task_card"))
    needs_pin = any(
        requires_second_subject(action.get("name", ""), action.get("args") or {})
        for action in actions
    )
    pin_key = f"hitl_pin_{batch_id or 'no_batch'}_{len(actions)}"

    with st.container(border=True):
        st.warning(
            f"下面有 {len(actions)} 个操作需要你确认。"
            "它们会操作这台电脑（修改文件、设置或控制界面），确认前请看清每一步。"
        )
        if batch_id:
            st.caption(f"批次号：{batch_id}（整批改动可在侧边栏「批次与回滚」里一次撤销）")

        for index, action in enumerate(actions, start=1):
            name = action.get("name", "")
            args = action.get("args", {}) or {}
            title, detail = describe_operation(name, args)
            target = operation_target(name, args)
            risk = operation_risk(name, args)
            level = reversibility_of(name, args)

            st.markdown(f"**{index}. {title}（可逆性 {level}）**")
            st.write(detail)
            if target:
                st.code(target, language=None)

            if name == "delete_path":
                try:
                    path = expand_path(str(args.get("path", "")))
                    if in_workspace(path) and path.exists():
                        st.info(f"影响范围：{measure_path(path)}")
                except OSError:
                    pass

            st.caption(f"需要注意：{risk}")
            st.caption(f"回滚边界：{rollback_note(name, args)}")

        if needs_pin:
            if approval.pin_configured():
                st.text_input(
                    "审批口令（R2/R3 动作需要第二主体）",
                    type="password",
                    key=pin_key,
                    help="口令只在本机校验，不会写进提示词、对话历史或审计日志。",
                )
            else:
                st.error(approval.pin_status_text())

        col_approve, col_reject = st.columns(2)
        approved = col_approve.button(
            "我确认，执行",
            type="primary",
            use_container_width=True,
            disabled=needs_pin and not approval.pin_configured(),
        )
        rejected = col_reject.button("不要执行", use_container_width=True)

    if not (approved or rejected):
        return

    if rejected:
        audit_logger.log_approval(
            session_id, batch_id, "reject", actions, approver="human"
        )
        decisions = [{"type": "reject", "message": "用户拒绝执行该操作"} for _ in actions]
    else:
        token = ""
        if needs_pin:
            pin_value = st.session_state.get(pin_key, "")
            if not approval.verify_pin(pin_value):
                st.error("审批口令不正确，操作未执行。请核对后重试。")
                return
            try:
                token = approval.sign_approval(session_id, batch_id, actions)
            except OSError as exc:
                st.error(f"生成审批凭证失败：{exc}")
                return
            guard_state.set_approval_context(
                token,
                "human",
                actions,
                thread_id=session_id,
                batch_id=batch_id,
            )
        audit_logger.log_approval(
            session_id, batch_id, "approve", actions, token=token, approver="human"
        )
        decisions = [{"type": "approve"} for _ in actions]

    st.session_state.pop("pending_hitl", None)
    st.session_state.pop(pin_key, None)

    try:
        content, operations = _render_events(
            agent.resume_stream(
                decisions,
                thread_id=session_id,
                pending_actions=actions,
                batch_id=batch_id or None,
                task_card=task_card,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 界面需要保留现场并允许重试
        logger.exception("[app]继续执行待确认操作失败")
        st.error(f"继续执行失败：{exc}")
        st.session_state["pending_hitl"] = pending
        return
    finally:
        guard_state.clear_approval_context()

    if content or operations:
        st.session_state["message"].append(
            {
                "role": "assistant",
                "content": content or "（操作已处理，详见下方记录）",
                "operations": operations,
            }
        )
    st.rerun()


def _on_readonly_toggle() -> None:
    """侧边栏只读开关：写入 config/agent.yml，立即生效。"""
    set_read_only(bool(st.session_state.get("read_only_toggle", False)))


def _render_task_card_section() -> None:
    """任务卡表单；启用后写进提示词与审计。"""
    defaults = task_defaults()
    active = TaskCard.from_dict(st.session_state.get("task_card"))

    with st.expander("任务卡（必须填写）", expanded=bool(active)):
        st.caption(
            "每个任务都必须有任务卡；缺目标/成功判据/机器判据（写入类）无法启用。"
            "任务结束会由代码核对机器判据，未通过/未确认不能算完成。"
        )
        st.caption(
            "如果禁止动作里保留「发送/提交/付款」，所有 GUI 输入类动作都会被保守拒绝；"
            "系统无法判断一次点击是否等于提交。run_command/open_path 不在此语义拦截内，仍需人工确认 + PIN。"
        )
        if active:
            st.success(active.summary_text())
            st.caption("成功判据：" + "；".join(active.success_criteria))
            st.caption(
                "机器判据：" + ("；".join(active.machine_checks) if active.machine_checks else "未配置（只能标未确认）")
            )
            st.caption(f"回滚边界：{ROLLBACK_BOUNDARY_TEXT}")
            if st.button("清除任务卡", key="clear_task_card", use_container_width=True):
                st.session_state.pop("task_card", None)
                st.rerun()

        with st.form("task_card_form"):
            name = st.text_input("任务名", value=active.name if active else "")
            user = st.text_input("使用者", value=active.user if active else "")
            goal = st.text_area(
                "目标", value=active.goal if active else "", height=80,
                placeholder="例如：把下载目录里的发票按 年月_金额_供应商 改名并归档",
            )
            criteria = st.text_area(
                "成功判据（人话；每行一条）",
                value="\n".join(active.success_criteria) if active else "",
                height=100,
                placeholder="例如：归档目录文件数 = 源目录文件数\n无重名文件",
            )
            machine_checks = st.text_area(
                "机器判据（写入/命令/打开类必填；每行一条）",
                value="\n".join(active.machine_checks) if active else "",
                height=100,
                placeholder=(
                    "exists|D:\\输出\\结果.xlsx\n"
                    "file_count|D:\\输出|3\n"
                    "sha256|D:\\输出\\结果.xlsx|64位十六进制"
                ),
            )
            input_scope = st.text_area(
                "输入范围（每行一个目录；留空 = 工作区只读范围）",
                value="\n".join(active.input_scope) if active else "",
                height=70,
            )
            output_location = st.text_input(
                "输出位置（目录；留空 = 只受工作区限制）",
                value=active.output_location if active else "",
            )
            allowed = st.multiselect(
                "允许的动作",
                ALL_ACTIONS,
                default=active.allowed_actions if active and active.allowed_actions else [ALL_ACTIONS[0]],
                help="默认只允许「读」；需要写入/命令/桌面时再显式选择。",
            )
            forbidden = st.multiselect(
                "禁止的动作",
                ALL_ACTIONS + DEFAULT_FORBIDDEN,
                default=active.forbidden_actions if active else DEFAULT_FORBIDDEN,
            )
            col_steps, col_minutes, col_cost = st.columns(3)
            steps = col_steps.number_input(
                "步数上限", min_value=1, max_value=200,
                value=int(active.max_steps if active else defaults["max_steps"]),
            )
            minutes = col_minutes.number_input(
                "时长上限（分钟）", min_value=1, max_value=120,
                value=int(active.max_minutes if active else defaults["max_minutes"]),
            )
            cost = col_cost.number_input(
                "费用上限（元）", min_value=0.1, max_value=100.0,
                value=float(active.max_cost_yuan if active else defaults["max_cost_yuan"]),
                step=0.5,
            )
            valid_minutes = st.number_input(
                "有效期（分钟）", min_value=1, max_value=1440,
                value=int(active.valid_minutes if active else 120),
            )
            rollback_note_value = st.text_area(
                "回滚说明（可选）",
                value=active.rollback_note if active else "",
                height=60,
                placeholder="例如：只回滚文件类改动；命令/GUI 不在回滚范围",
            )
            st.caption(f"回滚边界：{ROLLBACK_BOUNDARY_TEXT}")
            uncertain = st.text_input(
                "不确定时怎么办",
                value=active.on_uncertain if active else "标记 UNKNOWN 并停下来问人",
            )
            submitted = st.form_submit_button("启用任务卡", type="primary", use_container_width=True)

        if submitted:
            card = TaskCard(
                name=name.strip(),
                user=user.strip(),
                goal=goal.strip(),
                success_criteria=[line.strip() for line in criteria.splitlines() if line.strip()],
                machine_checks=[line.strip() for line in machine_checks.splitlines() if line.strip()],
                input_scope=[line.strip() for line in input_scope.splitlines() if line.strip()],
                output_location=output_location.strip(),
                allowed_actions=allowed,
                forbidden_actions=forbidden,
                max_steps=int(steps),
                max_minutes=int(minutes),
                max_cost_yuan=float(cost),
                valid_minutes=int(valid_minutes),
                rollback_note=rollback_note_value.strip(),
                on_uncertain=uncertain.strip(),
            )
            errors = card.validate()
            if errors:
                for error in errors:
                    st.error(error)
            else:
                st.session_state["task_card"] = card.to_dict()
                st.success("任务卡已启用；本轮及后续任务都会遵守，做完请清除。")
                st.rerun()


def _describe_batch_entry(entry: dict) -> str:
    op = entry.get("op", "")
    if op == "task_start":
        card = entry.get("card") or {}
        return f"任务开始：{card.get('name') or '（未使用任务卡）'}"
    if op == "write":
        return f"写入：{entry.get('path')}（{'覆盖' if entry.get('existed') else '新建'}）"
    if op == "mkdir":
        return f"创建目录：{entry.get('path')}"
    if op == "move":
        return f"移动：{entry.get('src')} -> {entry.get('dst')}"
    if op == "copy":
        return f"复制：{entry.get('src')} -> {entry.get('dst')}"
    if op == "rmdir":
        return f"删除空目录：{entry.get('path')}"
    if op == "trash":
        return f"移到回收区：{entry.get('src')} -> {entry.get('trash')}"
    if op == "delete_permanent":
        return f"永久删除（不可逆）：{entry.get('path')}"
    if op == "rollback":
        return (
            f"回滚：恢复 {entry.get('restored')} / 失败 {entry.get('failed')} / "
            f"不可逆 {entry.get('irreversible')}"
        )
    return f"{op}：{json.dumps(entry, ensure_ascii=False)[:160]}"


def _render_batch_section() -> None:
    """批次列表 + 回放 + 一键整批回滚。"""
    with st.expander("批次与回滚", expanded=False):
        st.caption("每个任务一个批次号；这里可以看回放，也可以撤销整批改动。")
        batches = journal.list_batches(12)
        if not batches:
            st.info("还没有任务批次。")
            return

        for batch in batches:
            with st.container(border=True):
                st.markdown(f"**{batch['batch_id']}**")
                st.caption(
                    f"{batch['task_name']}｜{batch['created_at']}｜{batch['ops']} 个操作"
                )
                if batch["goal"]:
                    st.caption(batch["goal"])
                if batch["irreversible"]:
                    st.warning(
                        f"包含 {batch['irreversible']} 个永久删除，整批回滚无法恢复这些项目。"
                    )
                if batch["rolled_back"]:
                    st.info("这个批次已经执行过回滚。")

                with st.expander("查看回放"):
                    for entry in batch["entries"]:
                        st.text(f"{entry.get('seq', '')}. {_describe_batch_entry(entry)}")

                confirm = st.checkbox(
                    "我确认要撤销这一批",
                    key=f"rollback_confirm_{batch['batch_id']}",
                )
                if st.button(
                    "撤销整批",
                    key=f"rollback_{batch['batch_id']}",
                    disabled=not confirm or batch["rolled_back"],
                    use_container_width=True,
                ):
                    report = journal.rollback_batch(batch["batch_id"])
                    if report.ok:
                        st.success(report.to_text())
                    else:
                        st.warning(report.to_text())
                    st.rerun()


# 标题
st.title("电脑助手")
st.divider()
if read_only_enabled():
    st.info("当前为只读模式：写操作（写入/移动/删除/命令/桌面输入）会被守卫层拒绝。")

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

        with st.popover(
            "工作区域",
            icon=":material/folder_open:",
            type="tertiary",
            width="content",
        ):
            st.markdown("**当前工作区域**")
            st.code(workspace_text(), language=None)
            st.caption(
                "所有文件操作（包括读取和搜索）只能在这里面执行。"
                "每行写一个绝对路径，例如 C:\\Users\\你的用户名\\Desktop。"
            )
            if "workspace_roots_input" not in st.session_state:
                st.session_state["workspace_roots_input"] = "\n".join(
                    str(root) for root in workspace_roots()
                )
            workspace_input = st.text_area(
                "工作区域根目录",
                height=140,
                key="workspace_roots_input",
                help="每行一个绝对路径；支持 ~（用户目录）和 .（项目根）。",
            )
            if st.button("保存并应用", type="primary", use_container_width=True):
                ok, message = save_workspace_roots(workspace_input.splitlines())
                if ok:
                    st.success(message)
                    st.session_state.pop("workspace_roots_input", None)
                    st.rerun()
                else:
                    st.error(message)

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

    st.divider()
    st.subheader("安全开关")
    st.toggle(
        "只读模式（拒绝所有写操作）",
        value=read_only_enabled(),
        key="read_only_toggle",
        on_change=_on_readonly_toggle,
        help="开启后保留读取/搜索/截图，但写入、移动、删除、命令和桌面输入都会被拒绝。",
    )
    stop_reason = guard_state.stop_reason()
    if stop_reason:
        st.error(f"急停中：{stop_reason}")
        if st.button("解除急停", use_container_width=True):
            guard_state.clear_stop()
            st.rerun()
    else:
        if st.button("急停（当前动作做完就停）", use_container_width=True):
            guard_state.request_stop("用户按下急停")
            st.rerun()
    st.caption(approval.pin_status_text())

    st.divider()
    _render_task_card_section()
    st.divider()
    _render_batch_section()

    st.divider()
    st.subheader("工作区域")
    st.caption("所有文件操作（包括读取）只能在这个范围内执行。")
    st.code(workspace_text(), language=None)
    st.caption("点击输入框左侧的「工作区域」按钮可以修改，保存后立即生效。")

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
    st.session_state.pop("task_card", None)
    _reset_chat(history=[])
    st.rerun()

# 手动修改会话 ID
entered_session_id = st.session_state[f"session_id_input_{st.session_state['session_input_nonce']}"].strip()
session_changed = bool(entered_session_id) and entered_session_id != st.session_state["session_id"]
if session_changed:
    st.session_state["session_id"] = entered_session_id
    st.session_state.pop("pending_hitl", None)
    st.session_state.pop("task_card", None)

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

message_list = st.session_state["message"]
last_index = len(message_list) - 1
for index, message in enumerate(message_list):
    st.chat_message(message["role"]).write(message["content"])
    # 最新一轮的操作记录默认展开，历史记录折叠
    _render_operations(message.get("operations", []), expanded=(index == last_index))

# 有待确认的危险操作时，在消息区展示确认卡片
_render_hitl_block(st.session_state["agent"])

if prompt:
    active_card = TaskCard.from_dict(st.session_state.get("task_card"))
    if active_card is None:
        st.warning("请先在侧边栏启用任务卡：每个任务都必须有目标、成功判据、范围和预算。")
        st.stop()
    errors = active_card.validate()
    if errors:
        st.error("任务卡当前不合法，请先修正：" + "；".join(errors))
        st.stop()

    st.chat_message("user").write(prompt)
    st.session_state["message"].append({"role": "user", "content": prompt})

    with st.spinner("正在处理..."):
        try:
            content, operations = _render_events(
                st.session_state["agent"].execute_stream(
                    prompt,
                    thread_id=st.session_state["session_id"],
                    task_card=active_card,
                )
            )
        except (TaskBusyError, guard_state.TaskStopped) as exc:
            st.warning(str(exc))
            content, operations = "", []
        except Exception as exc:
            # 生产环境只看一句 st.error 无法定位问题：把堆栈写进日志，界面保留简短提示
            logger.exception("[app]生成回答失败")
            st.error(f"生成回答失败：{exc}")
            st.caption("这一轮已中断；下次提问前会自动修复本轮留下的未完成工具调用，可以直接继续。")
            content, operations = "", []

    # 有操作等待确认时，本轮先不写历史，避免和确认后的结果重复
    if st.session_state.get("pending_hitl"):
        st.rerun()

    if content or operations:
        st.session_state["message"].append(
            {
                "role": "assistant",
                "content": content or "（操作已完成，详见下方记录）",
                "operations": operations,
            }
        )
    st.rerun()

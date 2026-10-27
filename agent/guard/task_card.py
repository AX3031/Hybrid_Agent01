"""任务卡（P0-1）。

任务卡不是给模型看的“建议”，而是这次任务的范围合同：

- 目标与成功判据：什么叫做完，由人写，机器可判定；
- 输入范围 / 输出位置：只允许在这些目录里读、在这些目录里写；
- 允许 / 禁止的动作：越界的工具调用会被守卫层直接拒绝；
- 预算与有效期：步数、时长、费用上限；
- 不确定时：给模型一个明确的“我不知道”出口。

卡里**永远不包含**审批口令（PIN）。PIN 只用于人工在界面上签署
R2/R3 动作，见 `agent/guard/approval.py`。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.guard.config import task_defaults
from agent.guard import criteria
from agent.guard.scope import ROLLBACK_BOUNDARY_TEXT

ACTION_READ = "读"
ACTION_WRITE = "写"
ACTION_MOVE = "移动"
ACTION_COPY = "复制"
ACTION_DELETE = "删除"
ACTION_COMMAND = "命令"
ACTION_OPEN = "打开"
ACTION_DESKTOP = "桌面"
ACTION_NETWORK = "联网"

ALL_ACTIONS = [
    ACTION_READ,
    ACTION_WRITE,
    ACTION_MOVE,
    ACTION_COPY,
    ACTION_DELETE,
    ACTION_COMMAND,
    ACTION_OPEN,
    ACTION_DESKTOP,
    ACTION_NETWORK,
]

DEFAULT_FORBIDDEN = ["发送", "提交", "付款", "提权"]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass
class TaskCard:
    """一张任务卡；字段与《小企业版》附录 B 对齐。"""

    name: str
    user: str
    goal: str
    success_criteria: list[str] = field(default_factory=list)
    machine_checks: list[str] = field(default_factory=list)
    input_scope: list[str] = field(default_factory=list)
    output_location: str = ""
    allowed_actions: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN))
    max_steps: int = 30
    max_minutes: int = 10
    max_cost_yuan: float = 2.0
    valid_minutes: int = 120
    rollback_note: str = ""
    on_uncertain: str = "标记 UNKNOWN 并停下来问人"

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "user": self.user,
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "machine_checks": list(self.machine_checks),
            "input_scope": list(self.input_scope),
            "output_location": self.output_location,
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "max_steps": int(self.max_steps),
            "max_minutes": int(self.max_minutes),
            "max_cost_yuan": float(self.max_cost_yuan),
            "valid_minutes": int(self.valid_minutes),
            "rollback_note": self.rollback_note,
            "on_uncertain": self.on_uncertain,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "TaskCard | None":
        if not data:
            return None
        defaults = task_defaults()
        return cls(
            name=str(data.get("name", "")).strip(),
            user=str(data.get("user", "")).strip(),
            goal=str(data.get("goal", "")).strip(),
            success_criteria=_as_list(data.get("success_criteria")),
            machine_checks=_as_list(data.get("machine_checks")),
            input_scope=_as_list(data.get("input_scope")),
            output_location=str(data.get("output_location", "")).strip(),
            allowed_actions=_as_list(data.get("allowed_actions")),
            forbidden_actions=_as_list(data.get("forbidden_actions")) or list(DEFAULT_FORBIDDEN),
            max_steps=int(data.get("max_steps") or defaults["max_steps"]),
            max_minutes=int(data.get("max_minutes") or defaults["max_minutes"]),
            max_cost_yuan=float(data.get("max_cost_yuan") or defaults["max_cost_yuan"]),
            valid_minutes=int(data.get("valid_minutes") or 120),
            rollback_note=str(data.get("rollback_note", "")).strip(),
            on_uncertain=str(data.get("on_uncertain") or "标记 UNKNOWN 并停下来问人").strip(),
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """返回错误列表；空列表表示可以启用。"""
        errors: list[str] = []
        if not self.name:
            errors.append("任务名不能为空")
        if not self.goal:
            errors.append("目标不能为空")
        if not self.success_criteria:
            errors.append("至少写一条成功判据（机器能判定的那种，例如文件数一致、合计金额一致）")
        if not self.allowed_actions:
            errors.append("至少允许一个动作类别（只读任务就选「读」）")

        # 写入/命令/打开类任务必须有可执行的机器判据；只有只读/纯 GUI 任务
        # 允许留空，但任务结束时只能标“未确认”。
        machine_required = {
            ACTION_WRITE,
            ACTION_MOVE,
            ACTION_COPY,
            ACTION_DELETE,
            ACTION_COMMAND,
            ACTION_OPEN,
        }
        if not self.machine_checks and any(
            action in machine_required for action in self.allowed_actions
        ):
            errors.append(
                "写入/命令/打开类任务至少需要一条机器判据，"
                "例如 exists|D:\\输出\\结果.xlsx 或 file_count|D:\\输出|3"
            )
        errors.extend(criteria.validate_machine_checks(self.machine_checks))

        if not (1 <= int(self.max_steps) <= 200):
            errors.append("步数上限必须在 1–200 之间")
        if not (1 <= int(self.max_minutes) <= 120):
            errors.append("时长上限必须在 1–120 分钟之间")
        if not (0.1 <= float(self.max_cost_yuan) <= 100.0):
            errors.append("费用上限必须在 0.1–100 元之间")
        if not (1 <= int(self.valid_minutes) <= 1440):
            errors.append("有效期必须在 1–1440 分钟之间")

        unknown_allowed = [item for item in self.allowed_actions if item not in ALL_ACTIONS]
        if unknown_allowed:
            errors.append(f"允许的动作里有未知项：{'、'.join(unknown_allowed)}")
        unknown_forbidden = [item for item in self.forbidden_actions if item not in ALL_ACTIONS + DEFAULT_FORBIDDEN]
        if unknown_forbidden:
            errors.append(f"禁止的动作里有未知项：{'、'.join(unknown_forbidden)}")

        both = set(self.allowed_actions) & set(self.forbidden_actions)
        if both:
            errors.append(f"同一个动作不能既允许又禁止：{'、'.join(sorted(both))}")

        # 路径校验延迟导入 safety，避免 agent.tools.safety -> guard 的循环依赖
        try:
            from agent.tools.safety import expand_path, in_workspace
        except Exception:  # noqa: BLE001 - 导入失败时不阻断表单
            return errors

        for raw in self.input_scope:
            try:
                path = expand_path(raw)
            except OSError:
                errors.append(f"输入范围无法解析：{raw}")
                continue
            if not in_workspace(path):
                errors.append(f"输入范围不在工作区域内：{path}")

        if self.output_location:
            try:
                output = expand_path(self.output_location)
            except OSError:
                errors.append(f"输出位置无法解析：{self.output_location}")
            else:
                if not in_workspace(output):
                    errors.append(f"输出位置不在工作区域内：{output}")

        return errors

    # ------------------------------------------------------------------
    # 给模型看的说明
    # ------------------------------------------------------------------

    def to_prompt(self) -> str:
        criteria_text = "\n".join(f"  - {item}" for item in self.success_criteria) or "  - （未填写）"
        machine = "\n".join(f"  - {item}" for item in self.machine_checks) or "  - （未配置；只能标未确认）"
        allowed = "、".join(self.allowed_actions) or "未限制（但仍受工作区与安全策略约束）"
        forbidden = "、".join(self.forbidden_actions) or "无"
        scope = "、".join(self.input_scope) or "工作区内的只读范围"
        output = self.output_location or "未指定（写操作仍受工作区限制）"
        return (
            "【当前任务卡（必须遵守，越界会被系统直接拒绝）】\n"
            f"任务名：{self.name}\n"
            f"使用者：{self.user or '未填写'}\n"
            f"目标：{self.goal}\n"
            "成功判据（完成前逐条核对）：\n"
            f"{criteria_text}\n"
            "机器判据（任务结束由代码核对，未通过/未确认不能算完成）：\n"
            f"{machine}\n"
            f"输入范围：{scope}\n"
            f"输出位置：{output}\n"
            f"允许的动作：{allowed}\n"
            f"禁止的动作：{forbidden}\n"
            f"预算：步数≤{self.max_steps}，时长≤{self.max_minutes} 分钟，费用≤{self.max_cost_yuan} 元\n"
            f"有效期：本次任务开始后 {self.valid_minutes} 分钟\n"
            f"回滚方式：{self.rollback_note or '批次号会在任务开始时生成，可在界面「批次与回滚」里一键撤销整批'}\n"
            f"回滚边界：{ROLLBACK_BOUNDARY_TEXT}\n"
            f"不确定时：{self.on_uncertain}\n"
            "语义禁令说明：如果禁止动作里包含「发送/提交/付款」，系统会保守拒绝"
            "所有 GUI 输入类动作（点击/键盘/剪贴板写入/应用启停），因为无法判断一次点击"
            "是否会触发提交；不要尝试用命令或打开程序绕过。\n"
            "执行纪律：\n"
            "1. 不要调用任务卡之外的工具类别；被拒绝时不要尝试绕过。\n"
            "2. 每做完一个写操作，用确定性证据核对成功判据；核对不了就说「未确认」。\n"
            "3. 任务卡不能修改；预算或范围不够时停下来告诉用户，不要自行扩大。\n"
            "4. 不确定就标记 UNKNOWN 并停下来问人，不要猜。"
        )

    def summary_text(self) -> str:
        return (
            f"{self.name}｜{self.goal}\n"
            f"预算 {self.max_steps} 步 / {self.max_minutes} 分钟 / {self.max_cost_yuan} 元"
        )

    def is_expired(self, started_at: float) -> bool:
        return (datetime.now().timestamp() - started_at) > self.valid_minutes * 60

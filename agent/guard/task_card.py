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

from agent.guard.config import desktop_task_defaults, task_defaults
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

# 快速卡默认只保留“提权”这一条语义禁令。
# 如果把“发送/提交/付款”也默认写进去，所有 GUI 输入类动作都会被保守拒绝，
# 与“能操作所有软件”的目标冲突；GUI 副作用仍由人工确认 + PIN 兜底。
QUICK_FORBIDDEN = ["提权"]

DEFAULT_SUCCESS_CRITERIA = [
    "按用户要求完成，并以工具实际返回结果和现场证据为准",
]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


# 把“打开微信，登录后给邓三三发个你好”拆成可逐项推进的子目标。
# 只用于提醒模型“这是一连串要求”，不改变任务卡的安全约束。
_SUBGOAL_SEPARATORS = (
    "然后", "接着", "之后", "接下来", "再帮我", "最后",
    "，", "。", "；", ";", ",", "、", "\n",
)

_MAX_SUBGOALS = 8


def split_subgoals(text: str) -> list[str]:
    """从一段目标描述里拆出子目标；拆不出两项以上就返回空列表。

    只做保守拆分：按标点与连接词切，丢掉太短的碎片。拆分失败不影响任务，
    模型仍按原来的整句目标执行。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    pieces = [raw]
    for separator in _SUBGOAL_SEPARATORS:
        if not pieces:
            break
        pieces = [piece for chunk in pieces for piece in chunk.split(separator)]
    items = [piece.strip(" “”\"'(（) ）").strip() for piece in pieces if piece and piece.strip()]
    items = [item for item in items if len(item) >= 2]
    if len(items) < 2:
        return []
    return items[:_MAX_SUBGOALS]


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
    # 可复用技能库给出的步骤参考；只作为提示，不替代任务卡约束和现场核对。
    reference_steps: list[str] = field(default_factory=list)
    # 一连串要求拆出的子目标（仅用于提醒执行顺序，不是新的权限）。
    subgoals: list[str] = field(default_factory=list)

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
            "reference_steps": list(self.reference_steps),
            "subgoals": list(self.subgoals),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "TaskCard | None":
        # 也接受已经是 TaskCard 的对象（热重载后可能不是同一个类，不靠 isinstance）
        to_dict = getattr(data, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
        if not data or not isinstance(data, dict):
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
            reference_steps=_as_list(data.get("reference_steps")),
            subgoals=_as_list(data.get("subgoals")),
        )

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        *,
        reference_steps: list[str] | None = None,
        allowed_actions: list[str] | None = None,
    ) -> "TaskCard":
        """按用户输入生成一张可用的快速任务卡。

        这是界面的默认路径：用户不需要先填一长串表单。目标、预算和动作范围
        先给安全默认值，仍然保留工作区、只读、人工确认和 PIN 这些硬约束。
        """
        text = str(prompt or "").strip()
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        name = first_line[:40] or "快速任务"
        goal = text or "按用户输入完成一次电脑操作"
        allowed = list(ALL_ACTIONS if allowed_actions is None else allowed_actions)
        # 需要动鼠标键盘或启停应用的任务是一步一截图的长任务，
        # 用通用预算（30 步 / 2 元）很容易在还剩几个子目标时被熔断。
        desktop_like = bool({ACTION_DESKTOP, ACTION_OPEN} & set(allowed))
        defaults = desktop_task_defaults() if desktop_like else task_defaults()
        return cls(
            name=name,
            user="",
            goal=goal,
            success_criteria=list(DEFAULT_SUCCESS_CRITERIA),
            machine_checks=[],
            input_scope=[],
            output_location="",
            allowed_actions=allowed,
            forbidden_actions=list(QUICK_FORBIDDEN),
            max_steps=int(defaults["max_steps"]),
            max_minutes=int(defaults["max_minutes"]),
            max_cost_yuan=float(defaults["max_cost_yuan"]),
            valid_minutes=120,
            rollback_note="",
            on_uncertain="标记 UNKNOWN 并停下来问人",
            reference_steps=list(reference_steps or []),
            subgoals=split_subgoals(goal),
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

        # 机器判据是可选的增强项：有就校验格式；没有时任务结束只能标“未确认”。
        # 这样普通任务不必先学判据语法，写入/命令类仍建议填写。
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
        # 手工填的任务卡没经快速卡时不会预先拆分，这里补上，避免两种入口行为不一致。
        items = list(self.subgoals) or split_subgoals(self.goal)
        reference = (
            "\n可复用技能参考（只作步骤提示，执行前仍要按现场核对）：\n"
            + "\n".join(f"  - {item}" for item in self.reference_steps)
            + "\n"
            if self.reference_steps
            else ""
        )
        subgoals = (
            "\n本次是一连串要求，拆成子目标逐项推进（只提醒顺序，不扩大权限）：\n"
            + "\n".join(f"  {index}. {item}" for index, item in enumerate(items, 1))
            + "\n一次只做一个子目标：做完一个用事实确认后再进入下一个；"
              "卡住（需要你登录、输入密码、扫码、付款这类）就停下来交给人，"
              "不要用同一个动作反复硬试。\n"
            if len(items) > 1
            else ""
        )
        return (
            "【当前任务卡（必须遵守，越界会被系统直接拒绝）】\n"
            f"任务名：{self.name}\n"
            f"使用者：{self.user or '未填写'}\n"
            f"目标：{self.goal}\n"
            "成功判据（完成前逐条核对）：\n"
            f"{criteria_text}\n"
            "机器判据（可选；任务结束由代码核对，未配置按人工确认验收）：\n"
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
            f"{reference}"
            f"{subgoals}"
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

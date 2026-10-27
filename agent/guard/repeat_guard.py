"""原地打转检测：同一区域反复做同类动作时先提醒、再拦截。

真实事故：任务是「打开微信，登录后给好友邓三三发个你好」，模型没有在
启动应用上想办法，而是在屏幕左下角 (178~200, 970~985) 连点数了 5 次、
又双击一次，每一步都要重新截图，12 步就把费用顶到上限被熔断。

这类"看不见进展就再点一次"的循环，靠提示词劝不住，需要确定性判据：

- 按动作 + 位置/内容做指纹，滑窗统计邻近重复次数；
- 达到软阈值：不阻断，但在工具结果里追加一句"换策略"的具体建议；
- 达到硬阈值：直接拒绝执行，避免继续烧预算，并把控制权交回给人。

只统计有副作用的鼠标/键盘动作；截图、屏幕尺寸这类观察动作不计入，
否则正常的"截一张看一次"会被误判成打转。
"""

import threading
from typing import Any

_LOCK = threading.RLock()

# 坐标按这个半径视为"同一个地方"（像素）；跨几个相邻点位反复试也算打转。
_RADIUS = 60
# 同一带内的第 3 次开始提醒，第 6 次开始拒绝执行。
_SOFT_REPEATS = 3
_HARD_REPEATS = 6
# 只回看最近这么多次交互动作，避免长任务里早期动作永久占着窗口。
_WINDOW = 12

_TRACKED_ACTIONS = {
    "click",
    "double_click",
    "right_click",
    "drag",
    "scroll",
    "type_text",
    "press_key",
    "hotkey",
    "move_mouse",
    # UI 自动化与 OCR 点击同样可能“反复点同一个东西”
    "ui_click",
    "click_text",
    "ui_set_text",
}

_ADVICE_TEXT = (
    "[重复动作提醒] 你已经在这个位置反复做同一个动作 {count} 次，界面没有可见进展。"
    "不要再点第四次：先想清楚这一步到底要达成什么，换一种做法——"
    "要打开某个软件就改用 computer_use(action=app_open, name=...)，"
    "Windows 上打不开再用 computer_use(action=app_open_start_menu, name=...) 从开始菜单按中英文名启动；"
    "确实需要连按同一个键（如连续回车）时，用 press_key(key=..., presses=N) 一次完成；"
    "要找输入框就先 screenshot 看清焦点位置；确实做不到就停下来告诉用户卡在哪一步，"
    "不要继续用截图消耗预算。"
)

_BLOCK_TEXT = (
    "同一位置已经重复同一个动作 {count} 次，守卫层判定为原地打转，这次调用没有执行。"
    "如果本意就是连按同一个键，请改用 press_key(key=..., presses=N) 一次完成；"
    "否则请停下来向用户说明：目标是什么、已经做到了哪一步、卡住的原因是什么，"
    "并请求用户补充信息或手动完成（例如登录、输入密码、扫码这类必须由人做的事）。"
    "不要换工具绕过这条限制。"
)

_STATE: dict[str, Any] = {"batch_id": "", "history": []}


def reset_for_tests() -> None:
    with _LOCK:
        _STATE["batch_id"] = ""
        _STATE["history"] = []


def _coordinate(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fingerprint(tool_name: str, args: dict) -> tuple | None:
    """把一个交互动作压成可比较的指纹；非交互动作返回 None。"""
    if tool_name != "computer_use":
        return None
    action = str(args.get("action") or "")
    if action not in _TRACKED_ACTIONS:
        return None
    if action in {"type_text", "press_key", "hotkey"}:
        payload = args.get("text") if action == "type_text" else args.get("key", args.get("keys"))
        return (action, str(payload or "").strip().lower()[:200])
    if action in {"ui_click", "ui_set_text", "click_text"}:
        # 控件点击没有坐标可用，按“点的是什么”做指纹
        payload = args.get("name") or args.get("automation_id") or args.get("text")
        return (action, str(payload or "").strip().lower()[:200])
    x = _coordinate(args.get("x"))
    y = _coordinate(args.get("y"))
    if x is None or y is None:
        # drag 用起点，scroll 可能不带坐标
        x = _coordinate(args.get("x1"))
        y = _coordinate(args.get("y1"))
    return (action, x, y)


def _same_place(first: tuple, second: tuple) -> bool:
    """同一动作，且坐标互相在半径内（无坐标的按键类按内容精确比较）。"""
    if first[0] != second[0]:
        return False
    if len(first) == 2:
        return first[1] == second[1]
    if first[1] is None or second[1] is None:
        return True
    return abs(first[1] - second[1]) <= _RADIUS and abs(first[2] - second[2]) <= _RADIUS


def note_action(batch_id: str, tool_name: str, args: dict) -> tuple[int, str, str]:
    """记录一次动作，返回（该处已重复次数, 建议文本, 拦截原因）。

    非交互动作返回 (0, "", "")，不打扰主流程。
    """
    fingerprint = _fingerprint(tool_name, args)
    if fingerprint is None:
        return 0, "", ""
    with _LOCK:
        if _STATE["batch_id"] != (batch_id or ""):
            _STATE["batch_id"] = batch_id or ""
            _STATE["history"] = []
        _STATE["history"].append(fingerprint)
        window = _STATE["history"][-_WINDOW:]
        count = sum(1 for item in window if _same_place(item, fingerprint))
    if count >= _HARD_REPEATS:
        return count, "", _BLOCK_TEXT.format(count=count)
    if count >= _SOFT_REPEATS:
        return count, _ADVICE_TEXT.format(count=count), ""
    return count, "", ""

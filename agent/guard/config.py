"""守卫层配置：只读开关、任务预算默认值、模型价格、截图历史、状态目录。

注意：`config/agent.yml` 是用户可改的普通配置，不是“Agent 不能读”的
秘密文件；审批口令（PIN）的哈希不放在这里，见 `agent/guard/approval.py`。
"""

import os
import threading
from pathlib import Path

import yaml

from utils.path_tool import get_abs_path, get_project_root

_CONFIG_LOCK = threading.RLock()
_CONFIG_PATH = Path(get_abs_path("config/agent.yml"))

# 状态目录默认放在项目根下；测试或部署时可用 AGENT_STATE_DIR 指向别处。
_STATE_DIR_ENV = "AGENT_STATE_DIR"


def read_agent_config() -> dict:
    """读取 agent.yml；文件缺失或格式错误时返回空配置。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as stream:
            return yaml.safe_load(stream) or {}
    except OSError:
        return {}
    except yaml.YAMLError:
        return {}


def _render_yaml_block(key: str, value) -> list[str]:
    """把单个顶层配置项渲染成 YAML 块（含键行）。"""
    dumped = yaml.safe_dump(
        {key: value}, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return [line.rstrip("\n") for line in dumped.splitlines()]


def _replace_top_level_block(lines: list[str], key: str, block: list[str]) -> list[str] | None:
    """替换顶层键的整块内容；键不存在时返回 None（由调用方追加）。"""
    start = None
    for index, line in enumerate(lines):
        if line[:1] in {" ", "\t"}:
            continue
        if line.rstrip() == f"{key}:" or line.startswith(f"{key}:"):
            start = index
            break
    if start is None:
        return None
    # 只扫描“属于这个键”的行：缩进行或列于 0 列的块序列项（`- xxx`）；
    # 碰到空行、顶层注释或下一个顶层键就停，把它们当作下一节的前导原样保留。
    end = start + 1
    while end < len(lines):
        line = lines[end]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            break
        if line[:1] in {" ", "\t"} or stripped.startswith("- ") or stripped == "-":
            end += 1
            continue
        break
    return lines[:start] + block + lines[end:]


def _atomic_write_text(text: str) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _CONFIG_PATH.with_suffix(".yml.tmp")
    with open(temp_path, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temp_path, _CONFIG_PATH)


def update_agent_config_block(key: str, value) -> bool:
    """定点替换 agent.yml 里单个顶层配置项，保留注释与其它配置原样。

    界面保存工作区域/只读开关以前是整份 `yaml.safe_dump` 回写，
    会把注释、空行和无关配置（如 sensitive_dirs）一起重排；
    这里只替换目标键的块，其余行逐行原样保留。返回是否完成了替换；
    文件缺失或键不存在时写入新块（返回 False 供调用方判断）。
    """
    with _CONFIG_LOCK:
        block = _render_yaml_block(key, value)
        try:
            original = _CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            original = ""
        if original:
            newline = "\r\n" if "\r\n" in original else "\n"
            lines = original.replace("\r\n", "\n").split("\n")
            replaced = _replace_top_level_block(lines, key, block)
            if replaced is not None:
                _atomic_write_text(newline.join(replaced).rstrip("\n") + "\n")
                return True
            stripped = "\n".join(line for line in lines if line.strip())
            text = stripped.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
            _atomic_write_text(text.replace("\n", newline))
        else:
            _atomic_write_text("\n".join(block) + "\n")
        return False


def write_agent_config(config: dict) -> None:
    """原子写回 agent.yml，避免写一半把配置写坏。

    注意：这是整份重 dump，会丢掉注释；界面上的单项修改请改用
    `update_agent_config_block`，只有拿到完整配置结构时才用本函数。
    """
    with _CONFIG_LOCK:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = _CONFIG_PATH.with_suffix(".yml.tmp")
        with open(temp_path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, _CONFIG_PATH)


def set_config_value(key: str, value) -> None:
    """修改单个顶层配置项并立即写回；注释和其它配置保持原样。"""
    update_agent_config_block(key, value)


def read_only_enabled() -> bool:
    """只读总开关：环境变量优先，其次 agent.yml 的 read_only。

    环境变量 `AGENT_READ_ONLY=1` 适合临时演练；界面切换会写回配置文件，
    重启后仍然生效。
    """
    env_value = os.getenv("AGENT_READ_ONLY")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(read_agent_config().get("read_only", False))


def set_read_only(enabled: bool) -> None:
    """持久化只读开关。"""
    set_config_value("read_only", bool(enabled))


def task_defaults() -> dict:
    """任务卡未填写预算时的默认上限。"""
    conf = read_agent_config().get("task_defaults") or {}
    try:
        max_steps = int(conf.get("max_steps", 30))
    except (TypeError, ValueError):
        max_steps = 30
    try:
        max_minutes = int(conf.get("max_minutes", 10))
    except (TypeError, ValueError):
        max_minutes = 10
    try:
        max_cost = float(conf.get("max_cost_yuan", 2.0))
    except (TypeError, ValueError):
        max_cost = 2.0
    return {
        "max_steps": max(1, min(max_steps, 200)),
        "max_minutes": max(1, min(max_minutes, 120)),
        "max_cost_yuan": max(0.1, min(max_cost, 100.0)),
    }


def model_prices() -> tuple[float, float]:
    """返回（输入, 输出）每百万 token 的价格，单位：元。

    这里的数字只是上限估算用的配置，不是账单；请按自己的供应商改。
    没有拿到 usage_metadata 时不会乱扣费，只记录调用次数。
    """
    conf = read_agent_config().get("model_prices") or {}
    try:
        input_price = float(conf.get("input_per_million", 2.0))
    except (TypeError, ValueError):
        input_price = 2.0
    try:
        output_price = float(conf.get("output_per_million", 6.0))
    except (TypeError, ValueError):
        output_price = 6.0
    return max(0.0, input_price), max(0.0, output_price)


def desktop_task_defaults() -> dict:
    """桌面/应用类任务的预算默认值（比通用任务宽，但仍受 validate 硬范围限制）。

    一连串键鼠动作是一步一截图的长任务：30 步 / 2 元在现实场景中经常
    不够用，任务会在还剩几个子目标时被熔断。读 `task_defaults.desktop_*`，
    没配置时给 60 步 / 20 分钟 / 5 元；不需要这么高的预算可以在 agent.yml 里调低。
    """
    conf = read_agent_config().get("task_defaults") or {}
    base = task_defaults()

    def _pick(key: str, fallback: int | float) -> int | float:
        try:
            return type(fallback)(conf.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    max_steps = int(_pick("desktop_max_steps", 60))
    max_minutes = int(_pick("desktop_max_minutes", 20))
    max_cost = float(_pick("desktop_max_cost_yuan", 5.0))
    return {
        "max_steps": max(base["max_steps"], max(1, min(max_steps, 200))),
        "max_minutes": max(base["max_minutes"], max(1, min(max_minutes, 120))),
        "max_cost_yuan": max(base["max_cost_yuan"], max(0.1, min(max_cost, 100.0))),
    }


def keep_recent_screenshots() -> int:
    """发给模型时保留最近几张桌面截图；返回 -1 表示不裁剪。

    截图是桌面任务最主要的花费：历史里的图片每次调用都会重发，十几步就能
    把费用顶到上限。只把最近 N 张图发给模型（旧的换成文字占位，需要时重新
    截图即可），状态和审计仍保留原图。默认 1 张；配置为 0 按 1 张处理。
    """
    conf = read_agent_config().get("vision_history") or {}
    try:
        keep = int(conf.get("keep_recent_screenshots", 1))
    except (TypeError, ValueError):
        keep = 1
    if keep <= -1:
        return -1
    return max(1, keep)


def state_dir() -> Path:
    """守卫层运行状态目录（批次、会话快照、锁、急停文件）。

    默认 `<项目根>/.agent_state`；测试用 `AGENT_STATE_DIR` 指到临时目录。
    这个目录对 Agent 工具只允许读、不允许写（见 safety.py），避免模型
    篡改自己的批次记录。
    """
    override = os.getenv(_STATE_DIR_ENV)
    base = Path(override) if override else Path(get_project_root()) / ".agent_state"
    base.mkdir(parents=True, exist_ok=True)
    return base

"""守卫层配置：只读开关、任务预算默认值、模型价格、状态目录。

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


def write_agent_config(config: dict) -> None:
    """原子写回 agent.yml，避免写一半把配置写坏。"""
    with _CONFIG_LOCK:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = _CONFIG_PATH.with_suffix(".yml.tmp")
        with open(temp_path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, _CONFIG_PATH)


def set_config_value(key: str, value) -> None:
    """修改单个配置项并立即写回。"""
    with _CONFIG_LOCK:
        config = read_agent_config()
        config[key] = value
        write_agent_config(config)


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

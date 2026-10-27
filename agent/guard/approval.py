"""第二主体审批口令（P0-4）。

为什么需要“第二主体”：

- 小企业版的主要风险是误操作，不是定向攻击；
- 一个“我确认”按钮挡不住手滑，也挡不住模型自己点掉确认框；
- 所以难逆/不可逆动作（R2/R3）除了点确认，还要输入**人工持有**的
  审批口令（PIN）。PIN 只在本机校验，不进入提示词、不进入对话历史、
  不进入工作区。

安全边界（必须说清楚）：

- 这能挡住“模型顺手点确认”和“模型改写审批结果”；
- 挡不住同一个 Windows 用户下的恶意程序（它可以读进程内存、键盘记录）；
- 因此它是“防误触 + 防自欺”，不是完整的攻击隔离。要挡蓄意攻击，
  需要独立用户/独立主机/带外审批，见企业版文档。

实现：

- PIN 只存 PBKDF2-SHA256 哈希（随机盐 + 20 万次迭代），不存明文；
- 审批通过后生成 HMAC 签名凭证，审计只记录凭证摘要，不记录 PIN；
- 存储目录优先 `%PROGRAMDATA%\\OSGuiAgent\\approval`；不可写时退回
  `%LOCALAPPDATA%`，并由 `is_protected_path()` 让 Agent 工具拒绝读写。
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from utils.logger_handler import logger

_PIN_FILE = "pin.json"
_KEY_FILE = "key.bin"
_LOG_FILE = "approvals.jsonl"
_ITERATIONS = 200_000
_MIN_PIN_LEN = 6
_MAX_PIN_LEN = 64
_LOCK = threading.RLock()

_WEAK_PINS = {
    "123456",
    "1234567",
    "12345678",
    "000000",
    "111111",
    "666666",
    "888888",
    "abcdef",
    "password",
}


def _is_under(path: Path, root: Path) -> bool:
    child = os.path.normcase(os.path.realpath(os.path.abspath(str(path))))
    parent = os.path.normcase(os.path.realpath(os.path.abspath(str(root))))
    return child == parent or child.startswith(parent + os.sep)


def _candidate_dir() -> Path:
    override = os.getenv("AGENT_APPROVAL_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        program_data = os.getenv("PROGRAMDATA") or r"C:\ProgramData"
        return Path(program_data) / "OSGuiAgent" / "approval"
    return Path.home() / ".config" / "os_gui_agent" / "approval"


def approval_dir() -> Path:
    """返回实际使用的审批目录；ProgramData 不可写时退回 LocalAppData。"""
    candidate = _candidate_dir()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        if os.name != "nt" or os.getenv("AGENT_APPROVAL_DIR"):
            raise
        fallback = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "OSGuiAgent" / "approval"
        fallback.mkdir(parents=True, exist_ok=True)
        logger.warning(f"[approval]ProgramData 不可写，审批目录退回：{fallback}")
        return fallback


def is_protected_path(path: str | Path) -> bool:
    """判断路径是否落在审批目录内；Agent 工具必须拒绝读写这里。

    这里不创建目录，避免每次路径检查都触发文件系统写入；同时把
    ProgramData 与 LocalAppData 两个候选目录都视为受保护，覆盖回退场景。
    """
    candidates = [_candidate_dir()]
    if os.name == "nt" and not os.getenv("AGENT_APPROVAL_DIR"):
        local = os.getenv("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "OSGuiAgent" / "approval")
    return any(_is_under(Path(path), candidate) for candidate in candidates)


def _pin_path() -> Path:
    return approval_dir() / _PIN_FILE


def _key_path() -> Path:
    return approval_dir() / _KEY_FILE


def _log_path() -> Path:
    return approval_dir() / _LOG_FILE


def _write_bytes(path: Path, data: bytes) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "wb") as stream:
        stream.write(data)
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def pin_configured() -> bool:
    try:
        with open(_pin_path(), "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return bool(data.get("salt") and data.get("digest"))
    except (OSError, json.JSONDecodeError):
        return False


def _validate_pin(pin: str) -> None:
    text = str(pin)
    if len(text) < _MIN_PIN_LEN:
        raise ValueError(f"审批口令至少 {_MIN_PIN_LEN} 位")
    if len(text) > _MAX_PIN_LEN:
        raise ValueError(f"审批口令最多 {_MAX_PIN_LEN} 位")
    if text.lower() in _WEAK_PINS:
        raise ValueError("这个口令太常见，请换一个更难猜的")
    if len(set(text)) <= 2:
        raise ValueError("口令字符太单一，请换一个更难猜的")


def set_pin(pin: str) -> None:
    """设置或重置审批口令；只保存哈希。会同时确保签名密钥存在。"""
    _validate_pin(pin)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, _ITERATIONS)
    record = {
        "version": 1,
        "algorithm": "pbkdf2_hmac_sha256",
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "digest": digest.hex(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": "只保存哈希，不保存口令本身",
    }
    with _LOCK:
        _write_bytes(
            _pin_path(),
            json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        _ensure_key()
    logger.info("[approval]审批口令已设置（只保存哈希）")


def verify_pin(pin: str) -> bool:
    """校验口令；未设置、格式错误或匹不上都返回 False（fail closed）。"""
    if not pin_configured():
        return False
    try:
        with open(_pin_path(), "r", encoding="utf-8") as stream:
            record = json.load(stream)
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["digest"])
        iterations = int(record.get("iterations") or _ITERATIONS)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _ensure_key() -> bytes:
    path = _key_path()
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    key = secrets.token_bytes(32)
    _write_bytes(path, key)
    return key


def pin_status_text() -> str:
    if pin_configured():
        return "审批口令已设置：R2/R3 动作需要输入口令才能批准。"
    return (
        "审批口令未设置：R2/R3 动作（命令、永久删除、桌面输入等）将无法批准。"
        "请运行 `uv run python scripts/setup_approval_pin.py` 设置。"
    )


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _actions_digest(actions: list[dict]) -> list[dict]:
    items = []
    for action in actions or []:
        name = str(action.get("name", ""))
        args = action.get("args") or {}
        digest = hashlib.sha256(_canonical(args)).hexdigest()
        items.append({"name": name, "args_sha256": digest})
    return items


def sign_approval(thread_id: str, batch_id: str, actions: list[dict]) -> str:
    """为一次人工批准生成 HMAC 凭证，并写入审批日志（不含 PIN）。"""
    payload = {
        "thread_id": thread_id or "",
        "batch_id": batch_id or "",
        "actions": _actions_digest(actions),
        "ts": int(time.time()),
        "nonce": uuid.uuid4().hex,
    }
    key = _ensure_key()
    token = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    record = {
        **payload,
        "token": token,
        "approver": "local-human",
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with _LOCK:
            with open(_log_path(), "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(f"[approval]写入审批日志失败（已忽略）：{exc}")
    logger.info(f"[approval]已生成审批凭证 batch={batch_id} actions={len(actions or [])}")
    return token


def verify_approval_token(
    token: str,
    max_age_seconds: int = 600,
    *,
    thread_id: str = "",
    batch_id: str = "",
    actions: list[dict] | None = None,
) -> bool:
    """校验审批凭证是否由本机签发、是否过期、是否绑定到指定任务/批次/动作。

    只校验 token 存在不够：同一个进程里任何代码都可以传入一个非空字符串。
    执行侧必须同时校验签名、有效期，以及 thread_id / batch_id / actions 摘要。
    """
    if not token:
        return False
    try:
        key = _ensure_key()
        lines = _log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    expected_actions = _actions_digest(actions) if actions is not None else None
    for line in reversed(lines[-500:]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("token") != token:
            continue
        if time.time() - int(record.get("ts") or 0) > max_age_seconds:
            return False
        if thread_id and str(record.get("thread_id") or "") != thread_id:
            return False
        if batch_id and str(record.get("batch_id") or "") != batch_id:
            return False
        if expected_actions is not None and record.get("actions") != expected_actions:
            return False
        payload = {
            key_name: record.get(key_name)
            for key_name in ("thread_id", "batch_id", "actions", "ts", "nonce")
        }
        expected = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, token)
    return False

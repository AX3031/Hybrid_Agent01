"""把 LangGraph 会话状态以 JSON 持久化到本地文件。

实现方式：继承 InMemorySaver，在其 storage / writes / blobs 变更后，
把内存结构原子写入本地 JSON 文件；进程重启时再加载回来。

适用于单机开发与演示；多进程/多实例部署请使用 PostgreSQL checkpointer。
"""
import base64
import json
import os
import threading
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

DEFAULT_STORAGE_PATH = get_abs_path("memorys/chat_history/agent_checkpoints.json")


def _encode(value: Any) -> Any:
    """把 bytes / tuple / 非字符串键 dict 转成可 JSON 序列化的结构。"""
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {"__tuple__": [_encode(item) for item in value]}
    if isinstance(value, dict):
        # writes 的键是 tuple，JSON 只支持字符串键，所以统一转成键值对列表
        return {"__dict__": [[_encode(key), _encode(item)] for key, item in value.items()]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if "__bytes__" in value:
            return base64.b64decode(value["__bytes__"])
        if "__tuple__" in value:
            return tuple(_decode(item) for item in value["__tuple__"])
        if "__dict__" in value:
            return {_decode(key): _decode(item) for key, item in value["__dict__"]}
    return value


class FileCheckpointSaver(InMemorySaver):
    """InMemorySaver 的文件持久化版本，接口与 LangGraph checkpointer 一致。"""

    def __init__(self, file_path: str = DEFAULT_STORAGE_PATH):
        super().__init__()
        self.file_path = file_path
        # Streamlit 多会话可能在不同线程里同时写入，用可重入锁串行化
        self._lock = threading.RLock()
        self._load()

    def put(self, config, checkpoint, metadata, new_versions):
        with self._lock:
            result = super().put(config, checkpoint, metadata, new_versions)
            self._save()
            return result

    def put_writes(self, config, writes, task_id, task_path=""):
        with self._lock:
            super().put_writes(config, writes, task_id, task_path)
            self._save()

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            super().delete_thread(thread_id)
            self._save()

    def _load(self) -> None:
        if not os.path.exists(self.file_path):
            return

        with self._lock:
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(f"[FileCheckpointSaver] 读取 {self.file_path} 失败，将从空记忆开始：{exc}")
                return

            if not isinstance(payload, dict):
                logger.warning(f"[FileCheckpointSaver] {self.file_path} 格式不正确，将从空记忆开始")
                return

            storage = _decode(payload.get("storage", {}))
            for thread_id, namespace_storage in storage.items():
                for checkpoint_ns, checkpoints in namespace_storage.items():
                    self.storage[thread_id][checkpoint_ns].update(checkpoints)

            for outer_key, writes in _decode(payload.get("writes", {})).items():
                self.writes[outer_key].update(writes)

            for blob_key, blob_value in _decode(payload.get("blobs", {})).items():
                self.blobs[blob_key] = blob_value

    def _save(self) -> None:
        with self._lock:
            storage_dir = os.path.dirname(self.file_path) or "."
            os.makedirs(storage_dir, exist_ok=True)

            payload = {
                "version": 1,
                "storage": _encode(self.storage),
                "writes": _encode(self.writes),
                "blobs": _encode(self.blobs),
            }

            # 先写临时文件再替换，避免写一半时进程退出导致 JSON 损坏
            temp_path = f"{self.file_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(temp_path, self.file_path)


_file_checkpointer: FileCheckpointSaver | None = None


def get_file_checkpointer() -> FileCheckpointSaver:
    """进程内复用同一个实例，避免多个会话各自持有旧快照互相覆盖。"""
    global _file_checkpointer
    if _file_checkpointer is None:
        _file_checkpointer = FileCheckpointSaver()
    return _file_checkpointer

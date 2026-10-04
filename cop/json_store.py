"""L1 无状态 JSON 存储层。

load_json / save_json 逐字迁移自 main.py 原第 288-317 行，保留异常兜底与原子写逻辑。
"""

import json
import os
import tempfile
from pathlib import Path

from astrbot.api import logger


class JsonStore:
    """无状态 JSON 存储：容错读 + 原子写。"""

    def load_json(self, path: Path, default):
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载 {path.name} 失败: {e}")
        return default

    def save_json(self, path: Path, data):
        """原子写入 JSON：先写同目录临时文件再 os.replace，避免中途异常写坏原文件。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error(f"保存 {path.name} 失败: {e}")

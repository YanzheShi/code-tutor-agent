"""tests/integration/ 下所有用例自动打 `integration` 标记。

这些是真实端到端集成测试，会真调 LLM / 沙箱（单题出题实测约 3 分钟），
用于日常快速回归时跳过：

    uv run pytest -m "not integration"        # 只跑快的（单元/接口）
    uv run pytest -m integration              # 只跑集成
    uv run pytest                             # 全跑（CI / 完整验证）
"""
from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if path is None:
            continue
        if _HERE in Path(str(path)).resolve().parents:
            item.add_marker(pytest.mark.integration)

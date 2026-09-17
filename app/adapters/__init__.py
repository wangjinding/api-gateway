"""供应商适配器注册表。"""

from __future__ import annotations

from app.adapters.base import BaseAdapter
from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import ProviderConfig


def create_adapter(
    pconfig: ProviderConfig, token: str, timeout: int = 120
) -> BaseAdapter:
    """根据供应商配置创建对应适配器。

    当前三个供应商（Agnes / 火山方舟 / 智谱 GLM）均为 OpenAI 兼容协议，
    统一使用 OpenAICompatAdapter，通过 base_url 和 extra_headers 区分。
    图像/视频端点通过 media 配置覆盖（Agnes 走 /videos + /agnesapi）。
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    headers.update(pconfig.extra_headers)

    return OpenAICompatAdapter(
        name=pconfig.name,
        base_url=pconfig.base_url,
        headers=headers,
        timeout=timeout,
        media=pconfig.media,
    )

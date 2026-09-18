"""供应商适配器注册表。"""

from __future__ import annotations

from app.adapters.base import BaseAdapter
from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import ProviderConfig


def create_adapter(
    pconfig: ProviderConfig, token: str, timeout: int = 120
) -> BaseAdapter:
    """根据供应商配置创建对应适配器。

    当前三个供应商（Agnes / 火山方舟 / OpenCode Go）均为 OpenAI 兼容协议，
    统一使用 OpenAICompatAdapter，通过 base_url 和 extra_headers 区分。
    图像/视频端点通过 media 配置覆盖（Agnes 走 /videos + /agnesapi）。
    OpenCode Go 由 requires_session_header 触发每请求动态注入 x-opencode-session。
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
        requires_session_header=pconfig.requires_session_header,
        # 第二十五轮: 把模型级强制参数（providers.*.models[].params）随适配器带下去。
        #   适配器按 model id 查表，在**组装完 payload 之后**覆盖，客户端无法绕过。
        #   放在适配器而非每个调用点：所有入口（对话 / 流式 / 批量 / 一键测速）自动一致，
        #   不会出现"主链路修了、测速还漏着"的偏差。
        #   配置变更时 main._apply_config 会丢弃整个适配器缓存，约束不会滞留旧值。
        model_params={m.id: dict(m.params) for m in pconfig.models if m.params},
    )

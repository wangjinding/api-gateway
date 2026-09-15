"""模型路由：根据请求中的 model 字段查找对应供应商。"""

from __future__ import annotations

from app.config import GatewayConfig, ModelInfo, ProviderConfig


class ModelNotFoundError(Exception):
    def __init__(self, model_id: str, known: list[str] | None = None):
        self.model_id = model_id          # 只存裸模型名（此前误存整句消息）
        self.known = list(known or [])
        msg = f"未知模型 '{model_id}'"
        if self.known:
            msg += f"。可用模型: {self.known}"
        super().__init__(msg)


class AuthConfigError(Exception):
    def __init__(self, provider_key: str, detail: str):
        self.provider_key = provider_key
        super().__init__(f"供应商 {provider_key} 认证配置错误: {detail}")


def resolve(
    config: GatewayConfig, model_id: str
) -> tuple[str, ProviderConfig, ModelInfo]:
    """解析 model_id → (provider_key, ProviderConfig, ModelInfo)。"""
    found = config.find_provider_for_model(model_id)
    if found is None:
        known = [m["id"] for m in config.all_models()]
        raise ModelNotFoundError(model_id, known)
    return found


def resolve_with_auth(
    config: GatewayConfig, model_id: str
) -> tuple[str, ProviderConfig, ModelInfo, str]:
    """解析 + 校验认证 token，返回 (pkey, pconfig, minfo, token)。"""
    pkey, pconfig, minfo = resolve(config, model_id)
    token = pconfig.auth.resolve_token()
    if not token:
        raise AuthConfigError(
            pkey,
            f"未找到 API Key（env: {pconfig.auth.token_env or '无'}，配置 token: 未设置）"
        )
    return pkey, pconfig, minfo, token

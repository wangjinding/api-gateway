"""启动脚本：python run.py

端口/宿主可被环境变量覆盖：
  GATEWAY_PORT    覆盖端口（默认取配置文件 server.port，即 8808）
  GATEWAY_HOST    覆盖宿主（默认取配置文件 server.host，即 127.0.0.1）
  GATEWAY_CONFIG  指定配置文件路径（默认项目根/data/gateway.yaml）

说明：配置读取与 .env 加载统一由 app.main 模块完成（单一来源），
本脚本只负责把 app 对象喂给 uvicorn，避免重复解析 yaml / 重复加载 .env。
"""

import os

from app.main import CONFIG_PATH, app, config  # 触发 app.main 模块加载（含 .env 与 yaml，单份）

host = os.environ.get("GATEWAY_HOST", config.server.host)
port = int(os.environ.get("GATEWAY_PORT", config.server.port))

if __name__ == "__main__":
    import uvicorn

    gs = int(os.environ.get("GATEWAY_GRACEFUL_SHUTDOWN", config.server.graceful_shutdown))
    print(f"[api-gateway] config={CONFIG_PATH} host={host} port={port} graceful_shutdown={gs}s")
    # 直接传入 app 对象（而非 import string），避免 uvicorn 二次导入 app.main 而再算一次配置。
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=str(config.observability.log_level).lower(),
        timeout_graceful_shutdown=gs,
    )
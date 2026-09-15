# API Gateway 快捷命令
# Windows 若无 make，可手动执行对应命令；或装 https://chocolatey.org/packages/make

.DEFAULT_GOAL := help
CONFIG := data/gateway.yaml
PORT ?= 8808

.PHONY: help install run test lint clean

help:  ## 显示帮助
	@echo "api-gateway 常用命令："
	@echo "  make install  安装依赖"
	@echo "  make run      本地启动（端口 $(PORT)）"
	@echo "  make test     跑单元测试（不依赖真实上游）"
	@echo "  make lint     语法编译检查"
	@echo "  make clean    清理缓存/临时文件"

install:  ## 安装依赖
	pip install -r requirements.txt
	pip install pytest httpx  # 测试依赖（测试用，非运行必需）

run:  ## 本地启动
	@python -c "import os,sys,subprocess; env=dict(os.environ); env['GATEWAY_CONFIG']='$(CONFIG)'; env['GATEWAY_PORT']='$(PORT)'; subprocess.call([sys.executable,'run.py'],env=env)"

test:  ## 跑单元测试
	python tests/test_gateway.py
	# 或: pytest tests/ -q

lint:  ## 语法编译检查
	python -m py_compile app/config.py app/circuit.py app/metrics.py app/main.py app/routing.py app/models.py app/adapters/*.py run.py
	@echo "syntax OK"

clean:  ## 清理
	@python -c "import shutil,glob,os; [shutil.rmtree(os.path.join(d,'__pycache__'),ignore_errors=True) for d in glob.glob('**',recursive=True)]"

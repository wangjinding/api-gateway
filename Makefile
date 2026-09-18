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
	# 第三十轮: 原来这里写「或: pytest tests/ -q」—— 但环境里**没装 pytest**，
	#   照着敲只会报 command not found。这套单测其实是**自带 runner 的独立脚本**
	#   （`python tests/test_gateway.py` 直接跑，pytest 只是可选外壳）：
	#   装了 pytest 时 `pytest tests/ -q` 收集的是同一批 test_* 函数，结果等价。

lint:  ## 语法编译检查
	# 第三十轮: 原来是一个**手写文件清单** —— 新增 app/users.py / history.py / feedback.py /
	#   settings.py / sessions.py / video_owners.py 时没人回来补，等于这几个文件从没被 lint 过。
	#   改成 compileall：整个包都覆盖，以后加文件也不用记得改这里。
	python -m compileall -q app run.py
	@echo "syntax OK"

clean:  ## 清理
	@python -c "import shutil,glob,os; [shutil.rmtree(os.path.join(d,'__pycache__'),ignore_errors=True) for d in glob.glob('**',recursive=True)]"

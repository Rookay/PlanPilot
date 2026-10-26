# 用 `make test` 代替记不住的 unittest discover 长命令。
# Windows 上没有 make 时，等价命令见 README「测试」一节。

PYTHON ?= python
TESTS  := tests

.PHONY: help test test-quiet run install install-lock lock check clean

help:
	@echo "make install      安装依赖（支持范围内的最新版）"
	@echo "make install-lock 安装依赖（锁定版本，与 CI 一致）"
	@echo "make test         跑全量测试（详细输出）"
	@echo "make test-quiet   跑全量测试（只输出结果）"
	@echo "make run          启动服务"
	@echo "make check        语法编译检查 + 测试"

install:
	$(PYTHON) -m pip install -r requirements.txt

install-lock:
	$(PYTHON) -m pip install -r requirements.lock

test:
	$(PYTHON) -m unittest discover -s $(TESTS) -v

test-quiet:
	$(PYTHON) -m unittest discover -s $(TESTS) -q

run:
	$(PYTHON) run.py

check:
	$(PYTHON) -m compileall -q app tests
	$(PYTHON) -m unittest discover -s $(TESTS) -q

lock:
	$(PYTHON) -c "import importlib.metadata as m; \
mods = {'fastapi':'fastapi','uvicorn':'uvicorn','SQLAlchemy':'sqlalchemy', \
'pydantic':'pydantic','jsonschema':'jsonschema','ortools':'ortools'}; \
[print(f'{k}=={m.version(v)}') for k, v in mods.items()]"

clean:
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +

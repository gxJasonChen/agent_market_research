# 本地门禁与常用命令（spec/000-constitution.md §3）
# 所有目标都从仓库根目录执行。

PY ?= .venv/bin/python
UV ?= uv

.DEFAULT_GOAL := help

.PHONY: help install lint format type test test-fast coverage test-system run-api run-worker smoke clean

help: ## 显示可用命令
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## 创建虚拟环境并安装依赖（含 dev）
	$(UV) sync --extra dev

lint: ## 静态检查：ruff check + format --check
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

format: ## 自动格式化
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

type: ## 类型检查：mypy
	$(PY) -m mypy

test: ## 门禁：单元 + 契约 + 集成（离线可跑）
	$(PY) -m pytest tests/unit tests/contract tests/integration

test-fast: ## 迭代用：只跑单元测试，遇错即停
	$(PY) -m pytest tests/unit -x -q

coverage: ## 带覆盖率的完整门禁测试，并按分层校验门槛（NFR-003）
	$(PY) -m pytest tests/unit tests/contract tests/integration \
		--cov=src/amr --cov-report=term-missing --cov-report=xml
	$(PY) scripts/check_coverage.py coverage.xml

test-system: ## 系统测试：需真实 MySQL/Redis，真实网关用 --run-real-llm 开启
	$(PY) -m pytest tests/system -m system

run-api: ## 本地启动 API（前台）
	$(PY) -m uvicorn amr.api.app:app --host 0.0.0.0 --port $${AMR_API_PORT:-8012} --app-dir src

run-worker: ## 本地启动 Celery Worker（前台）
	cd src && ../$(PY) -m celery -A amr.worker.tasks:celery_app worker --loglevel=info --concurrency=2

smoke: ## 对本地已启动的服务做端到端冒烟
	bash scripts/smoke.sh

clean: ## 清理缓存与产物
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

"""系统测试的前置设施（spec/040 §2.4）。

这些用例**真的**拉起 MySQL、Redis、Worker 与 FastAPI 进程，
所以它们跑得慢、有外部依赖，统一打 `system` 标记，不进本地门禁与 CI 的默认路径。

模型供应商一律用 `fake`：真实网关的用例单独放在 `test_e2e_real_llm.py` 并用
`real_llm` 标记，默认跳过（NFR-001）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from amr.config import PROJECT_ROOT, Settings

SYSTEM_API_PORT = 8112

# 说明：本目录下的用例由 tests/conftest.py 按路径自动打 `system` 标记——
# conftest 里的 `pytestmark` 不会作用于测试模块，写在这里是无效的（踩过一次）。


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _require_infra(settings: Settings) -> None:
    if not _port_open(settings.mysql_host, settings.mysql_port):
        pytest.skip(f"MySQL 不可达 {settings.mysql_host}:{settings.mysql_port}")
    if not _port_open(settings.redis_host, settings.redis_port):
        pytest.skip(f"Redis 不可达 {settings.redis_host}:{settings.redis_port}")


@pytest.fixture(scope="session")
def settings() -> Settings:
    """真实的进程配置（读仓库根的 .env）。"""
    loaded = Settings()
    _require_infra(loaded)
    return loaded


@pytest.fixture(scope="session")
def system_env(settings: Settings) -> dict[str, str]:
    """子进程环境：确定性供应商 + 独立的 API 端口。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["AMR_LLM_PROVIDER"] = "fake"
    env["AMR_API_PORT"] = str(SYSTEM_API_PORT)
    env["AMR_LOG_LEVEL"] = "INFO"
    return env


def _spawn(cmd: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("wb")
    return subprocess.Popen(cmd, env=env, stdout=handle, stderr=subprocess.STDOUT, cwd=PROJECT_ROOT)


def _wait_http(url: str, timeout: float = 30.0) -> bool:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - 服务还没起来，继续等
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def celery_worker(system_env: dict[str, str]) -> Iterator[subprocess.Popen[bytes]]:
    """真实 Celery Worker 进程。"""
    from celery import Celery

    loaded = Settings()
    proc = _spawn(
        [
            str(PROJECT_ROOT / ".venv/bin/python"),
            "-m",
            "celery",
            "-A",
            "amr.worker.tasks:celery_app",
            "worker",
            "--loglevel=info",
            "--concurrency=1",
        ],
        system_env,
        PROJECT_ROOT / ".artifacts/system-worker.log",
    )
    try:
        app = Celery(broker=loaded.redis_broker_url)
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("Worker 启动即退出，见 .artifacts/system-worker.log")
            if app.control.ping(timeout=1):
                break
            time.sleep(1)
        else:
            raise RuntimeError("等待 Celery Worker 就绪超时，见 .artifacts/system-worker.log")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@pytest.fixture(scope="session")
def api_server(
    system_env: dict[str, str],
    celery_worker: subprocess.Popen[bytes],
) -> Iterator[str]:
    """真实 FastAPI 进程，返回 base_url。"""
    proc = _spawn(
        [
            str(PROJECT_ROOT / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "amr.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(SYSTEM_API_PORT),
            "--app-dir",
            str(PROJECT_ROOT / "src"),
        ],
        system_env,
        PROJECT_ROOT / ".artifacts/system-api.log",
    )
    base_url = f"http://127.0.0.1:{SYSTEM_API_PORT}"
    try:
        if not _wait_http(f"{base_url}/healthz"):
            raise RuntimeError("等待 API 就绪超时，见 .artifacts/system-api.log")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()

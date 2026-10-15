"""真实模型网关的端到端验证（spec/040 §2.4 TC-S-004）。

**默认跳过**，需要显式开启：

    make test-system -- --run-real-llm
    # 或
    .venv/bin/python -m pytest tests/system/test_e2e_real_llm.py --run-real-llm

为什么默认关：真实调用慢（分钟级）、花钱、结果不确定，把它塞进每次门禁会让
TDD 的红绿循环彻底跑不动。但「真实网关到底通不通」又必须有人验——所以它留在
系统测试层，由人按需触发。
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from amr.config import Settings

pytestmark = [pytest.mark.system, pytest.mark.real_llm]

PAYLOAD = {
    "customer_domain": "openai.com",
    "project_description": "为一家 AI 基础设施公司制定新品发布营销方案",
}


@pytest.fixture(scope="module")
def real_llm_settings() -> Settings:
    loaded = Settings()
    missing = [
        name
        for name, value in (
            ("OPENAI_API_BASE", loaded.openai_api_base),
            ("OPENAI_API_KEY", loaded.openai_api_key),
            ("OPENAI_MODEL_NAME", loaded.openai_model_name),
        )
        if not value
    ]
    if missing:
        pytest.skip(f"缺少真实网关配置：{', '.join(missing)}")
    if loaded.llm_provider == "fake":
        pytest.skip("AMR_LLM_PROVIDER=fake，本次不是真实调用")
    return loaded


@pytest.fixture(scope="module")
def real_service(real_llm_settings: Settings) -> str:
    """用真实供应商起一套服务（API + Worker）。

    与 fake 路径分开起，避免 `system_env` 里 AMR_LLM_PROVIDER=fake 的覆盖生效。
    """
    import subprocess

    from amr.config import PROJECT_ROOT

    port = 8114
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["AMR_LLM_PROVIDER"] = real_llm_settings.llm_provider
    env["AMR_API_PORT"] = str(port)

    logs = PROJECT_ROOT / ".artifacts"
    logs.mkdir(parents=True, exist_ok=True)
    worker_log = (logs / "real-worker.log").open("wb")
    api_log = (logs / "real-api.log").open("wb")

    worker = subprocess.Popen(
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
        env=env,
        cwd=PROJECT_ROOT,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
    )
    api = subprocess.Popen(
        [
            str(PROJECT_ROOT / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "amr.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app-dir",
            str(PROJECT_ROOT / "src"),
        ],
        env=env,
        cwd=PROJECT_ROOT,
        stdout=api_log,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(f"{base_url}/healthz", timeout=2.0).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        else:
            raise RuntimeError("真实供应商模式下 API 未能就绪，见 .artifacts/real-api.log")
        time.sleep(5)  # 给 Worker 一点注册时间
        yield base_url
    finally:
        for proc in (api, worker):
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
        worker_log.close()
        api_log.close()


def test_real_gateway_produces_structured_result(real_service: str) -> None:
    """TC-S-004 真实网关跑通全链路并产出受 Schema 约束的结果（REQ-006 / REQ-008）。

    真实推理要跑几分钟，超时给到 20 分钟。
    """
    submitted = httpx.post(f"{real_service}/api/crew", json=PAYLOAD, timeout=30.0)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    deadline = time.time() + 1200
    final: dict = {}
    while time.time() < deadline:
        response = httpx.get(f"{real_service}/api/crew/{job_id}", timeout=30.0)
        response.raise_for_status()
        final = response.json()
        if final["status"] in {"COMPLETE", "ERROR"}:
            break
        time.sleep(10)

    assert final.get("status") == "COMPLETE", (
        f"作业未成功：{final.get('status')} / {final.get('result')}"
    )
    result = final["result"]
    assert set(result) == {"research_report", "strategy", "campaign_idea", "copy"}
    assert len(result["strategy"]["tactics"]) >= 1
    assert result["copy"]["body"]

"""端到端系统测试：真实 MySQL + Redis + Celery Worker + FastAPI（spec/040 §2.4）。

模型用 `fake` 供应商，所以整条链路是**确定性**的：同样输入必须得到同样结果。
真实网关的验证在 test_e2e_real_llm.py，用 `real_llm` 标记单独开启。

用例编号：TC-S-001 / TC-S-003。
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

from amr.config import PROJECT_ROOT, Settings

PAYLOAD = {
    "customer_domain": "openai.com",
    "project_description": "为一家 AI 基础设施公司制定新品发布营销方案",
}
TERMINAL = {"COMPLETE", "ERROR"}


def _wait_terminal(base_url: str, job_id: str, timeout: float = 240.0) -> dict:
    """轮询到终态。失败时把最后状态与事件一起抛出来，便于排障。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        response = httpx.get(f"{base_url}/api/crew/{job_id}", timeout=10.0)
        response.raise_for_status()
        last = response.json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(2)
    raise AssertionError(f"作业在 {timeout}s 内未到终态，最后状态：{last}")


# ------------------------------------------------------------------ TC-S-001


def test_end_to_end_job_completes_with_structured_result(
    api_server: str, settings: Settings
) -> None:
    """TC-S-001 全链路：提交 → Worker 执行 → 结构化结果落库（REQ-001~006）。

    这里刻意直接查数据库做交叉验证：HTTP 层说 COMPLETE，库里的行也必须是 COMPLETE，
    否则就是「接口好看、数据不对」——源项目那类状态 bug 的典型表象。
    """
    submitted = httpx.post(f"{api_server}/api/crew", json=PAYLOAD, timeout=10.0)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    final = _wait_terminal(api_server, job_id)

    assert final["status"] == "COMPLETE", final.get("result")
    result = final["result"]
    assert set(result) == {"research_report", "strategy", "campaign_idea", "copy"}
    assert result["research_report"]
    assert result["strategy"]["tactics"]
    assert result["copy"]["title"]

    events = [event["data"] for event in final["events"]]
    timestamps = [event["timestamp"] for event in final["events"]]
    assert timestamps == sorted(timestamps), "事件必须按时间正序"
    assert any("开始执行" in event for event in events)
    assert any("完成" in event for event in events)

    # 独立连接交叉验证落库结果
    import mysql.connector

    conn = mysql.connector.connect(**settings.mysql_config)  # type: ignore[arg-type]
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jobs WHERE job_id = %s", (job_id,))
        row = cursor.fetchone()
        assert row is not None, "作业没有落库"
        assert row[0] == "COMPLETE"
    finally:
        conn.close()


def test_end_to_end_unknown_job_returns_404(api_server: str) -> None:
    """TC-S-001 补充：真实服务上的 404 语义（REQ-003）。"""
    response = httpx.get(f"{api_server}/api/crew/definitely-not-a-job", timeout=10.0)
    assert response.status_code == 404
    assert "detail" in response.json()


def test_end_to_end_rejects_invalid_payload(api_server: str) -> None:
    """TC-S-001 补充：真实服务上的入参校验（REQ-010）。"""
    response = httpx.post(
        f"{api_server}/api/crew",
        json={"customer_domain": "ab", "project_description": "太短"},
        timeout=10.0,
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ TC-S-003


@pytest.mark.parametrize("cwd", ["/tmp", "/"])
def test_api_starts_from_any_working_directory(cwd: str) -> None:
    """TC-S-003 从任意工作目录启动都能读到配置（NFR-006 / REQ-009）。

    源项目要求「必须在源码目录下启动」，换个目录就 import 失败——
    根因是用相对路径找 .env 与模块。本系统用绝对路径定位，这里就该过。
    """
    port = 8113
    proc = subprocess.Popen(
        [
            str(PROJECT_ROOT / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "amr.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app-dir",
            str(PROJECT_ROOT / "src"),
        ],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        healthy = False
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                pytest.fail(f"从 {cwd} 启动 API 失败：\n{output}")
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0).status_code == 200:
                    healthy = True
                    break
            except Exception:  # noqa: BLE001 - 还没起来
                pass
            time.sleep(0.5)
        assert healthy, f"从 {cwd} 启动的 API 未就绪"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()

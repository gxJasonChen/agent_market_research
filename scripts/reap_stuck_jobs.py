#!/usr/bin/env python
"""回收卡住的作业（M2 的兜底手段）。

**为什么需要它**：收尾写库失败（数据库那一刻不可用）会让作业永久停在
`RUNNING`。执行器已经对**暂时性**存储故障做有限重试（`services/orchestrator.py::_finalize`），
但重试耗尽之后就没人管了——`PENDING` 侧有「入队失败置 ERROR」的防护，`RUNNING` 侧没有。

**它做什么**：把超过阈值仍处于非终态的作业标成 `ERROR`，说明原因。
默认是 **dry-run**（只打印不修改），要真改必须显式加 `--apply`。

用法：
    python scripts/reap_stuck_jobs.py                    # 看看有哪些
    python scripts/reap_stuck_jobs.py --older-than 60    # 卡超过 60 分钟
    python scripts/reap_stuck_jobs.py --older-than 60 --apply

退出码：0 正常（无论有没有回收）；1 出错。
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amr.adapters.mysql_repository import MySQLJobRepository  # noqa: E402
from amr.adapters.repository_publisher import RepositoryEventPublisher  # noqa: E402
from amr.adapters.system_clock import SystemClock, Uuid4Generator  # noqa: E402
from amr.config import get_settings  # noqa: E402
from amr.domain.errors import AmrError, IllegalTransitionError  # noqa: E402
from amr.domain.status import JobStatus, is_terminal  # noqa: E402
from amr.services.job_service import JobService  # noqa: E402

REAP_REASON = "作业长时间未进入终态，由回收脚本标记为失败（可能原因见服务端日志）"


class _NoEnqueue:
    """回收脚本不需要入队能力；给一个一用就报错的对象，而不是 `None`。"""

    def enqueue_flow(self, *_: object, **__: object) -> None:
        raise RuntimeError("回收脚本不参与入队")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把卡在非终态的作业回收成 ERROR")
    parser.add_argument("--older-than", type=int, default=30, metavar="分钟",
                        help="只处理创建时间早于该阈值的作业（默认 30 分钟）")
    parser.add_argument("--limit", type=int, default=200, help="单次最多扫描多少条（默认 200）")
    parser.add_argument("--apply", action="store_true", help="真的写入；不加则只打印（dry-run）")
    args = parser.parse_args(argv[1:])

    settings = get_settings()
    repository = MySQLJobRepository(settings)
    clock = SystemClock()
    service = JobService(
        repository=repository,
        queue=_NoEnqueue(),
        clock=clock,
        ids=Uuid4Generator(),
        publisher=RepositoryEventPublisher(repository=repository, clock=clock),
    )

    cutoff = clock.now().timestamp() - args.older_than * 60
    candidates = [
        job
        for job in service.list_recent(limit=args.limit)
        if not is_terminal(job.status) and job.created_at.timestamp() < cutoff
    ]

    if not candidates:
        print(f"没有卡住的作业（阈值 {args.older_than} 分钟）")
        return 0

    print(f"发现 {len(candidates)} 个卡住的作业：")
    for job in candidates:
        age = int((clock.now() - job.created_at).total_seconds() // 60)
        print(f"  {job.job_id}  status={job.status}  已存在 {age} 分钟")

    if not args.apply:
        print("\n以上为 dry-run。确认无误后加 --apply 真正回收。")
        return 0

    failed = 0
    for job in candidates:
        try:
            service.finish_error(job.job_id, REAP_REASON)
            failed += 1
        except IllegalTransitionError:
            # 扫描到这里之间它自己跑到终态了——好事，跳过
            print(f"  {job.job_id} 已自行进入终态，跳过")
        except AmrError as exc:
            print(f"  {job.job_id} 回收失败：{exc}", file=sys.stderr)

    print(f"\n已回收 {failed} 个作业，时间 {datetime.now(UTC).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

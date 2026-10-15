#!/usr/bin/env python
"""按分层校验覆盖率门槛（spec/001 NFR-003）。

为什么不用 coverage.py 自带的 `fail_under`：它只能设一个全局阈值，而本项目的
要求是**分层的**——正确性核心（domain / services）要求 95%，适配层允许低一些。
一个全局数字要么把核心放太松，要么把适配层卡太死。

用法：
    python scripts/check_coverage.py coverage.xml

退出码：0 全部达标；1 有分层未达标。
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET  # noqa: S405 —— 输入是本仓库自己生成的 coverage.xml
from dataclasses import dataclass
from pathlib import Path

#: 分层门槛（spec/001 NFR-003）
THRESHOLDS: dict[str, float] = {
    "src/amr/domain": 95.0,
    "src/amr/services": 95.0,
    "src/amr": 85.0,  # 整体
}


@dataclass
class LayerCoverage:
    name: str
    total: int
    covered: int
    threshold: float
    nested: bool  # True 表示这一行是父目录的汇总行

    @property
    def rate(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 100.0

    @property
    def ok(self) -> bool:
        # 没统计到任何文件一律判不达标：那种情况下 100% 是假的，
        # 「门槛形同虚设」比「覆盖率不够」危险得多
        if self.total == 0:
            return False
        return self.rate + 1e-9 >= self.threshold


REPO_ROOT = Path(__file__).resolve().parents[1]


def _line_counts(xml_path: Path) -> dict[str, tuple[int, int]]:
    """返回 {仓库相对路径: (已覆盖行数, 总行数)}。

    Cobertura XML 里的 `filename` 是**相对于 `<source>` 根**的（coverage.py 把
    `src/amr` 当作 source root，于是文件名长成 `domain/status.py`）。
    直接把 filename 当仓库路径去匹配前缀会一条都匹配不上——那会让门槛静默失效，
    所以这里显式解析 `<source>` 再归一化成仓库相对路径。

    只统计带 `hits` 的 `<line>`：未覆盖的行是 `hits="0"`，它同样计入分母。
    """
    tree = ET.parse(xml_path)  # noqa: S314 —— 本地生成的文件

    source_node = tree.find(".//sources/source")
    source_root = Path(source_node.text.strip()) if source_node is not None and source_node.text else None

    counts: dict[str, tuple[int, int]] = {}
    for class_node in tree.iter("class"):
        filename = class_node.get("filename")
        if not filename:
            continue

        path = Path(filename)
        if source_root is not None and not path.is_absolute():
            path = source_root / path
        try:
            key = path.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            # 不在仓库内的文件（例如 site-packages 里的）不参与分层统计
            continue

        covered = total = 0
        for line in class_node.iter("line"):
            total += 1
            if int(line.get("hits", "0")) > 0:
                covered += 1
        prev_covered, prev_total = counts.get(key, (0, 0))
        counts[key] = (prev_covered + covered, prev_total + total)

    return counts


def _aggregate(counts: dict[str, tuple[int, int]], prefix: str) -> tuple[int, int]:
    covered = total = 0
    for filename, (file_covered, file_total) in counts.items():
        if filename == prefix or filename.startswith(f"{prefix}/"):
            covered += file_covered
            total += file_total
    return covered, total


def evaluate(xml_path: Path) -> list[LayerCoverage]:
    counts = _line_counts(xml_path)
    if not counts:
        raise SystemExit(f"coverage.xml 里没有任何仓库内文件：{xml_path}")

    results: list[LayerCoverage] = []
    # 先算最具体的前缀，再算父目录，保证 parent 的阈值用的是 parent 自己的行数
    for name in sorted(THRESHOLDS, key=len, reverse=True):
        covered, total = _aggregate(counts, name)
        results.append(
            LayerCoverage(
                name=name,
                total=total,
                covered=covered,
                threshold=THRESHOLDS[name],
                nested=name == "src/amr",
            )
        )
    return results


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "coverage.xml")
    if not path.exists():
        raise SystemExit(f"找不到覆盖率报告：{path}（先跑 make coverage）")

    results = evaluate(path)
    width = max(len(item.name) for item in results)

    print(f"{'层'.ljust(width)}  {'覆盖率':>8}  {'门槛':>6}  {'行数':>12}  结论")
    print("-" * (width + 40))
    failures = []
    for item in results:
        verdict = "OK" if item.ok else "不达标"
        print(
            f"{item.name.ljust(width)}  {item.rate:7.2f}%  {item.threshold:5.1f}%"
            f"  {item.covered:5d}/{item.total:<5d}  {verdict}"
        )
        if not item.ok:
            failures.append(item)

    for item in results:
        if item.total == 0:
            print(f"警告：{item.name} 没有任何被统计的文件，门槛形同虚设")

    if failures:
        print("\n覆盖率不达标：", ", ".join(item.name for item in failures))
        return 1

    print("\n覆盖率门槛全部达标 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

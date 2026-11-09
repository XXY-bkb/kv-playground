"""导出：把每个场景跑一遍，写成前端可直接读取的 JSON。

用法（在仓库根目录）：
    python -m sim.export
    python -m sim.export --out data/scenarios

导出的 JSON 必须满足两个要求：
1. 确定性：同一份代码、同一个场景，重复导出内容完全一样（git diff 为空）；
2. 自描述：文件里有 schema 版本、场景说明、配置、每一步快照和 summary。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .scenarios import SCENARIO_DEFS, get_scenario
from .simulator import Simulator


SCHEMA = "kv-playground/1"


def build_trace(scenario_id: str) -> dict[str, Any]:
    """跑一个场景，返回 trace 字典。"""
    scenario = get_scenario(scenario_id)
    simulator = Simulator(
        config=scenario["config"],
        requests=scenario["requests"],
        scenario_meta={
            "id": scenario["id"],
            "title": scenario["title"],
            "description": scenario["description"],
            "group": scenario["group"],
        },
    )
    return simulator.run()


def _dump_json(path: Path, payload: dict[str, Any]) -> None:
    """统一的 JSON 写法：中文不转义、key 排序、缩进 2 格、末尾换行。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def export_all(out_dir: str | Path | None = None) -> list[Path]:
    """导出全部场景，返回写出的文件列表。"""
    root = Path(__file__).resolve().parents[1]
    target = Path(out_dir) if out_dir is not None else root / "data" / "scenarios"
    target.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    written: list[Path] = []

    for scenario in SCENARIO_DEFS:
        trace = build_trace(scenario["id"])
        path = target / f"{scenario['id']}.json"
        _dump_json(path, trace)
        written.append(path)

        manifest.append(
            {
                "id": scenario["id"],
                "title": scenario["title"],
                "description": scenario["description"],
                "group": scenario["group"],
                "file": path.name,
                "summary": trace["summary"],
            }
        )

    index_path = target / "index.json"
    _dump_json(
        index_path,
        {
            "schema": SCHEMA,
            "scenarios": manifest,
        },
    )
    written.append(index_path)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="导出 kv-playground 场景 trace")
    parser.add_argument(
        "--out",
        default=None,
        help="输出目录（默认 data/scenarios）",
    )
    args = parser.parse_args(argv)

    written = export_all(args.out)
    print(f"已导出 {len(written)} 个文件：")
    for path in written:
        print(f"  - {path}")


if __name__ == "__main__":
    main()

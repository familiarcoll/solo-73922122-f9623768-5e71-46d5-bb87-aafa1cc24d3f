"""生成虚构的医学影像切片标注 CSV（不包含任何真实患者数据）。

布局：12 名受试者、4 个病种、共 60 个切片样本。
每个受试者只对应一个病种，但样本数各不相同，用于验证
"同受试者全部样本只能归入一个集合" 与精确配额的组合约束。

用法：
    python scripts/generate_fixture.py            # 写入 data/slices_seed.csv
    python scripts/generate_fixture.py out.csv 2>/dev/null
"""
from __future__ import annotations

import csv
import os
import random
import sys

DISEASES = ["glioma", "meningioma", "pituitary", "normal"]

# 受试者 -> (病种, 切片数)，总计 60 个样本
SUBJECT_PLAN = [
    ("sub-001", "glioma", 7),
    ("sub-002", "glioma", 5),
    ("sub-003", "glioma", 4),
    ("sub-004", "meningioma", 8),
    ("sub-005", "meningioma", 4),
    ("sub-006", "meningioma", 3),
    ("sub-007", "pituitary", 6),
    ("sub-008", "pituitary", 4),
    ("sub-009", "pituitary", 2),
    ("sub-010", "normal", 9),
    ("sub-011", "normal", 4),
    ("sub-012", "normal", 4),
]


def generate(seed: int = 20261113) -> list[dict[str, str]]:
    rng = random.Random(seed)
    rows: list[dict[str, str]] = []
    for subject, disease, count in SUBJECT_PLAN:
        for idx in range(1, count + 1):
            rows.append(
                {
                    "sample_id": f"{subject}_slice-{idx:03d}",
                    "subject_id": subject,
                    "disease": disease,
                    # 仅用于让虚构数据更逼真的伪字段，不参与求解
                    "frame": rng.randint(10, 240),
                }
            )
    return rows


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "slices_seed.csv",
    )
    rows = generate()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["sample_id", "subject_id", "disease", "frame"]
        )
        writer.writeheader()
        writer.writerows(rows)
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["disease"]] = totals.get(r["disease"], 0) + 1
    print(f"wrote {len(rows)} rows / {len(SUBJECT_PLAN)} subjects -> {out}", file=sys.stderr)
    print(f"disease totals: {totals}", file=sys.stderr)


if __name__ == "__main__":
    main()

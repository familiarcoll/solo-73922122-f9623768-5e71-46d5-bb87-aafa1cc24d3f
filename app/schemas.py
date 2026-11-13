"""请求/响应模型与配额校验。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config import GROUPS

GroupName = Literal["train", "validation", "test"]


class SampleOut(BaseModel):
    sample_id: str
    subject_id: str
    disease: str


class SamplePatch(BaseModel):
    disease: str | None = Field(default=None, min_length=1, max_length=64)
    subject_id: str | None = Field(default=None, min_length=1, max_length=64)


class FreezeRequest(BaseModel):
    group: GroupName


class GroupQuota(BaseModel):
    train: int = Field(ge=0)
    validation: int = Field(ge=0)
    test: int = Field(ge=0)


class SolveRequest(BaseModel):
    # key 为病种名称，value 为该病种在三个集合的精确样本数
    quota: dict[str, GroupQuota]
    # 求解墙钟时间预算（秒），默认 30s
    time_limit_seconds: float = Field(default=30.0, gt=0, le=600)
    # 演示用：即使立刻找到可行解，也至少持续搜索该时长，便于观察暂停/继续
    min_hold_seconds: float = Field(default=0.0, ge=0, le=600)


def validate_quota(
    quota_req: dict[str, GroupQuota],
    disease_totals: dict[str, int],
) -> dict[str, dict[str, int]]:
    """校验配额请求并转为普通 dict。

    规则：
    - 数据中的每个病种都必须给出配额；
    - 不允许请求数据中不存在的病种；
    - 每个病种三组配额之和必须恰好等于该病种的现有样本总数
      （全部样本都要被分配，且数量必须精确满足）。
    """
    from .errors import ServiceError

    if not quota_req:
        raise ServiceError(422, "quota 不能为空")
    asked = set(quota_req.keys())
    known = set(disease_totals.keys())
    unknown = asked - known
    if unknown:
        raise ServiceError(422, f"配额包含数据中不存在的病种: {sorted(unknown)}")
    missing = known - asked
    if missing:
        raise ServiceError(422, f"以下病种缺少配额: {sorted(missing)}")

    normalized: dict[str, dict[str, int]] = {}
    for disease, q in quota_req.items():
        parts = {g: getattr(q, g) for g in GROUPS}
        total = sum(parts.values())
        if total != disease_totals[disease]:
            raise ServiceError(
                422,
                f"病种 {disease} 的配额之和 {total} 不等于现有样本数 "
                f"{disease_totals[disease]}（train/validation/test 必须精确瓜分全部样本）",
            )
        normalized[disease] = parts
    return normalized

"""Token 用量统计模型。

按 (模型, 用户, API Key, 日期) 维度聚合，支持每模型计量。
流式响应在末尾 chunk 通常含 usage 字段；非流式直接读取 response.usage。
"""

from __future__ import annotations

from datetime import date

from sqlmodel import Field, Index

from cpustack.schemas.common import ActiveRecordMixin, TimestampMixin


class TokenUsage(TimestampMixin, ActiveRecordMixin, table=True):
    """Token 用量聚合表（按天 + 模型 + 用户 + API Key 维度）。

    设计为 upsert 行：同一 (model_name, user_id, api_key_id, usage_date)
    仅保留一行，每次推理请求累加 tokens 与请求次数。
    """

    __tablename__ = "token_usage"

    id: int | None = Field(default=None, primary_key=True)

    # 维度
    model_name: str = Field(nullable=False, index=True, max_length=128)
    user_id: int | None = Field(default=None, foreign_key="users.id", nullable=True, index=True)
    api_key_id: int | None = Field(
        default=None, foreign_key="api_keys.id", nullable=True, index=True
    )
    usage_date: date = Field(nullable=False, index=True)

    # 计量
    prompt_tokens: int = Field(default=0, nullable=False)
    completion_tokens: int = Field(default=0, nullable=False)
    total_tokens: int = Field(default=0, nullable=False)
    request_count: int = Field(default=0, nullable=False)

    __table_args__ = (
        Index(
            "ix_token_usage_unique",
            "model_name",
            "user_id",
            "api_key_id",
            "usage_date",
            unique=True,
        ),
    )


class ModelUsageSummary(TimestampMixin, ActiveRecordMixin, table=True):
    """模型用量月度汇总（每模型每月一行，便于快速查询）。

    由周期任务从 TokenUsage 聚合，避免实时聚合全表扫描。
    """

    __tablename__ = "model_usage_summary"

    id: int | None = Field(default=None, primary_key=True)
    model_name: str = Field(nullable=False, index=True, max_length=128)
    year_month: str = Field(nullable=False, index=True, max_length=7)  # "YYYY-MM"

    prompt_tokens: int = Field(default=0, nullable=False)
    completion_tokens: int = Field(default=0, nullable=False)
    total_tokens: int = Field(default=0, nullable=False)
    request_count: int = Field(default=0, nullable=False)

    __table_args__ = (
        Index(
            "ix_model_usage_summary_unique",
            "model_name",
            "year_month",
            unique=True,
        ),
    )

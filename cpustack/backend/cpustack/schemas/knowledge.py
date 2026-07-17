"""本地知识库模型：知识库 + 文档 + 分段。

支持文档上传、文本切分、关键词检索（BM25 风格打分）。
检索结果可作为对话上下文注入（RAG），由网关层在调用推理后端前拼接。
"""

from __future__ import annotations

from enum import Enum

from sqlmodel import Field

from cpustack.schemas.common import ActiveRecordMixin, TimestampMixin


class KnowledgeBaseState(str, Enum):
    """知识库状态。"""

    ACTIVE = "active"  # 可用
    INDEXING = "indexing"  # 索引中
    ERROR = "error"  # 异常


class KnowledgeDocState(str, Enum):
    """知识库文档状态。"""

    PENDING = "pending"  # 待处理
    PROCESSING = "processing"  # 切分中
    READY = "ready"  # 就绪
    ERROR = "error"  # 失败


class KnowledgeBase(TimestampMixin, ActiveRecordMixin, table=True):
    """知识库。"""

    __tablename__ = "knowledge_bases"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True, nullable=False, max_length=128)
    description: str = Field(default="", nullable=False, max_length=2048)

    # 切分参数
    chunk_size: int = Field(default=512, nullable=False)  # 字符数
    chunk_overlap: int = Field(default=64, nullable=False)

    # 统计
    doc_count: int = Field(default=0, nullable=False)
    chunk_count: int = Field(default=0, nullable=False)

    state: KnowledgeBaseState = Field(
        default=KnowledgeBaseState.ACTIVE, nullable=False, index=True
    )
    error_message: str = Field(default="", nullable=False, max_length=2048)

    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)


class KnowledgeDocument(TimestampMixin, ActiveRecordMixin, table=True):
    """知识库文档。"""

    __tablename__ = "knowledge_documents"

    id: int | None = Field(default=None, primary_key=True)
    kb_id: int = Field(foreign_key="knowledge_bases.id", nullable=False, index=True)

    filename: str = Field(nullable=False, max_length=256)
    file_path: str = Field(default="", nullable=False, max_length=512)
    file_size: int = Field(default=0, nullable=False)  # 字节
    mime_type: str = Field(default="", nullable=False, max_length=128)

    # 切分统计
    chunk_count: int = Field(default=0, nullable=False)
    char_count: int = Field(default=0, nullable=False)

    state: KnowledgeDocState = Field(
        default=KnowledgeDocState.PENDING, nullable=False, index=True
    )
    error_message: str = Field(default="", nullable=False, max_length=2048)


class KnowledgeChunk(TimestampMixin, table=True):
    """知识库分段（用于检索的最小单元）。

    纯文本检索（无向量化），通过 BM25 风格打分排序。
    """

    __tablename__ = "knowledge_chunks"

    id: int | None = Field(default=None, primary_key=True)
    kb_id: int = Field(foreign_key="knowledge_bases.id", nullable=False, index=True)
    doc_id: int = Field(foreign_key="knowledge_documents.id", nullable=False, index=True)

    chunk_index: int = Field(default=0, nullable=False)  # 文档内序号
    content: str = Field(nullable=False)  # 分段文本

    # 检索辅助字段
    token_count: int = Field(default=0, nullable=False)  # 近似 token 数（按字符 / 2 估算）

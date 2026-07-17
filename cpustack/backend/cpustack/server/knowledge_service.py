"""本地知识库服务：文档切分 + BM25 关键词检索。

设计取舍：
- 不依赖外部 embedding 模型，纯 Python 实现，开箱即用
- 中文采用字符级 bigram + 词频；英文按空白分词
- BM25 打分排序，返回 top-k 相关分段
- 文档切分按字符数滑窗（默认 512 字符，重叠 64）

未来可扩展：接入运行中的推理后端 embedding 端点做向量检索。
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Any

from sqlmodel import select

from cpustack.config import settings
from cpustack.db import session_scope
from cpustack.schemas.knowledge import (
    KnowledgeBase,
    KnowledgeDocState,
    KnowledgeDocument,
    KnowledgeChunk,
)

logger = logging.getLogger(__name__)


# ---------- 文档读取 ----------

def _read_text_file(file_path: str, mime_type: str = "") -> str:
    """读取文本类文件内容（txt/md/json/csv）。"""
    p = Path(file_path)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        logger.exception("读取文件失败: %s", file_path)
        return ""


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """按字符滑窗切分文本。"""
    if not text:
        return []
    if chunk_size <= 0:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap if overlap > 0 else end
    return chunks


# ---------- 分词 ----------

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """分词：英文按单词，中文按字符 + bigram。"""
    if not text:
        return []
    tokens: list[str] = []
    chars: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        tok = m.group(0)
        if _CJK_RE.match(tok):
            chars.append(tok)
        else:
            if chars:
                _add_bigrams(chars, tokens)
                chars = []
            tokens.append(tok)
    if chars:
        _add_bigrams(chars, tokens)
    return tokens


def _add_bigrams(chars: list[str], tokens: list[str]) -> None:
    """中文 bigram：相邻两字组合。"""
    for i, c in enumerate(chars):
        tokens.append(c)  # 单字也保留
        if i + 1 < len(chars):
            tokens.append(c + chars[i + 1])


# ---------- 文档处理 ----------

async def process_document(doc_id: int) -> None:
    """处理知识库文档：读取 → 切分 → 写入 chunks → 更新统计。

    在后台任务中调用。
    """
    try:
        async with session_scope() as session:
            doc = await session.get(KnowledgeDocument, doc_id)
            if not doc:
                return

            kb = await session.get(KnowledgeBase, doc.kb_id)
            if not kb:
                return

            doc.state = KnowledgeDocState.PROCESSING
            session.add(doc)
            await session.commit()

            content = _read_text_file(doc.file_path, doc.mime_type)
            if not content:
                doc.state = KnowledgeDocState.ERROR
                doc.error_message = "文件内容为空或读取失败"
                session.add(doc)
                await session.commit()
                return

            chunks = _chunk_text(content, kb.chunk_size, kb.chunk_overlap)

            # 删除旧 chunks（若有）
            old = (
                await session.execute(
                    select(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id)
                )
            ).scalars().all()
            for c in old:
                await session.delete(c)

            for i, chunk_text in enumerate(chunks):
                # 估算 token 数（字符 / 2）
                tc = max(1, len(chunk_text) // 2)
                chunk = KnowledgeChunk(
                    kb_id=kb.id,
                    doc_id=doc.id,
                    chunk_index=i,
                    content=chunk_text,
                    token_count=tc,
                )
                session.add(chunk)

            doc.chunk_count = len(chunks)
            doc.char_count = len(content)
            doc.state = KnowledgeDocState.READY
            doc.error_message = ""
            session.add(doc)

            # 更新知识库统计
            kb.doc_count = (
                len(
                    (
                        await session.execute(
                            select(KnowledgeDocument).where(
                                KnowledgeDocument.kb_id == kb.id,
                                KnowledgeDocument.state == KnowledgeDocState.READY,
                            )
                        )
                    ).scalars().all()
                )
            )
            total_chunks = (
                await session.execute(
                    select(KnowledgeChunk).where(KnowledgeChunk.kb_id == kb.id)
                )
            ).scalars().all()
            kb.chunk_count = len(total_chunks)
            session.add(kb)

            await session.commit()
            logger.info(
                "知识库文档处理完成: doc=%s, chunks=%d, chars=%d",
                doc.filename,
                len(chunks),
                len(content),
            )
    except Exception:
        logger.exception("处理知识库文档失败: doc_id=%d", doc_id)
        try:
            async with session_scope() as session:
                doc = await session.get(KnowledgeDocument, doc_id)
                if doc:
                    doc.state = KnowledgeDocState.ERROR
                    doc.error_message = "处理异常"
                    session.add(doc)
                    await session.commit()
        except Exception:
            pass


# ---------- BM25 检索 ----------

class _BM25Index:
    """简易内存 BM25 索引（每次检索时构建，适合中小规模知识库）。"""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs
        self.doc_len = [len(d) for d in docs]
        self.avg_len = sum(self.doc_len) / len(docs) if docs else 0
        self.df: dict[str, int] = {}
        self.tf: list[dict[str, int]] = []
        for d in docs:
            freq: dict[str, int] = {}
            for t in d:
                freq[t] = freq.get(t, 0) + 1
            self.tf.append(freq)
            for t in freq:
                self.df[t] = self.df.get(t, 0) + 1
        self.N = len(docs)
        self.idf = {
            t: math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for t, df in self.df.items()
        }

    def score(self, query_tokens: list[str]) -> list[float]:
        scores = [0.0] * self.N
        for qt in query_tokens:
            idf = self.idf.get(qt, 0.0)
            if idf == 0:
                continue
            for i, freq in enumerate(self.tf):
                f = freq.get(qt, 0)
                if f == 0:
                    continue
                dl = self.doc_len[i] or 1
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                scores[i] += idf * (f * (self.k1 + 1)) / denom
        return scores


async def search_knowledge(
    kb_id: int,
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """在指定知识库中检索相关分段。

    Args:
        kb_id: 知识库 ID
        query: 查询文本
        top_k: 返回前 K 条

    Returns:
        [{"chunk_id", "doc_id", "filename", "content", "score", "chunk_index"}]
    """
    if not query.strip():
        return []

    try:
        async with session_scope() as session:
            kb = await session.get(KnowledgeBase, kb_id)
            if not kb:
                return []

            # 拉取所有 chunk
            chunks = (
                await session.execute(
                    select(KnowledgeChunk, KnowledgeDocument)
                    .where(
                        KnowledgeChunk.kb_id == kb_id,
                        KnowledgeChunk.doc_id == KnowledgeDocument.id,
                    )
                )
            ).all()

            if not chunks:
                return []

            # 构建 BM25 索引
            doc_tokens: list[list[str]] = []
            for chunk, _doc in chunks:
                doc_tokens.append(tokenize(chunk.content))

            index = _BM25Index(doc_tokens)
            query_tokens = tokenize(query)
            scores = index.score(query_tokens)

            # 排序取 top_k
            ranked = sorted(
                enumerate(scores), key=lambda x: x[1], reverse=True
            )[:top_k]

            result: list[dict[str, Any]] = []
            for idx, score in ranked:
                if score <= 0:
                    continue
                chunk, doc = chunks[idx]
                result.append(
                    {
                        "chunk_id": chunk.id,
                        "doc_id": doc.id,
                        "filename": doc.filename,
                        "content": chunk.content,
                        "score": round(score, 4),
                        "chunk_index": chunk.chunk_index,
                    }
                )
            return result
    except Exception:
        logger.exception("知识库检索失败: kb_id=%d", kb_id)
        return []


async def build_rag_context(
    kb_ids: list[int], query: str, top_k_per_kb: int = 3
) -> str:
    """为对话构建 RAG 上下文（拼接多个知识库的检索结果）。"""
    if not kb_ids or not query.strip():
        return ""
    parts: list[str] = []
    for kb_id in kb_ids:
        results = await search_knowledge(kb_id, query, top_k=top_k_per_kb)
        for r in results:
            parts.append(f"[{r['filename']}#{r['chunk_index']}]\n{r['content']}")
    if not parts:
        return ""
    return "\n\n".join(parts)

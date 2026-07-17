"""本地知识库路由：CRUD + 文档上传 + 检索。"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import select

from cpustack.config import settings
from cpustack.db import get_session
from cpustack.schemas.knowledge import (
    KnowledgeBase,
    KnowledgeBaseState,
    KnowledgeDocState,
    KnowledgeDocument,
)
from cpustack.schemas.users import User
from cpustack.server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

# 允许的文档类型
ALLOWED_TEXT_EXTS = {".txt", ".md", ".markdown", ".json", ".csv", ".log", ".py", ".js", ".ts", ".yaml", ".yml", ".html", ".xml"}


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: str = ""
    chunk_size: int = 512
    chunk_overlap: int = 64


class KnowledgeBaseResponse(BaseModel):
    id: int
    name: str
    description: str
    chunk_size: int
    chunk_overlap: int
    doc_count: int
    chunk_count: int
    state: str
    error_message: str


class KnowledgeDocumentResponse(BaseModel):
    id: int
    kb_id: int
    filename: str
    file_size: int
    mime_type: str
    chunk_count: int
    char_count: int
    state: str
    error_message: str


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5


class SearchResult(BaseModel):
    chunk_id: int
    doc_id: int
    filename: str
    content: str
    score: float
    chunk_index: int


def _to_kb_response(kb: KnowledgeBase) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
        state=kb.state.value,
        error_message=kb.error_message,
    )


def _to_doc_response(doc: KnowledgeDocument) -> KnowledgeDocumentResponse:
    return KnowledgeDocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        filename=doc.filename,
        file_size=doc.file_size,
        mime_type=doc.mime_type,
        chunk_count=doc.chunk_count,
        char_count=doc.char_count,
        state=doc.state.value,
        error_message=doc.error_message,
    )


def _kb_storage_path(kb_id: int) -> Path:
    """知识库文档存储目录。"""
    p = settings.knowledge_base_path / str(kb_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------- 知识库 CRUD ----------

@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """列出知识库。"""
    stmt = select(KnowledgeBase).where(KnowledgeBase.user_id == user.id)
    kbs = (await session.execute(stmt)).scalars().all()
    return [_to_kb_response(k) for k in kbs]


@router.post("", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    req: KnowledgeBaseCreate,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """创建知识库。"""
    kb = KnowledgeBase(
        name=req.name,
        description=req.description,
        chunk_size=req.chunk_size,
        chunk_overlap=req.chunk_overlap,
        user_id=user.id,
        state=KnowledgeBaseState.ACTIVE,
    )
    session.add(kb)
    await session.commit()
    await session.refresh(kb)
    return _to_kb_response(kb)


@router.delete("/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """删除知识库及其所有文档和分段。"""
    kb = await session.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 删除文档记录
    docs = (
        await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id)
        )
    ).scalars().all()
    for d in docs:
        await session.delete(d)
    await session.delete(kb)
    await session.commit()

    # 删除文件
    try:
        storage = settings.knowledge_base_path / str(kb_id)
        if storage.exists():
            shutil.rmtree(storage, ignore_errors=True)
    except Exception:
        logger.debug("删除知识库存储目录失败: kb_id=%d", kb_id, exc_info=True)

    return {"message": "知识库已删除"}


# ---------- 文档管理 ----------

@router.get("/{kb_id}/documents", response_model=list[KnowledgeDocumentResponse])
async def list_documents(
    kb_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """列出知识库下的文档。"""
    kb = await session.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    docs = (
        await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id)
        )
    ).scalars().all()
    return [_to_doc_response(d) for d in docs]


@router.post("/{kb_id}/documents", response_model=KnowledgeDocumentResponse)
async def upload_document(
    kb_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """上传文档到知识库（自动切分索引）。

    支持 txt/md/json/csv 等文本类文件。
    """
    kb = await session.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名缺失")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_TEXT_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}，仅支持文本类（txt/md/json/csv 等）",
        )

    # 保存文件
    storage = _kb_storage_path(kb_id)
    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    file_path = storage / safe_name
    content = await file.read()
    file_path.write_bytes(content)

    # 创建文档记录
    doc = KnowledgeDocument(
        kb_id=kb_id,
        filename=safe_name,
        file_path=str(file_path),
        file_size=len(content),
        mime_type=file.content_type or "",
        state=KnowledgeDocState.PENDING,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    # 后台处理（切分索引）
    from cpustack.server.knowledge_service import process_document
    asyncio.create_task(process_document(doc.id))

    return _to_doc_response(doc)


@router.delete("/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: int,
    doc_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """删除文档及其分段。"""
    kb = await session.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    doc = await session.get(KnowledgeDocument, doc_id)
    if not doc or doc.kb_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 删除 chunks
    from cpustack.schemas.knowledge import KnowledgeChunk
    chunks = (
        await session.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id)
        )
    ).scalars().all()
    for c in chunks:
        await session.delete(c)

    file_path = doc.file_path
    await session.delete(doc)
    await session.commit()

    # 删除文件
    try:
        if file_path:
            p = Path(file_path)
            if p.exists():
                p.unlink()
    except Exception:
        pass

    return {"message": "文档已删除"}


# ---------- 检索 ----------

@router.post("/{kb_id}/search", response_model=list[SearchResult])
async def search(
    kb_id: int,
    req: SearchRequest,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """在知识库中检索相关分段（BM25 关键词检索）。"""
    kb = await session.get(KnowledgeBase, kb_id)
    if not kb or kb.user_id != user.id:
        raise HTTPException(status_code=404, detail="知识库不存在")

    from cpustack.server.knowledge_service import search_knowledge
    results = await search_knowledge(kb_id, req.query, top_k=req.top_k)
    return [SearchResult(**r) for r in results]

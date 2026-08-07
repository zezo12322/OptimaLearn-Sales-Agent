"""Sales knowledge base: chunk, embed, retrieve.

Separate from the transcript pipeline on purpose. Transcript chunks are bounded
by timestamps and scoped to a video; sales content is prose bounded by headings
and scoped to an audience (everyone / companies / individuals). Sharing one
table would mean every retrieval carried a filter that only makes sense for the
other domain.

Grounding here is what stops the agent inventing prices. If retrieval comes back
empty the agent is instructed to say so and offer a human, never to improvise.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.chunking import count_tokens
from app.ai.embeddings import embed_texts
from app.core.config import settings
from app.models.sales import SalesChunk, SalesChunkEmbedding, SalesDocument
from app.sales.enums import Audience, DocType

logger = logging.getLogger(__name__)

TARGET_CHUNK_TOKENS = 320
MAX_CHUNK_TOKENS = 420
#: Paragraphs shorter than this get merged forward rather than becoming their
#: own chunk — a lone heading retrieves badly.
MIN_CHUNK_TOKENS = 40


@dataclass
class TextChunk:
    index: int
    text: str
    token_count: int

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass
class RetrievedSalesChunk:
    chunk_id: str
    document_id: str
    document_title: str
    doc_type: str
    audience: str
    locale: str
    text: str
    similarity: float


def content_hash(content: str) -> str:
    """Stable hash used to de-duplicate documents within a tenant."""
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def chunk_text(content: str) -> list[TextChunk]:
    """Split prose into token-bounded chunks along paragraph boundaries.

    Paragraphs are kept whole whenever they fit, because a pricing table or an
    objection/answer pair loses its meaning when cut in half. A paragraph that
    is too big on its own is split by sentence, and only then by token count.
    """
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    if not paragraphs:
        stripped = content.strip()
        if not stripped:
            return []
        paragraphs = [stripped]

    units: list[str] = []
    for para in paragraphs:
        if count_tokens(para) <= MAX_CHUNK_TOKENS:
            units.append(para)
            continue
        units.extend(_split_oversized(para))

    chunks: list[TextChunk] = []
    buffer: list[str] = []
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        if not buffer:
            return
        body = "\n\n".join(buffer).strip()
        if body:
            chunks.append(
                TextChunk(index=len(chunks), text=body, token_count=buffer_tokens)
            )
        buffer = []
        buffer_tokens = 0

    for unit in units:
        unit_tokens = count_tokens(unit)
        if buffer and buffer_tokens + unit_tokens > MAX_CHUNK_TOKENS:
            flush()
        buffer.append(unit)
        buffer_tokens += unit_tokens
        if buffer_tokens >= TARGET_CHUNK_TOKENS:
            flush()

    flush()

    # A trailing scrap (a closing line, a single heading) reads better appended
    # to its predecessor than standing alone in the retrieval set.
    if len(chunks) > 1 and chunks[-1].token_count < MIN_CHUNK_TOKENS:
        tail = chunks.pop()
        prev = chunks[-1]
        merged = f"{prev.text}\n\n{tail.text}"
        chunks[-1] = TextChunk(
            index=prev.index, text=merged, token_count=count_tokens(merged)
        )

    return chunks


def _split_oversized(paragraph: str) -> list[str]:
    """Break a too-large paragraph on sentence ends, then hard-split if needed."""
    pieces: list[str] = []
    sentence: list[str] = []
    for token in paragraph.replace("\n", " ").split(" "):
        sentence.append(token)
        if token.endswith((".", "!", "?", "؟", "۔", ":")):
            pieces.append(" ".join(sentence).strip())
            sentence = []
    if sentence:
        pieces.append(" ".join(sentence).strip())

    out: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if count_tokens(piece) <= MAX_CHUNK_TOKENS:
            out.append(piece)
            continue
        # No sentence boundary to lean on: split on whitespace by token budget.
        words = piece.split(" ")
        current: list[str] = []
        for word in words:
            current.append(word)
            if count_tokens(" ".join(current)) >= MAX_CHUNK_TOKENS:
                out.append(" ".join(current))
                current = []
        if current:
            out.append(" ".join(current))
    return [p for p in out if p]


async def retrieve_sales_chunks(
    db: AsyncSession,
    query_vector: list[float],
    tenant_id: str,
    audience: Audience = Audience.ALL,
    top_k: int = 6,
    min_similarity: float = 0.20,
    doc_types: Optional[list[str]] = None,
) -> list[RetrievedSalesChunk]:
    """Cosine-similarity search over active documents for one tenant.

    ``audience`` widens rather than narrows: a B2B prospect should see both the
    company material and the shared material, never only one of them. Locale is
    intentionally not filtered — an English case study still answers an Arabic
    question, and the model is told which language each passage is in.
    """
    audiences = [Audience.ALL.value]
    if audience is not Audience.ALL:
        audiences.append(audience.value)

    sql = """
        SELECT
            sc.id::text          AS chunk_id,
            sc.document_id::text AS document_id,
            sd.title             AS document_title,
            sce.doc_type         AS doc_type,
            sce.audience         AS audience,
            sce.locale           AS locale,
            sc.text              AS text,
            1 - (sce.embedding <=> CAST(:query_vec AS vector)) AS similarity
        FROM sales_chunk_embeddings sce
        JOIN sales_chunks sc ON sc.id = sce.chunk_id
        JOIN sales_documents sd ON sd.id = sce.document_id
        WHERE
            sce.tenant_id = CAST(:tenant_id AS uuid)
            AND sce.audience = ANY(:audiences)
            AND sd.is_active = true
            AND 1 - (sce.embedding <=> CAST(:query_vec AS vector)) >= :min_similarity
    """
    params: dict[str, object] = {
        "query_vec": str(query_vector),
        "tenant_id": tenant_id,
        "audiences": audiences,
        "min_similarity": min_similarity,
        "top_k": top_k,
    }
    if doc_types:
        sql += " AND sce.doc_type = ANY(:doc_types)"
        params["doc_types"] = doc_types
    sql += " ORDER BY similarity DESC LIMIT :top_k"

    result = await db.execute(text(sql), params)
    return [
        RetrievedSalesChunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            document_title=row["document_title"],
            doc_type=row["doc_type"],
            audience=row["audience"],
            locale=row["locale"],
            text=row["text"],
            similarity=float(row["similarity"]),
        )
        for row in result.mappings().all()
    ]


async def ingest_document(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    title: str,
    content: str,
    doc_type: DocType = DocType.PRODUCT,
    audience: Audience = Audience.ALL,
    locale: str = "ar",
    source_uri: Optional[str] = None,
) -> tuple[SalesDocument, int, bool]:
    """Chunk, embed and store one document.

    Returns ``(document, chunk_count, created)``. Identical content is a no-op:
    the tenant+hash unique constraint means re-posting the same pricing sheet
    does not duplicate it or re-spend on embeddings.
    """
    digest = content_hash(content)
    existing = await db.execute(
        select(SalesDocument).where(
            SalesDocument.tenant_id == tenant_id,
            SalesDocument.content_hash == digest,
        )
    )
    document = existing.scalar_one_or_none()
    if document is not None:
        # Re-activate and refresh the metadata in case it was retired earlier.
        document.is_active = True
        document.title = title
        document.doc_type = doc_type.value
        document.audience = audience.value
        document.locale = locale
        chunk_count = await db.scalar(
            select(func.count())
            .select_from(SalesChunk)
            .where(SalesChunk.document_id == document.id)
        )
        return document, int(chunk_count or 0), False

    chunks = chunk_text(content)
    if not chunks:
        raise ValueError("document has no indexable content")

    document = SalesDocument(
        tenant_id=tenant_id,
        title=title,
        doc_type=doc_type.value,
        audience=audience.value,
        locale=locale,
        source_uri=source_uri,
        content=content,
        content_hash=digest,
        version=1,
        is_active=True,
    )
    db.add(document)
    await db.flush()

    # Embedding is the slow, paid part — run the whole document in one batch and
    # off the event loop.
    vectors = await asyncio.get_event_loop().run_in_executor(
        None, embed_texts, [chunk.text for chunk in chunks]
    )
    if len(vectors) != len(chunks):
        raise ValueError(
            f"embedding count {len(vectors)} does not match chunk count {len(chunks)}"
        )

    for chunk, vector in zip(chunks, vectors, strict=True):
        row = SalesChunk(
            tenant_id=tenant_id,
            document_id=document.id,
            chunk_index=chunk.index,
            text=chunk.text,
            token_count=chunk.token_count,
            content_hash=chunk.content_hash,
        )
        db.add(row)
        await db.flush()
        db.add(
            SalesChunkEmbedding(
                chunk_id=row.id,
                tenant_id=tenant_id,
                document_id=document.id,
                audience=audience.value,
                locale=locale,
                doc_type=doc_type.value,
                embedding=vector,
                embedding_model=settings.azure_deployment_embeddings,
            )
        )

    return document, len(chunks), True


async def deactivate_document(db: AsyncSession, document_id: uuid.UUID) -> bool:
    """Retire a document from retrieval, keeping it for audit.

    A soft retire rather than a delete: knowing what the agent was grounding on
    last month is the only way to explain an answer it gave last month.
    """
    document = await db.get(SalesDocument, document_id)
    if document is None:
        return False
    document.is_active = False
    return True


def build_context_block(chunks: list[RetrievedSalesChunk]) -> str:
    """Render retrieved passages for the prompt, tagged for citation."""
    blocks = []
    for i, chunk in enumerate(chunks, 1):
        blocks.append(
            f"[KB {i}]\n"
            f"source: {chunk.document_title}\n"
            f"type: {chunk.doc_type} | audience: {chunk.audience} | "
            f"language: {chunk.locale}\n"
            f"content: {chunk.text}"
        )
    return "\n\n".join(blocks)

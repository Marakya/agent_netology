"""База знаний (RAG): разбор документов, индексация и гибридный поиск в Qdrant.

Индексация: файл → текстовые секции → фрагменты с перекрытием → dense-вектор (OpenAI
embeddings) + sparse-вектор (частоты слов) → точки в Qdrant, текст дублируется в SQLite.
Поиск: dense- и sparse-кандидаты сливаются через RRF, результат фильтруется по владельцу.
"""

import hashlib
import io
import re
import threading
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from qdrant_client import QdrantClient, models

from .db import uid

MAX_FILE_BYTES = 10 * 1024 * 1024  # лимит загружаемого файла
MAX_UNPACKED_BYTES = 50 * 1024 * 1024  # лимит распакованного DOCX/PPTX (защита от zip-бомбы)
MAX_PDF_PAGES = 500
MAX_CHUNKS = 1500  # фрагментов на документ
CHUNK_SIZE = 2400  # длина фрагмента, символы
CHUNK_STEP = 2000  # шаг нарезки; соседние фрагменты перекрываются на CHUNK_SIZE - CHUNK_STEP
EMBED_BATCH = 32  # фрагментов в одном запросе embeddings
PREFETCH = 30  # кандидатов от каждого вида поиска (dense и sparse) до слияния


def words(text):
    """Слова текста в нижнем регистре (буквы и цифры любого языка)."""
    return re.findall(r"[\w]+", text.lower(), flags=re.UNICODE)


def sparse(text):
    """Лексический (sparse) вектор: индекс — хэш слова, значение — число вхождений.

    blake2b вместо встроенного hash(): результат одинаков в разных процессах.
    """
    counts = Counter(
        int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest(), "big") for w in words(text)
    )
    return models.SparseVector(indices=sorted(counts), values=[float(counts[i]) for i in sorted(counts)])


def parse_file(name, data):
    """Извлечь текст из файла. Возвращает список секций (locator, text).

    locator указывает место в документе (страница, слайд) и попадает в ссылку на источник.
    """
    suffix = Path(name).suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        return [("text", data.decode("utf-8-sig"))]
    if suffix in {".docx", ".pptx"}:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_UNPACKED_BYTES:
                raise ValueError("Слишком большой распакованный документ")
            if suffix == ".pptx":
                names = sorted(
                    (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)),
                )
                return [
                    (
                        f"slide {i + 1}",
                        "\n".join(
                            e.text or ""
                            for e in ElementTree.fromstring(archive.read(n)).iter()
                            if e.tag.endswith("}t")
                        ),
                    )
                    for i, n in enumerate(names)
                ]
        from docx import Document

        doc = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        text += "\n" + "\n".join(" | ".join(c.text for c in r.cells) for t in doc.tables for r in t.rows)
        return [("document", text)]
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("Лимит 500 страниц")
        return [(f"page {i + 1}", p.extract_text() or "") for i, p in enumerate(reader.pages)]
    raise ValueError("Поддерживаются TXT, MD, CSV, PDF, DOCX, PPTX")


class KnowledgeBase:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self.lock = threading.RLock()
        self.dim = settings.embedding_dimensions
        # Отдельная коллекция на каждую модель/размерность: векторы разных моделей несовместимы.
        # Префикс "live:" и суффикс ":v1" сохранены, чтобы уже проиндексированные базы остались доступны.
        revision = hashlib.sha256(f"live:{settings.embedding_model}:{self.dim}:v1".encode()).hexdigest()[:12]
        self.collection = f"knowledge_{revision}"
        self.client = (
            QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key.get_secret_value() or None,
                timeout=15,
            )
            if settings.qdrant_url
            else QdrantClient(path=str(settings.data_dir / "qdrant"))
        )
        from langchain_openai import OpenAIEmbeddings

        self.embedder = OpenAIEmbeddings(
            model=settings.embedding_model,
            dimensions=self.dim,
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            request_timeout=30,
            max_retries=1,
        )
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config={"dense": models.VectorParams(size=self.dim, distance=models.Distance.COSINE)},
                sparse_vectors_config={"lexical": models.SparseVectorParams()},
            )
        if settings.qdrant_url:
            self.client.create_payload_index(self.collection, "owner", models.PayloadSchemaType.KEYWORD)

    def ingest(self, owner, name, data):
        """Проиндексировать документ владельца и вернуть его запись из SQLite.

        Повторная загрузка того же содержимого (по SHA-256) возвращает существующий документ.
        Статусы: indexing → ready, при ошибке — failed.
        """
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Максимальный размер 10 MiB")
        file_hash = hashlib.sha256(data).hexdigest()
        with self.lock:
            existing = self.db.one("SELECT * FROM documents WHERE owner=? AND hash=?", (owner, file_hash))
            if existing and existing["state"] == "ready":
                return existing
            if existing:
                self.delete(owner, existing["id"])
            sections = parse_file(name, data)
            chunks = []
            for locator, text in sections:
                # Нарезка по символам не зависит от языка; перекрытие сохраняет контекст на границах.
                for offset in range(0, len(text), CHUNK_STEP):
                    piece = text[offset : offset + CHUNK_SIZE].strip()
                    if piece:
                        chunks.append((f"{locator}, chars {offset}:{offset + len(piece)}", piece))
            if not chunks:
                raise ValueError("Текст не найден. Для сканированного PDF нужен OCR.")
            if len(chunks) > MAX_CHUNKS:
                raise ValueError("Документ превышает лимит 1500 фрагментов")
            doc_id = uid()
            self.db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                (doc_id, owner, Path(name).name, file_hash, "indexing", len(chunks), time.time()),
            )
            try:
                for offset in range(0, len(chunks), EMBED_BATCH):
                    batch = chunks[offset : offset + EMBED_BATCH]
                    vectors = self.embedder.embed_documents([t for _, t in batch])
                    points = []
                    for (locator, text), vector in zip(batch, vectors):
                        chunk_id = str(uuid.uuid4())
                        self.db.execute(
                            "INSERT INTO chunks VALUES(?,?,?,?,?)", (chunk_id, doc_id, owner, text, locator)
                        )
                        points.append(
                            models.PointStruct(
                                id=chunk_id,
                                vector={"dense": vector, "lexical": sparse(text)},
                                payload={
                                    "owner": owner,
                                    "document_id": doc_id,
                                    "text": text,
                                    "locator": locator,
                                },
                            )
                        )
                    self.client.upsert(self.collection, points, wait=True)
                self.db.execute("UPDATE documents SET state='ready' WHERE id=?", (doc_id,))
            except Exception:
                self.db.execute("UPDATE documents SET state='failed' WHERE id=?", (doc_id,))
                raise
            return self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))

    def search(self, owner, query, limit=6):
        """Гибридный поиск по документам владельца. Возвращает до limit источников."""
        if not words(query):
            return []
        with self.lock:
            scope = models.Filter(
                must=[models.FieldCondition(key="owner", match=models.MatchValue(value=owner))]
            )
            result = self.client.query_points(
                self.collection,
                prefetch=[
                    models.Prefetch(
                        query=self.embedder.embed_query(query), using="dense", filter=scope, limit=PREFETCH
                    ),
                    models.Prefetch(query=sparse(query), using="lexical", filter=scope, limit=PREFETCH),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=scope,
                limit=20,
            )
            # Индекс мог отстать от SQLite (документ удаляется или переиндексируется):
            # берём только фрагменты, которые существуют и принадлежат готовому документу владельца.
            output = []
            for point in result.points:
                row = self.db.one(
                    """SELECT c.*,d.name,d.hash FROM chunks c JOIN documents d ON d.id=c.document_id
                  WHERE c.id=? AND c.owner=? AND d.state='ready'""",
                    (str(point.id), owner),
                )
                if row:
                    output.append(
                        {
                            "source_id": row["id"],
                            "kind": "document",
                            "title": row["name"],
                            "locator": row["locator"],
                            "excerpt": row["text"],
                            "document_id": row["document_id"],
                            "document_version": 1,
                            "content_hash": "sha256:" + row["hash"],
                        }
                    )
                if len(output) >= limit:
                    break
            return output

    def delete(self, owner, doc_id):
        """Удалить документ владельца из индекса и из SQLite (KeyError, если его нет)."""
        with self.lock:
            row = self.db.one("SELECT * FROM documents WHERE id=? AND owner=?", (doc_id, owner))
            if not row:
                raise KeyError("Документ не найден")
            self.db.execute("UPDATE documents SET state='deleted' WHERE id=?", (doc_id,))
            self.client.delete(
                self.collection,
                models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(key="document_id", match=models.MatchValue(value=doc_id)),
                            models.FieldCondition(key="owner", match=models.MatchValue(value=owner)),
                        ]
                    )
                ),
                wait=True,
            )
            self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def close(self):
        self.client.close()

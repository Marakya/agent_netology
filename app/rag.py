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


def words(text):
    return re.findall(r"[\w]+", text.lower(), flags=re.UNICODE)


def sparse(text):
    counts = Counter(
        int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest(), "big") for w in words(text)
    )
    # A stable lexical vector, independent of process hash randomization.
    return models.SparseVector(indices=sorted(counts), values=[float(counts[i]) for i in sorted(counts)])


def parse_file(name, data):
    suffix = Path(name).suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        return [("text", data.decode("utf-8-sig"))]
    if suffix in {".docx", ".pptx"}:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024:
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
        if len(reader.pages) > 500:
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
        if len(data) > 10 * 1024 * 1024:
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
                # Character chunks preserve original text and language; overlap keeps boundary context.
                for offset in range(0, len(text), 2000):
                    piece = text[offset : offset + 2400].strip()
                    if piece:
                        chunks.append((f"{locator}, chars {offset}:{offset + len(piece)}", piece))
            if not chunks:
                raise ValueError("Текст не найден. Для сканированного PDF нужен OCR.")
            if len(chunks) > 1500:
                raise ValueError("Документ превышает лимит 1500 фрагментов")
            doc_id = uid()
            self.db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                (doc_id, owner, Path(name).name, file_hash, "indexing", len(chunks), time.time()),
            )
            try:
                for offset in range(0, len(chunks), 32):
                    batch = chunks[offset : offset + 32]
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
                        query=self.embedder.embed_query(query), using="dense", filter=scope, limit=30
                    ),
                    models.Prefetch(query=sparse(query), using="lexical", filter=scope, limit=30),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=scope,
                limit=20,
            )
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

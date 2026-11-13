"""База знаний: загрузка, список и удаление документов, учебный магазин."""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..rag import MAX_FILE_BYTES
from ..support import SAMPLE_DIR
from .deps import assert_idle, principal, reset_context, services

router = APIRouter(prefix="/api")


def add_document(s, owner, name, data):
    """Проиндексировать документ и сохранить исходный файл как артефакт.

    Исходник нужен песочнице (попадает в /workspace/input). Повторная загрузка того же
    содержимого возвращает существующий документ и второй копии файла не создаёт.
    """
    doc = s.rag.ingest(owner, name, data)
    if not s.db.one("SELECT artifact_id FROM document_inputs WHERE document_id=?", (doc["id"],)):
        artifact = s.artifacts.save(owner, None, name, data)
        s.db.execute("INSERT INTO document_inputs VALUES(?,?)", (doc["id"], artifact["artifact_id"]))
    return doc


@router.get("/documents")
def documents(owner=Depends(principal), s=Depends(services)):
    return s.db.all("SELECT * FROM documents WHERE owner=? ORDER BY created DESC", (owner,))


@router.post("/documents", status_code=201)
def upload(file: UploadFile = File(), owner=Depends(principal), s=Depends(services)):
    # Читаем на байт больше лимита: так превышение видно без загрузки всего файла в память.
    data = file.file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Максимальный файл 10 MiB")
    try:
        return add_document(s, owner, file.filename or "document.txt", data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/support/sample")
def load_sample(owner=Depends(principal), s=Depends(services)):
    """Загрузить учебный магазин «Лист» (app/sample_store). Повтор не создаёт дубликатов."""
    assert_idle(s, owner)
    with s.rag.lock:
        loaded = [
            add_document(s, owner, "Лист — " + path.name, path.read_bytes())
            for path in sorted(SAMPLE_DIR.glob("*.md"))
        ]
    return {"store": "Лист", "synthetic": True, "documents": loaded}


@router.delete("/documents/{doc_id}")
def document_delete(doc_id: str, owner=Depends(principal), s=Depends(services)):
    """Удалить документ из индекса вместе с исходным файлом и сбросить контекст бесед."""
    assert_idle(s, owner)
    inputs = s.db.all(
        """SELECT a.* FROM artifacts a JOIN document_inputs d ON a.id=d.artifact_id
           WHERE d.document_id=? AND a.owner=?""",
        (doc_id, owner),
    )
    try:
        s.rag.delete(owner, doc_id)
    except KeyError:
        raise HTTPException(404, "Документ не найден") from None
    for artifact in inputs:
        Path(artifact["path"]).unlink(missing_ok=True)
        s.db.execute("DELETE FROM artifacts WHERE id=?", (artifact["id"],))
    reset_context(s, owner)
    return {"deleted_from_index": True, "context_reset": True}

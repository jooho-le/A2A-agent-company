import logging
import os
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from database import get_db
from models import Document, User
from routers.auth import read_current_user
from schemas import DocumentListResponse, DocumentResponse


router = APIRouter(prefix="/api/documents")
logger = logging.getLogger(__name__)
UPLOAD_DIRECTORY = Path(__file__).resolve().parents[1] / "uploads"
MAX_FILE_SIZE = 10 * 1024 * 1024


@router.get("", response_model=DocumentListResponse)
def list_documents(
    view: Literal["submitted", "assigned"] = Query(
        default="submitted", description="submitted: 내가 제출한 문서, assigned: 내가 담당자인 문서"
    ),
    current_user: User = Depends(read_current_user),
    db: Session = Depends(get_db),
):
    # 조회 기준 ID는 쿼리 입력이 아닌 JWT로 인증된 현재 사용자에게서만 가져옵니다.
    user_column = Document.submitter_id if view == "submitted" else Document.assignee_id
    query = (
        select(Document)
        .where(user_column == current_user.id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    return {"documents": db.scalars(query).all()}


@router.get("/{document_id}/download", response_class=Response)
def download_document(
    document_id: int,
    current_user: User = Depends(read_current_user),
    db: Session = Depends(get_db),
):
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    if current_user.id not in (document.submitter_id, document.assignee_id):
        raise HTTPException(status_code=403, detail="문서를 다운로드할 권한이 없습니다.")

    try:
        # 기존 업로드가 생성하는 UUID v4 파일명만 허용합니다.
        stored_name = document.stored_filename
        file_uuid = UUID(stored_name.removesuffix(".pdf"))
        if file_uuid.version != 4 or stored_name != f"{file_uuid}.pdf":
            raise ValueError("유효하지 않은 저장 파일명")
        if Path(document.file_path) != UPLOAD_DIRECTORY / stored_name:
            raise ValueError("유효하지 않은 저장 경로")

        # 디렉터리와 파일 모두 심볼릭 링크를 따라가지 않고 엽니다.
        directory_fd = os.open(UPLOAD_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            file_fd = os.open(
                stored_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)

        # 열린 파일을 그대로 읽어, 검사 뒤 경로 교체로 다른 파일이 열리는 것을 막습니다.
        try:
            file_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink > 1
                or file_stat.st_size != document.file_size
            ):
                raise ValueError("유효하지 않은 저장 파일")
            with os.fdopen(file_fd, "rb", closefd=False) as saved_file:
                contents = saved_file.read(MAX_FILE_SIZE + 1)
            if len(contents) > MAX_FILE_SIZE or len(contents) != document.file_size:
                raise ValueError("유효하지 않은 파일 크기")
        finally:
            os.close(file_fd)
    except (OSError, ValueError, TypeError):
        # 누락, 경로 조작, 파일 접근 오류 모두 내부 경로나 예외를 노출하지 않습니다.
        raise HTTPException(status_code=404, detail="문서 파일을 찾을 수 없습니다.") from None

    # 원본 파일명에서도 경로와 제어 문자를 제거하고 UTF-8로 인코딩합니다.
    filename = document.original_filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(character for character in filename if character.isprintable()).strip()
    if filename in ("", ".", ".."):
        filename = "document.pdf"
    disposition = f"attachment; filename=\"document.pdf\"; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(
        content=contents,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    assignee_id: int = Form(..., gt=0),
    current_user: User = Depends(read_current_user),
    db: Session = Depends(get_db),
):
    # 같은 필드로 여러 파일을 보내거나 다른 이름의 파일을 추가해도 거부합니다.
    form = await request.form()
    uploaded_files = [
        value for _, value in form.multi_items()
        if isinstance(value, StarletteUploadFile)
    ]
    if len(uploaded_files) != 1:
        raise HTTPException(status_code=400, detail="PDF 파일은 한 번에 1개만 제출할 수 있습니다.")

    if db.get(User, assignee_id) is None:
        raise HTTPException(status_code=400, detail="지정한 담당자가 존재하지 않습니다.")
    if not file.filename:
        raise HTTPException(status_code=400, detail="파일명이 필요합니다.")

    try:
        # 제한보다 1바이트만 더 읽어서 정확히 10MiB인 파일은 허용합니다.
        contents = await file.read(MAX_FILE_SIZE + 1)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="파일을 읽는 중 오류가 발생했습니다.") from exc
    finally:
        await file.close()

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="파일 크기는 10MiB 이하여야 합니다.")
    # 확장자와 클라이언트 Content-Type 대신 실제 파일 시작 바이트를 확인합니다.
    if not contents.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="PDF 파일만 제출할 수 있습니다.")

    stored_filename = f"{uuid4()}.pdf"
    file_path = UPLOAD_DIRECTORY / stored_filename
    file_created = False

    try:
        UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
        # 기존 파일을 덮어쓰지 않는 방식으로 새 파일을 만듭니다.
        with file_path.open("xb") as saved_file:
            file_created = True
            saved_file.write(contents)

        document = Document(
            submitter_id=current_user.id,
            assignee_id=assignee_id,
            original_filename=file.filename,
            stored_filename=stored_filename,
            file_path=str(file_path),
            file_size=len(contents),
            content_type="application/pdf",
        )
        db.add(document)
        db.flush()
        db.refresh(document)
        # 응답 생성까지 확인한 뒤 commit하여 이후 DB 조회 실패로 파일을 지우지 않습니다.
        response = DocumentResponse.model_validate(document)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        finally:
            if file_created:
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("실패한 업로드 파일을 정리하지 못했습니다.")
        logger.exception("문서 파일 또는 메타데이터 저장에 실패했습니다.")
        raise HTTPException(status_code=500, detail="문서 저장 중 오류가 발생했습니다.") from exc

    return response

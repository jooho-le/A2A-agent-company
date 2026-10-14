import logging
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
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

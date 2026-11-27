"""FastAPI 앱 진입점(시작 코드 골격). 회원가입 기능은 들어 있지 않다.

연결 규격: 앱은 이 파일의 `app`이다. 서버는 `uvicorn main:app`으로 띄운다.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import models  # noqa: F401  # 모델을 Base에 등록한다(개발 에이전트가 models.py에 모델을 만든다).
from database import Base, engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 시작할 때 등록된 모델의 테이블을 만든다. DB 위치는 DATABASE_URL로 정해진다(database.py).
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health():
    # 서버 준비 확인용(브라우저 시험 도구가 이 경로의 2xx 응답으로 준비를 확인한다).
    return {"status": "ok"}

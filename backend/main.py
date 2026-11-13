from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from database import Base, engine
import models  # 기존 제품 테이블은 시작 시 생성하고 프로젝트 테이블은 명시적으로 확장합니다.
from routers.auth import router as auth_router
from routers.documents import router as documents_router
from routers.projects import router as projects_router
from routers.host_runs import router as host_runs_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(
        bind=engine, tables=[models.User.__table__, models.Document.__table__]
    )
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(projects_router)
app.include_router(host_runs_router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # 입력한 비밀번호가 검증 실패 응답에 그대로 포함되지 않게 합니다.
    errors = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/")
def read_root():
    return {"message": "A2A backend is running"}

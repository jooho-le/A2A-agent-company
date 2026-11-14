from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, server_default="user")
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    submitter_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    original_filename = Column(String, nullable=False)
    stored_filename = Column(String, nullable=False, unique=True)
    file_path = Column(String, nullable=False)
    file_size = Column(Integer, nullable=False)
    content_type = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    # Host의 Run ID와 별개로 제품 백엔드가 생성하는 UUIDv4입니다.
    project_id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    name = Column(String, nullable=False)
    # 시나리오 레지스트리는 Host가 관리하므로 제품 DB의 외래키로 가정하지 않습니다.
    scenario_id = Column(String(36), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ProjectRun(Base):
    __tablename__ = "project_runs"
    __table_args__ = (
        UniqueConstraint("host_key", "run_id", name="uq_project_runs_host_run"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), nullable=False, index=True)
    host_key = Column(String, nullable=False, default="local-multi", server_default="local-multi")
    run_id = Column(String(36), nullable=False)
    # 설정 참조의 형식은 Host 계약에 따르며 설정 본문이나 비밀정보는 저장하지 않습니다.
    configuration_id = Column(String, nullable=False)
    linked_at = Column(DateTime, nullable=False, server_default=func.now())

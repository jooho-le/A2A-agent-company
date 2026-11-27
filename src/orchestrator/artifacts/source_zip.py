"""Bounded ZIP projection of verified immutable Source; never extract or execute."""

from dataclasses import dataclass, field
from hashlib import sha256
import io
import stat
import unicodedata
import zipfile

from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.sandbox.contracts import SandboxError, SandboxErrorCode
from orchestrator.sandbox.materialization import _archive_entries


@dataclass(frozen=True)
class SourceZip:
    content: bytes = field(repr=False)
    filename: str
    archive_sha256: str
    snapshot_sha256: str
    code_version: int


class _LimitedBuffer(io.BytesIO):
    def __init__(self, maximum):
        super().__init__()
        self.maximum = maximum

    def write(self, value):
        if self.tell() + len(value) > self.maximum:
            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
        return super().write(value)


def _runtime_path(name):
    parts = unicodedata.normalize("NFKC", name).casefold().split("/")
    basename = parts[-1]
    return (parts[0] in {".data", ".sandbox", "workspaces", "snapshots", "outputs", "planning"}
        or "logs" in parts or basename == ".ds_store"
        or basename.endswith((".db", ".sqlite", ".sqlite3", ".log",
            ".db-wal", ".db-shm", ".db-journal", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
            ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal"))
        or basename.startswith("owned-") and basename.endswith(".local.json"))


def build_source_zip(store, run_id, artifact_id) -> SourceZip:
    record = store.read_registered_source(run_id, artifact_id)
    limits = store._git_limits
    if len(record.content) > limits.max_archive_bytes:
        raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
    try:
        # Share Sandbox's canonical tar/path/type/credential policy. It rejects
        # duplicates, aliases, links, special files and hidden trailing archives.
        entries = _archive_entries(record.content)
    except SandboxError as error:
        code = ArtifactErrorCode.PATH_DENIED if error.code == SandboxErrorCode.PATH else ArtifactErrorCode.INTEGRITY
        raise ArtifactAccessError(code) from None
    if (len(entries) > limits.max_files or sum(len(content) for _, content, _ in entries) > limits.max_total_bytes
            or any(len(content) > limits.max_file_bytes for _, content, _ in entries)):
        raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
    if any(_runtime_path(name) for name, _, _ in entries):
        # Never silently omit a file and pretend the remaining ZIP is the Source.
        raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
    target = _LimitedBuffer(limits.max_archive_bytes)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=False) as archive:
            for name, content, mode in entries:
                member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(member, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        content = target.getvalue()
    except ArtifactAccessError:
        raise
    except Exception:
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
    source = record.metadata
    return SourceZip(content=content, filename=f"source-{source.run_id}-v{source.code_version}.zip",
        archive_sha256=sha256(content).hexdigest(), snapshot_sha256=source.snapshot_sha256,
        code_version=source.code_version)

"""Host-side export of frozen Source Snapshots for independent evaluation.

The Orchestrator stores each code version's Source as one canonical tar BLOB
with its Execution Manifest identity. This trusted operator tool copies those
exact bytes and the manifest out of the Host database so an evaluator can
score the final (or every) code version outside the development loop.

It reads stored artifacts only: project Source is never extracted, imported or
executed here, and nothing is sent anywhere. Bytes are written only after the
stored content hash, the tar hash and the manifest's snapshotSha256 agree.

    python -m agents.platform.snapshot_export --database PATH --run-id UUID --out DIR [--all-versions] [--force]
"""

import argparse
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import UUID

from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class SnapshotExportError(ValueError):
    """Stable code only; no paths, Source bytes or underlying exception."""

    def __init__(self, code="SNAPSHOT_EXPORT_FAILED"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExportedSnapshot:
    code_version: int
    archive: bytes = field(repr=False)
    manifest: dict = field(repr=False)


def export_snapshots(repository, run_id, *, all_versions=False) -> tuple[ExportedSnapshot, ...]:
    """Return the final code version's Snapshot, or every version's in order."""
    if not isinstance(repository, SQLiteWorkflowRepository):
        raise SnapshotExportError()
    try:
        run_id = UUID(str(run_id))
        run = repository.get_run(run_id)
    except Exception:
        raise SnapshotExportError("RUN_NOT_FOUND") from None
    if run is None:
        raise SnapshotExportError("RUN_NOT_FOUND")
    sources = [item for item in repository.list_project_artifacts(run_id) if item.artifact_type == "SOURCE"]
    versions = sorted({item.code_version for item in sources})
    if len(versions) != len(sources) or not versions:
        raise SnapshotExportError("SNAPSHOT_NOT_FOUND")
    if not all_versions:
        if run.code_version not in versions:
            raise SnapshotExportError("SNAPSHOT_NOT_FOUND")
        versions = [run.code_version]
    contents, exported = SQLiteArtifactContentStore(repository), []
    for version in versions:
        source = next(item for item in sources if item.code_version == version)
        try:
            record = contents.get(run_id, source.artifact_id)
        except Exception:
            raise SnapshotExportError("SNAPSHOT_NOT_FOUND") from None
        digest = sha256(record.content).hexdigest()
        # The registry row gains A2A Task/Artifact IDs after publication; the
        # stored content keeps the frozen form. Their code identity must agree.
        if (not isinstance(record.metadata, CodeSnapshotArtifact) or record.metadata.artifact_id != source.artifact_id
                or record.metadata.execution_manifest() != source.execution_manifest()
                or record.media_type != "application/x-tar" or record.content_sha256 != digest
                or digest != source.snapshot_sha256 or source.run_id != run_id):
            raise SnapshotExportError("SNAPSHOT_INTEGRITY_ERROR")
        manifest = source.execution_manifest().model_dump(mode="json", by_alias=True)
        exported.append(ExportedSnapshot(code_version=version, archive=record.content, manifest=manifest))
    return tuple(exported)


def write_export(repository, run_id, out, *, all_versions=False, force=False) -> dict:
    """Write v<N>/source.tar, v<N>/manifest.json and run.json; return {name: path}."""
    snapshots = export_snapshots(repository, run_id, all_versions=all_versions)
    run = repository.get_run(UUID(str(run_id)))
    out = Path(out)
    documents = {"run.json": json.dumps({
        "runId": str(run.run_id), "status": run.status.value,
        "verdict": None if run.verdict is None else run.verdict.value,
        "finalCodeVersion": run.code_version,
        "exportedCodeVersions": [item.code_version for item in snapshots]}, indent=2).encode() + b"\n"}
    for item in snapshots:
        documents[f"v{item.code_version}/source.tar"] = item.archive
        documents[f"v{item.code_version}/manifest.json"] = json.dumps(item.manifest, indent=2).encode() + b"\n"
    if not force and any((out / name).exists() for name in documents):
        raise SnapshotExportError("EXPORT_EXISTS")
    written = {}
    for name, data in documents.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written[name] = path
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m agents.platform.snapshot_export",
                                     description="Export frozen Source Snapshots for independent evaluation.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--all-versions", action="store_true", help="every code version, not only the final one")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args(argv)
    try:
        if not options.database.is_file():
            raise SnapshotExportError("DATABASE_NOT_FOUND")
        written = write_export(SQLiteWorkflowRepository(options.database), options.run_id, options.out,
                               all_versions=options.all_versions, force=options.force)
    except SnapshotExportError as error:
        sys.stderr.write(error.code + "\n")
        return 1
    for name, path in written.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

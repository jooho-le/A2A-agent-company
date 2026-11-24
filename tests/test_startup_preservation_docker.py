"""Actual isolated startup and grading; no model/API calls or experimental Runs."""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from agents.runtime.startup_preservation import with_startup_context
from agents.runtime.developer_input import compact_fix_input
from evaluation import protected_tests as pt
from evaluation.final_evaluation import DockerRunner, _runner_source
from orchestrator.sandbox.scn001_start_contract import scn001_build_argv, scn001_build_diagnostics
from tests.evaluation import signup_service
from test_startup_preservation import CONTRACT, checkpoint

pytestmark = pytest.mark.skipif(os.getenv('A2A_DOCKER_REPAIR_TEST') != '1',
                                reason='requires explicit local Docker verification')


def write_tree(root, files):
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        for directory in path.parents:
            if directory == root.parent:
                break
            directory.chmod(0o755)
        path.write_text(content)
        path.chmod(0o644)


def start(runner, source, image):
    name = 'startup-preservation98-' + uuid4().hex[:12]
    try:
        return runner._call(runner._base_command(name) + [
            '--mount', f'type=bind,src={source},dst=/snapshot,readonly',
            '--entrypoint', runner.python, image, *scn001_build_argv(runner.python)[1:]], 60)
    except Exception:
        runner._remove_timed_out_container(name)
        raise


def test_actual_missing_table_diagnostic_then_connected_startup_and_nine_checks(tmp_path):
    image = json.loads((Path(__file__).resolve().parents[1] / 'configs/owned-tools.local.json').read_text())['imageReference']
    runner = DockerRunner(image, timeout=60)
    assert runner.image_digest() == image
    source, inputs = tmp_path / 'source', tmp_path / 'inputs'
    good = signup_service.build('good')
    good['main.py'] += '\n@app.get("/")\ndef health():\n    return {"status": "ok"}\n'
    bad = {**good, 'main.py': good['main.py'].replace('FastAPI(lifespan=lifespan)', 'FastAPI()')}
    write_tree(source, bad)
    failed = start(runner, source, image)
    assert (failed.returncode, failed.stdout.splitlines()[-1]) == (1, 'SCN001_USER_TABLE_REQUIRED')
    diagnostics = scn001_build_diagnostics(argv=scn001_build_argv(runner.python), exit_code=failed.returncode,
        stdout=failed.stdout, source_files={n: c.encode() for n, c in bad.items()})
    issue = dict(issueId='measured-build98', category='BUILD_CODE_FAILURE', codeVersion=1,
                 sourceArtifactId='source98-v1', reportArtifactId='build98-v1')
    record = dict(artifactId='build98-v1', sourceArtifactId='source98-v1', codeVersion=1,
        executionOutcome='FAIL', failureKind='PRODUCT', diagnostics=[d.model_dump(by_alias=True) for d in diagnostics])
    task = compact_fix_input({'fixRequest': {'issues': [issue], 'inputArtifacts': [
        dict(artifactId='build98-v1', artifactType='BUILD_REPORT', record=record)]}})
    target, = with_startup_context(task, checkpoint(), CONTRACT)['activeRepairFocus']['brief']['startupRepairTargets']
    assert target['ruleId'] == 'SCN001_USER_TABLE_REQUIRED' and target['sourceArtifactId'] == 'source98-v1'
    # Controlled fixture repair, not a model choosing or generating a solution.
    # Preserve every route and handler byte; reconnect only the application lifecycle.
    fixed = bad['main.py'].replace('app = FastAPI()', 'app = FastAPI(lifespan=lifespan)')
    assert fixed == good['main.py']
    write_tree(source, {'main.py': fixed})
    passed = start(runner, source, image)
    assert (passed.returncode, passed.stdout.splitlines()[-1]) == (0, 'SCN001_START_CONTRACT_OK')
    files = pt.protected_files('final')
    files['_unit_runner.py'] = _runner_source().decode()
    files['_replay_runner.py'] = (Path(__file__).resolve().parents[1] / 'src/evaluation/replay_runner.py').read_text()
    files['_input_replay.json'] = '{}'
    write_tree(inputs, files)
    evaluated = runner.run(source, inputs, 'tests/signup', pt.load_suite()['scopes']['final']['pattern'],
                           script='_replay_runner.py')
    assert evaluated.error is None and evaluated.report is not None, 'CONTAINER_EVALUATION_UNAVAILABLE'
    assert (evaluated.report['total'], evaluated.report['passed'], evaluated.report['failed'],
            evaluated.report['skipped']) == (9, 9, 0, 0)

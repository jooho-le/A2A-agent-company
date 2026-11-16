"""Normal experiment scoring preserves private concrete inputs across regrades."""
from dataclasses import replace
import json
from pathlib import Path
import stat
from uuid import uuid4

from evaluation.experiment import score_export
from evaluation.final_evaluation import LocalRunner
from evaluation.replay import replay_evaluation
from tests.evaluation import signup_service
from tests.evaluation.test_final_evaluation import CONTRACT, IMAGE, LOCK, canonical_tar, manifest


class CapturingFixtureRunner:
    # Authored local service only; this stands in for image verification.
    kind = 'docker'

    def __init__(self):
        self.local = LocalRunner(trusted=True)

    def run(self, *args, **kwargs):
        return replace(self.local.run(*args, **kwargs), image_digest=IMAGE, kind=self.kind)


def test_version_inputs_are_private_replayable_and_survive_force_regrade(tmp_path):
    run_id = str(uuid4())
    export, results = tmp_path / 'export', tmp_path / 'results'
    for version in (1, 2):
        source = {path: text.encode() for path, text in signup_service.build('good').items()}
        archive = canonical_tar({**source, 'requirements.lock': LOCK})
        expected = {**manifest(archive).to_payload(), 'codeVersion': version}
        folder = export / f'v{version}'
        folder.mkdir(parents=True)
        (folder / 'source.tar').write_bytes(archive)
        (folder / 'manifest.json').write_text(json.dumps(expected))
    (export / 'run.json').write_text(json.dumps({
        'runId': run_id, 'status': 'FINISHED', 'verdict': 'SUCCESS',
        'finalCodeVersion': 2, 'exportedCodeVersions': [1, 2]}))
    runner = CapturingFixtureRunner()
    folder = score_export(export, results=results, runner=runner, exam='pilot',
                          contract=CONTRACT, all_versions=True)
    paths = [folder / 'output.json', folder / 'version_outputs' / 'v1.json']
    private = results / '.private-inputs' / run_id
    original = {}
    baselines = {}
    for path in paths:
        output = json.loads(path.read_text())
        assert output['inputRecord']['status'] == 'RECORDED'
        tape = private / output['inputRecord']['fileName']
        assert stat.S_IMODE(tape.stat().st_mode) == 0o600
        original[tape.name] = tape.read_bytes()
        baseline = results / '.evaluation-history' / run_id / output['report']['evaluationId'] / 'output.json'
        assert json.loads(baseline.read_text()) == output
        baselines[baseline] = baseline.read_bytes()
        data = json.loads(original[tape.name])
        passwords = [event['value'] for scope in data['scopes'].values()
                     for rows in scope['tests'].values() for event in rows
                     if event['kind'] == 'password' and len(event['value']) >= 8]
        assert passwords
        public_text = '\n'.join(p.read_text() for p in folder.rglob('*.json'))
        assert all(password not in public_text for password in passwords)
    assert (folder / 'versions' / 'v1.json').is_file()
    score_export(export, results=results, runner=runner, exam='pilot', contract=CONTRACT,
                 all_versions=True, force=True)
    assert len(list(private.glob('*.json'))) == 4
    assert all((private / name).read_bytes() == data for name, data in original.items())
    assert all(path.read_bytes() == data for path, data in baselines.items())
    original_output = json.loads(next(iter(baselines.values())))
    # The original tape must remain usable, not merely exist after force.
    replay = replay_evaluation(baseline=original_output,
                              input_path=private / original_output['inputRecord']['fileName'],
                              archive=(export / 'v2' / 'source.tar').read_bytes(),
                              expected=replace(manifest((export / 'v2' / 'source.tar').read_bytes()), code_version=2),
                              scenario_contract=CONTRACT, runner=runner)
    assert replay['inputReplay']['scopes']['pilot']['status'] == 'PASS'

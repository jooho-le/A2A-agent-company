"""Synthetic receipts for signup validation paths. Protected runs have no generated observations."""
import json
from dataclasses import replace

from test_sandbox_runtime import FakeDocker


OUTPUT_CHANNELS = ('SUCCESS_RESPONSE', 'REJECTION_RESPONSE', 'STORAGE_ERROR_RESPONSE',
                   'REQUEST_LOGS', 'STORAGE_ERROR_REQUEST_LOGS')


def generated_signup_evidence(role, ordinal):
    if role == 'QA':
        return {'signupChecks': {'concurrentCanonicalSignup': True}} if ordinal == 3 else {}
    if ordinal == 1:
        return {'signupChecks': {'canonicalEmailUniqueKey': True}}
    if ordinal == 2:
        return {'signupChecks': {'storedHashParameters': {'argon2id': True, 'verified': True,
                'distinctSalts': True, 'memoryKiB': 19456, 'iterations': 2, 'parallelism': 1}}}
    return {'outputChecks': dict.fromkeys(OUTPUT_CHANNELS, 'CHECKED')}


class SignupFixtureDocker(FakeDocker):
    async def run(self, args, **opts):
        result = await super().run(args, **opts)
        command = args[1:] if args[0] == 'container' else args
        if command[0] == 'start' and 'PROTECTED' in self.container['Config']['Cmd'] and result.returncode in (0, 1):
            data = json.loads(result.stdout)
            for row in data['tests']:
                row.pop('signupChecks', None)
                row.pop('outputChecks', None)
            result = replace(result, stdout=json.dumps(data).encode())
        return result

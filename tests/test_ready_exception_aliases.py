"""READY evidence for FastAPI's input error handler; no generated code is imported."""
import pytest

from agents.runtime.ready_path_diff import changed_path_refs, claim_issues, INPUT


PREAMBLE = 'from fastapi import FastAPI\napp = FastAPI()\n'
HANDLER = 'def invalid(request, exc):\n    return str(exc)\n'
REF = 'source/main.py::invalid'


def evidence(before, after):
    return changed_path_refs(before, after, (INPUT,), ('POST', '/signup'), ('main', 'app'))


@pytest.mark.parametrize(('imports', 'error'), [
    ('from fastapi.exceptions import RequestValidationError as E', 'E'),
    # The Single Ver1 candidate imports both names and uses the first.
    ('from fastapi.exception_handlers import RequestValidationError\n'
     'from fastapi.exceptions import RequestValidationError as FastAPIRequestValidationError', 'RequestValidationError'),
    ('import fastapi.exception_handlers as handlers', 'handlers.RequestValidationError'),
    ('import fastapi.exception_handlers', 'fastapi.exception_handlers.RequestValidationError'),
    ('from fastapi import routing as routes', 'routes.RequestValidationError'),
    ('from fastapi.applications import RequestValidationError', 'RequestValidationError'),
])
def test_registered_class_edit_links_to_the_ready_claim(imports, error):
    text = PREAMBLE + imports + '\n@app.exception_handler(' + error + ')\n' + HANDLER
    edits = evidence({'main.py': text.encode()}, {'main.py': text.replace('str(exc)', '"invalid"').encode()})
    assert edits == {INPUT: {REF}}
    assert not claim_issues({INPUT: {'edit': 'Updated validation handler', 'sourceRef': REF}}, edits)


def test_local_reexports_follow_the_import_chain():
    before = {
        'main.py': (PREAMBLE + 'from app_errors import R\n@app.exception_handler(R)\n' + HANDLER).encode(),
        'app_errors/__init__.py': b'from .shared import Invalid as R\n',
        'app_errors/shared.py': b'from fastapi.exception_handlers import RequestValidationError as Invalid\n',
    }
    assert evidence(before, {**before, 'main.py': before['main.py'].replace(b'str(exc)', b'"invalid"')}) == {INPUT: {REF}}


@pytest.mark.parametrize(('imports', 'error'), [
    ('from other.exceptions import RequestValidationError', 'RequestValidationError'),
    ('from fastapi.exception_handlers import WebSocketRequestValidationError as E', 'E'),
    ('from fastapi.exception_handlers import RequestValidationError as E\nclass E(Exception): pass', 'E'),
    ('from fastapi.exception_handlers import RequestValidationError as E\nE = ValueError', 'E'),
    ('from fastapi.exception_handlers import RequestValidationError as E\nif enabled: E = ValueError', 'E'),
    ('from fastapi.exception_handlers import RequestValidationError as E\nfrom app_errors import *', 'E'),
    ('import fastapi.exception_handlers as handlers\nhandlers.RequestValidationError = ValueError',
     'handlers.RequestValidationError'),
])
def test_unrelated_or_rebound_class_cannot_support_a_ready_claim(imports, error):
    before = {'main.py': (PREAMBLE + imports + '\n').encode()}
    after = {'main.py': before['main.py'] + ('@app.exception_handler(' + error + ')\n' + HANDLER).encode()}
    assert evidence(before, after) == {INPUT: set()}


def test_local_module_named_fastapi_does_not_prove_framework_identity():
    before = {'main.py': (PREAMBLE + 'from fastapi.exception_handlers import RequestValidationError as E\n').encode(),
              'fastapi/exception_handlers.py': b'class RequestValidationError(Exception): pass\n'}
    after = {**before, 'main.py': before['main.py'] + ('@app.exception_handler(E)\n' + HANDLER).encode()}
    assert evidence(before, after) == {INPUT: set()}


@pytest.mark.parametrize('decorated', [True, False])
def test_only_changing_the_spelling_does_not_count_as_a_repair(decorated):
    def code(module, local):
        imports = f'from fastapi.{module} import RequestValidationError as {local}\n'
        if decorated:
            registration = f'@app.exception_handler({local})\n' + HANDLER
        else:
            registration = HANDLER + f'app.add_exception_handler({local}, invalid)\n'
        return {'main.py': (PREAMBLE + imports + registration).encode()}
    assert evidence(code('exceptions', 'Old'), code('exception_handlers', 'Current')) == {INPUT: set()}


def test_shadowed_parameter_used_in_runtime_registration_is_not_framework_identity():
    before = {'main.py': (PREAMBLE + 'from fastapi.exception_handlers import RequestValidationError as E\n'
                          + HANDLER + "@app.on_event('startup')\ndef startup(E):\n    pass\n").encode()}
    after = {'main.py': before['main.py'].replace(b'    pass', b'    app.add_exception_handler(E, invalid)')}
    assert evidence(before, after) == {INPUT: set()}

"""Stdlib-only trusted harness mounted read-only as /inputs/_unit_runner.py.

Import is inert. Only the explicit CLI runs discovery inside the Container.
The Host must not use this module to execute Agent-generated Source locally.
"""

import argparse
import ast
from contextlib import contextmanager, ExitStack, closing, redirect_stderr, redirect_stdout
import hashlib
import importlib
import inspect
import io
import logging
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import string
import sys
import tempfile
import textwrap
from types import ModuleType
import unittest  # Import stdlib before inserting any untrusted Source path.


MAX_CASES = 1000
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ID_BYTES = 512
MAX_DETAILS_BYTES = 4096
TEST_DATA_POLICY = "RUNTIME_SYNTHETIC_TEST_DATA_V2"
TEST_DATA_MODULE = "_host_test_data"
TEST_DATA_EXPORTS = ("new_password", "check_http_output")
MAX_TEST_PASSWORD_LENGTH = 4096
APP_FIXTURE_POLICY = 'SCN001_HOST_SIGNUP_OBSERVATIONS_V8'
APP_FIXTURE_MODULE = '_host_app_test'
APP_FIXTURE_EXPORTS = ('AppTestCase',)
SERVICE_FIXTURE_MODULE = '_host_service_test'
SERVICE_FIXTURE_EXPORTS = ('ServiceTestCase',)


def service_fixture_contract():
    return {'module': SERVICE_FIXTURE_MODULE, 'class': 'ServiceTestCase', 'inherits': 'unittest.TestCase',
        'attributes': ['client', 'app'], 'createsProductTables': False,
        'policy': 'HOST_GENERIC_SERVICE_LIFESPAN_V3',
        'instruction': 'Subclass ServiceTestCase and use self.client in test methods. Do not import main/database/models at module level, construct TestClient, or override setup/teardown/run. The Host enters the real main.app lifespan with an isolated DATABASE_URL before each test and cleans up afterwards. It never creates product tables or changes dependencies. Pure non-HTTP unit tests can remain ordinary TestCase.',
        'methods': {'check_http_output': {'parameters': ['method', 'path'], 'keywordOnly': ['sensitive'],
            'optionalKeywordOnly': ['client'], 'clientPolicy': 'Omit client; if supplied it must be this test self.client.',
            'returns': 'httpx.Response', 'example': 'self.check_http_output(method, path, sensitive=sample, json=payload)',
            'negativeInputPolicy': 'Empty input and sensitive=None execute the request but record NO_SAMPLE, never CHECKED. Nonempty short runtime samples, lost or truncated output remain INCOMPLETE; encoded hash exposure still fails. A current-test synthetic sample of 8+ characters in a request permits complete observations; this is an observation threshold, not a product password rule.',
            'instruction': 'This bound method already supplies self.client. The module-level _host_test_data.check_http_output takes client as its first argument. sensitive is the actual current-test new_password() string in the request; for empty/absent input use the empty sample or None. Never pass a field name, list or dict. Pass json/data/headers as request keywords.'}}}


@contextmanager
def _service_app_test_api(source_root):
    """Generic app lifetime only, executed with Source inside the Sandbox."""
    source = Path(source_root).resolve()
    def product_modules():
        return {name: module for name, module in tuple(sys.modules.items())
                if getattr(module, '__file__', None)
                and Path(module.__file__).resolve().is_relative_to(source)}
    class ServiceTestCase(unittest.TestCase):
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            if any(name in cls.__dict__ for name in ('setUp', 'tearDown', 'setUpClass', 'tearDownClass',
                    '_callSetUp', '_callTearDown', '_callCleanup', 'run')):
                raise ValueError('HOST_SERVICE_LIFECYCLE_OVERRIDE')

        def setUp(self):
            if any(name in sys.modules for name in ('main', 'database', 'models')):
                raise ValueError('HOST_SERVICE_IMPORT_BEFORE_FIXTURE')
            resources = ExitStack()
            self.addCleanup(resources.close)
            folder = resources.enter_context(tempfile.TemporaryDirectory(prefix='host-service-'))
            previous = os.environ.get('DATABASE_URL')
            def restore_environment():
                if previous is None: os.environ.pop('DATABASE_URL', None)
                else: os.environ['DATABASE_URL'] = previous
            resources.callback(restore_environment)
            os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(folder) / 'case.db')
            previous_modules = product_modules()
            for name in previous_modules: sys.modules.pop(name, None)
            def restore_modules():
                failed = False
                for name, module in product_modules().items():
                    try:
                        engine = getattr(module, 'engine', None)
                        if engine is not None and callable(getattr(engine, 'dispose', None)): engine.dispose()
                    except Exception: failed = True
                    finally: sys.modules.pop(name, None)
                sys.modules.update(previous_modules)
                importlib.invalidate_caches()
                if failed: raise ValueError('HOST_SERVICE_CLEANUP_FAILED')
            resources.callback(restore_modules)
            from fastapi.testclient import TestClient
            self.app = importlib.import_module('main').app
            self.client = resources.enter_context(TestClient(self.app, raise_server_exceptions=False))

        def check_http_output(self, method, path, *, sensitive, client=None, **request):
            # Accept the module helper's explicit-client spelling only when it
            # names the already-started client of this very test. Never let an
            # arbitrary client bypass the Host-owned lifespan.
            if client is not None and client is not self.client:
                raise ValueError('HOST_SERVICE_CLIENT_MISMATCH')
            return sys.modules[TEST_DATA_MODULE].check_http_output(
                self.client, method, path, sensitive=sensitive, **request)

    previous = sys.modules.get(SERVICE_FIXTURE_MODULE)
    module = ModuleType(SERVICE_FIXTURE_MODULE)
    module.__all__ = list(SERVICE_FIXTURE_EXPORTS)
    module.ServiceTestCase = ServiceTestCase
    sys.modules[SERVICE_FIXTURE_MODULE] = module
    try: yield
    finally:
        if previous is None: sys.modules.pop(SERVICE_FIXTURE_MODULE, None)
        else: sys.modules[SERVICE_FIXTURE_MODULE] = previous

# Public fixture shapes only. No hidden test cases, acceptance thresholds or verdicts.
APP_FIXTURE_ATTRIBUTES = ('client', 'db', 'User', 'app')
APP_FIXTURE_METHODS = {
    'concurrent_signup_statuses': {
        'parameters': ['first', 'second'], 'keywordOnly': [],
        'arguments': [{'name': 'first', 'type': 'json'}, {'name': 'second', 'type': 'json'}],
        'returns': {'type': 'tuple', 'items': ['int', 'int'], 'order': 'payload order'},
    },
    'signup_email_state': {
        'parameters': ['canonical_email'], 'keywordOnly': [],
        'arguments': [{'name': 'canonical_email', 'type': 'str'}],
        'returns': {'type': 'dict', 'properties': {
            'totalRows': 'int', 'exactEmailRows': 'int', 'hasDirectEmailUniqueKey': 'bool'}},
    },
    'signup_hash_parameters': {
        'parameters': ['canonical_email'], 'keywordOnly': ['password'],
        'arguments': [{'name': 'canonical_email', 'type': 'str'},
                      {'name': 'password', 'type': 'str', 'runtimeFactory': '_host_test_data.new_password'}],
        'returns': {'type': 'dict', 'properties': {
            'argon2id': 'bool', 'verified': 'bool', 'memoryKiB': 'int',
            'iterations': 'int', 'parallelism': 'int', 'distinctSalts': 'bool (after two accounts)'},
            'required': ['argon2id', 'verified', 'memoryKiB', 'iterations', 'parallelism'],
            'optional': {'distinctSalts': 'Present only in the current return after observing another distinct '
                         'committed account in this test. Earlier return dictionaries are snapshots and never update.'},
            'usage': 'Call signup_hash_parameters for two distinct committed accounts, passing each account\'s '
                     'current-test runtime sample through the password keyword argument. Save separate first '
                     'and second return snapshots, then assert second["distinctSalts"]. Never read '
                     'first["distinctSalts"]. Public numeric hash parameters are minimums.'},
    },
    'check_signup_output': {
        'parameters': ['payload'], 'keywordOnly': ['password'],
        'arguments': [{'name': 'payload', 'type': 'json', 'contains': 'the same synthetic password string'},
                      {'name': 'password', 'type': 'str', 'minimumLength': 8,
                       'runtimeFactory': '_host_test_data.new_password', 'createdInside': 'current test body'}],
        'returns': {'type': 'httpx.Response', 'records': 'value-free channel observations'},
    },
}


def app_fixture_contract():
    """Fresh JSON-shaped public API metadata; importing this runner executes no Source."""
    return {'module': APP_FIXTURE_MODULE, 'class': APP_FIXTURE_EXPORTS[0],
            'contract': 'SCN-001', 'policy': APP_FIXTURE_POLICY,
            'attributes': list(APP_FIXTURE_ATTRIBUTES),
            'methods': json.loads(json.dumps(APP_FIXTURE_METHODS)),
            'inherits': 'unittest.TestCase', 'createsProductTables': False,
            'testDataFactory': {'module': TEST_DATA_MODULE, 'function': 'new_password'},
            'notProvided': ['fresh_email', 'new_password'],
            'customHelpers': 'Define your own helpers explicitly; undeclared members are not supplied by the Host.'}


class _CaptureBytes(io.BytesIO):
    truncated = False

    def write(self, data):
        remaining = MAX_OUTPUT_BYTES - self.tell()
        self.truncated |= len(data) > remaining
        super().write(data[:remaining])
        return len(data)


class _BoundedOutput:
    def __init__(self):
        self.buffer = _CaptureBytes()

    def write(self, value):
        self.buffer.write(value.encode('utf-8', errors='replace'))
        return len(value)

    def text(self):
        return self.buffer.getvalue().decode('utf-8', errors='replace')

    def flush(self):
        pass


def _contains_password(payload, password):
    if isinstance(payload, dict):
        return any(_contains_password(value, password) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_password(value, password) for value in payload)
    return type(payload) is str and payload == password


class _RequestObservation:
    """Value-free metadata for a single TestClient request, alongside its ordinary verdict."""
    def __init__(self):
        self.started = False
        self.escaped = None
        self.parts = None

    def annotate(self, row, contains=None):
        if contains is None:
            if self.escaped and row['status'] == 'UNVERIFIED':
                row['diagnostic'] = self.escaped
        elif row['status'] == 'FAIL':
            locations = self.locations(contains)
            if locations:
                row['diagnostic'] = {'locations': locations}

    def locations(self, contains):
        if self.parts is None:
            return []
        body, headers = self.parts
        locations = []
        if contains(headers):
            locations.append('HEADERS')
        if not contains(body):
            return locations
        try:
            todo = [(json.loads(body), 'BODY', 0)]
        except (ValueError, RecursionError):
            todo = []
        for _ in range(512):
            if not todo or len(locations) >= 4:
                break
            value, path, depth = todo.pop()
            if isinstance(value, str) and contains(value):
                if path not in locations:
                    locations.append(path)
            elif depth < 8 and isinstance(value, (list, dict)):
                children = []
                if isinstance(value, list):
                    children = [(child, path + '[' + str(i) + ']', depth+1) for i, child in enumerate(value[:32])]
                else:
                    for key, child in value.items():
                        # Arbitrary keys can be supplied by users; export only public field names.
                        segment = key if key in ('detail','input','body','email','password','password_hash','message') else '*'
                        children.append((child, path + '.' + segment, depth+1))
                        if len(children) == 32:
                            break
                todo.extend(reversed(children))
        if not any(p.startswith('BODY') for p in locations):
            locations.append('BODY')
        return locations


def _escaped_exception(error, source, started):
    allowed = {'sqlalchemy.exc': {'OperationalError','IntegrityError','DBAPIError','DatabaseError',
                                  'StatementError','DataError','SQLAlchemyError','ProgrammingError'},
               'sqlite3': {'OperationalError','IntegrityError','DatabaseError'},
               'builtins': {'RuntimeError','ValueError','TypeError','Exception','KeyError'},
               'fastapi.exceptions': {'RequestValidationError','HTTPException'},
               'starlette.exceptions': {'HTTPException'}}
    cls = type(error)
    name = cls.__module__ + '.' + cls.__name__ if cls.__name__ in allowed.get(cls.__module__, ()) else 'OTHER'
    data = {'escapedToClient': True, 'responseStarted': started, 'exceptionType': name}
    tb = error.__traceback__
    frame = None
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        try:
            path = Path(filename).resolve()
            if path.suffix == '.py' and path.is_relative_to(source) and 1 <= tb.tb_lineno <= 1_000_000:
                frame = path, tb.tb_lineno
        except (ValueError, OSError, RuntimeError):
            pass
        tb = tb.tb_next
    if frame:
        path, line = frame
        try:
            if path.is_file() and path.stat().st_size <= 128000:
                data['appFrame'] = {'sourceSha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'line': line}
        except OSError:
            pass
    # A recognized argument diagnostic may name a builtin type. Do not stringify
    # a product exception or forward its argument names, values, or error text.
    if 'appFrame' in data and type(error) is TypeError and len(error.args) == 1:
        message = error.args[0]
        if type(message) is str and len(message) <= 192:
            match = re.fullmatch(
                r"'[a-zA-Z_][a-zA-Z_0-9]{0,63}' must be (?:a|an) [a-zA-Z_][a-zA-Z_0-9]{0,63} "
                r"\(got (str|bytes|bool|int|float|list|tuple|dict|set|NoneType)\)\.?", message)
            if match:
                data['observedCause'] = {'code': 'ARGUMENT_TYPE_MISMATCH', 'actualType': match[1]}
    return data


@contextmanager
def _observe_request(client, source):
    from unittest.mock import patch
    observation = _RequestObservation()
    transport = client._transport
    original_app, original_send = transport.app, client.send

    async def app(scope, receive, send):
        async def forward(message):
            await send(message)
            if message.get('type') == 'http.response.start':
                observation.started = True
        return await original_app(scope, receive, forward)

    def observe_send(*args, **kwargs):
        try:
            response = original_send(*args, **kwargs)
        except Exception as error:
            observation.escaped = _escaped_exception(error, source, observation.started)
            raise
        if response.is_stream_consumed:
            # A streamed or undecodable response can still be a valid app response.
            try:
                body = response.content[:MAX_OUTPUT_BYTES].decode('utf-8', errors='replace')
                headers = '\n'.join(key + ': ' + val for key, val in response.headers.items())
                observation.parts = (body, headers[:MAX_OUTPUT_BYTES])
            except (RuntimeError, ValueError, LookupError):
                pass
        return response

    with patch.object(transport, 'app', app), patch.object(client, 'send', observe_send):
        yield observation


@contextmanager
def _app_test_api(source_root, exposure_checks=None, issued_passwords=None, diagnostics=None, signup_checks=None):
    """Public SCN-001 lifecycle only. No schema creation, overrides or grading answers.

    The runner is mounted read-only. Application imports and these fixtures run
    only in the same disposable container as generated tests, never on the Host.
    """
    source = Path(source_root).resolve()

    def local_modules():
        found = {}
        for name, module in tuple(sys.modules.items()):
            filename = getattr(module, '__file__', None)
            if filename and Path(filename).resolve().is_relative_to(source):
                found[name] = module
        return found

    from weakref import WeakKeyDictionary
    salt_samples = WeakKeyDictionary()

    class AppTestCase(unittest.TestCase):
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            # Setup belongs to the Host. Custom preparation belongs in a test
            # body, after the app is started; ordinary unittest remains available.
            if any(name in cls.__dict__ for name in ('setUp', 'tearDown', 'setUpClass',
                    'tearDownClass', '_callSetUp', '_callTearDown', 'run')):
                raise ValueError('HOST_APP_LIFECYCLE_OVERRIDE')

        def setUp(self):
            if any(name in sys.modules for name in ('main', 'database', 'models')):
                raise ValueError('HOST_APP_IMPORT_BEFORE_FIXTURE')
            resources = ExitStack()
            self.addCleanup(resources.close)  # also runs when startup raises
            folder = resources.enter_context(tempfile.TemporaryDirectory(prefix='host-app-'))
            previous = os.environ.get('DATABASE_URL')
            os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(folder) / 'case.db')
            def restore_environment():
                if previous is None:
                    os.environ.pop('DATABASE_URL', None)
                else:
                    os.environ['DATABASE_URL'] = previous
            resources.callback(restore_environment)
            before = local_modules()
            def unload_application():
                for name, module in local_modules().items():
                    if name not in before:
                        engine = getattr(module, 'engine', None)
                        if engine is not None and callable(getattr(engine, 'dispose', None)):
                            engine.dispose()
                        sys.modules.pop(name, None)
                importlib.invalidate_caches()
            resources.callback(unload_application)
            # Import after DATABASE_URL is fixed, so the application's own
            # database.get_db and lifespan use the same isolated database.
            main = importlib.import_module('main')
            database = importlib.import_module('database')
            models = importlib.import_module('models')
            from fastapi.testclient import TestClient
            from sqlalchemy.orm import Session
            self.app = main.app
            self.client = resources.enter_context(TestClient(self.app, raise_server_exceptions=False))
            dependency = database.get_db()
            if inspect.isgenerator(dependency):
                resources.enter_context(closing(dependency))
                self.db = next(dependency)
            else:
                self.db = resources.enter_context(dependency) if isinstance(dependency, Session) else dependency
            if not isinstance(self.db, Session):
                raise ValueError('HOST_APP_DATABASE_SESSION_REQUIRED')
            self.User = models.User
            url = self.db.get_bind(mapper=self.User).url
            if (url.get_backend_name() != 'sqlite'
                    or Path(url.database).resolve() != (Path(folder) / 'case.db').resolve()):
                raise ValueError('HOST_APP_DATABASE_URL_IGNORED')

        def concurrent_signup_statuses(self, first, second):
            """Release two test-authored HTTP requests together. Preserve both real status codes."""
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier

            start = Barrier(2, timeout=5)
            def send(payload):
                start.wait()
                return self.client.post('/api/auth/signup', json=payload, follow_redirects=False).status_code

            addresses = [p.get('email') for p in (first, second) if isinstance(p, dict)]
            canonical = (addresses[0].strip().lower() if len(addresses) == 2 and
                         all(isinstance(a, str) for a in addresses) and
                         addresses[0].strip().lower() == addresses[1].strip().lower() else None)
            valid_pair = (canonical and issued_passwords is not None
                          and all(isinstance(p.get('password'), str) and len(p['password']) >= 8
                                  and p['password'] in issued_passwords for p in (first, second)))
            before = self._signup_email_state(canonical) if signup_checks is not None and valid_pair else None
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = [pool.submit(send, payload) for payload in (first, second)]
                statuses = tuple(response.result() for response in responses)
            if before and before['exactEmailRows'] == 0:
                after = self._signup_email_state(canonical)
                added = after['totalRows'] - before['totalRows']
                created = sum(200 <= status < 300 for status in statuses)
                if (created == 1 and added == 1 and after['exactEmailRows'] == 1
                        and any(400 <= status < 500 for status in statuses)):
                    signup_checks.setdefault(self, {}).setdefault('concurrentCanonicalSignup', True)
                elif created or added or after['exactEmailRows']:
                    # A success without its committed canonical row, or a write after two rejections, is an observed violation.
                    signup_checks.setdefault(self, {})['concurrentCanonicalSignup'] = False
            return statuses

        def signup_email_state(self, canonical_email):
            """Read committed counts and deployed SQLite keys. Never select passwords or create schema."""
            result = self._signup_email_state(canonical_email)
            if (signup_checks is not None and canonical_email == canonical_email.strip().lower()
                    and result['exactEmailRows'] == 1 and result['hasDirectEmailUniqueKey']):
                signup_checks.setdefault(self, {})['canonicalEmailUniqueKey'] = True
            return result

        def _signup_email_state(self, canonical_email):
            from sqlalchemy import func, inspect as sa_inspect, select

            if not isinstance(canonical_email, str) or not canonical_email:
                raise ValueError('SUPPLY_EXPECTED_CANONICAL_EMAIL')
            column = sa_inspect(self.User).column_attrs['email'].columns[0]
            table = column.table
            with self.db.get_bind(mapper=self.User).connect() as connection:
                inspector = sa_inspect(connection)
                coordinates = {'table_name': table.name, 'schema': table.schema}
                pk = inspector.get_pk_constraint(**coordinates)
                unique = inspector.get_unique_constraints(**coordinates)
                indexes = inspector.get_indexes(**coordinates)
                key = [column.name]
                direct_key = (pk.get('constrained_columns') == key
                              or any(item.get('column_names') == key for item in unique)
                              or any(item.get('unique') and item.get('column_names') == key
                                     and item.get('dialect_options', {}).get('sqlite_where') is None
                                     for item in indexes))
                # One statement and a binary comparison even for a NOCASE column.
                count = select(func.count(), func.count().filter(column.collate('BINARY') == canonical_email)).select_from(table)
                total, exact = connection.execute(count).one()
            return {'totalRows': total, 'exactEmailRows': exact, 'hasDirectEmailUniqueKey': direct_key}

        def signup_hash_parameters(self, canonical_email, *, password):
            """Observe one committed credential. Return only non-secret metadata for test assertions."""
            from argon2 import PasswordHasher, extract_parameters
            from argon2.exceptions import InvalidHashError, VerificationError
            from sqlalchemy import select

            if (signup_checks is None or issued_passwords is None or password not in issued_passwords
                    or not isinstance(canonical_email, str) or not canonical_email):
                raise ValueError('SUPPLY_SYNTHETIC_SIGNUP_ACCOUNT')
            query = select(self.User.password_hash).where(self.User.email == canonical_email)
            with self.db.get_bind(mapper=self.User).connect() as connection:
                encoded = connection.execute(query).scalar_one_or_none()
            if encoded is None:
                self.skipTest('COMMITTED_CREDENTIAL_UNAVAILABLE')
            facts = {'argon2id': False, 'verified': False, 'memoryKiB': 0, 'iterations': 0, 'parallelism': 0}
            if isinstance(encoded, str):
                try:
                    params = extract_parameters(encoded)
                    facts.update(argon2id=params.type.name == 'ID', memoryKiB=params.memory_cost,
                                 iterations=params.time_cost, parallelism=params.parallelism)
                    facts['verified'] = PasswordHasher().verify(encoded, password)
                except (InvalidHashError, VerificationError, ValueError, TypeError):
                    pass
            # All observed credentials in this case must meet the public policy. Never export the hashes.
            marks = signup_checks.setdefault(self, {})
            prior = marks.get('storedHashParameters', facts)
            observed = {key: min(value, prior[key]) for key, value in facts.items()}
            if 'distinctSalts' in prior:
                observed['distinctSalts'] = prior['distinctSalts']
            if facts['verified']:
                salt = encoded.split('$')[-2]
                samples = salt_samples.setdefault(self, {})
                other_salts = [value for account, value in samples.items() if account != canonical_email]
                if other_salts:
                    observed['distinctSalts'] = observed.get('distinctSalts', True) and salt not in other_salts
                if len(samples) < 32 or canonical_email in samples:
                    samples[canonical_email] = salt
            marks['storedHashParameters'] = observed
            return {**facts, **({'distinctSalts': observed['distinctSalts']} if 'distinctSalts' in observed else {})}

        def check_signup_output(self, payload, *, password):
            """Measure the output of a test-authored signup request. Never export payload or output."""
            if exposure_checks is None:
                raise ValueError('GENERATED_SECURITY_OBSERVATION_UNAVAILABLE')
            if (type(password) is not str or len(password) < 8 or
                    not _contains_password(payload, password) or issued_passwords is None or password not in issued_passwords
                    or isinstance(payload, dict) and 'password' in payload and payload['password'] != password):
                raise ValueError('SUPPLY_SYNTHETIC_PASSWORD_PRESENT_IN_REQUEST')
            import fastapi.routing as routing
            from sqlalchemy import event
            from sqlalchemy.exc import IntegrityError
            engine = self.db.get_bind(mapper=self.User)
            known_hashes = set()
            def before_insert(_mapper, _connection, user):
                value = getattr(user, 'password_hash', None)
                if isinstance(value, str) and value and len(known_hashes) < 32:
                    known_hashes.add(value)
            storage_error = entered = matched = False
            original_endpoint = routing.run_endpoint_function
            async def observe_entry(*args, **kwargs):
                nonlocal entered
                entered = True
                return await original_endpoint(*args, **kwargs)

            transport = self.client._transport
            original_app = transport.app
            async def observe_route(scope, receive, send):
                nonlocal matched
                try:
                    return await original_app(scope, receive, send)
                finally:
                    matched = scope.get('route') is not None and callable(scope.get('endpoint'))
            def on_error(context):
                nonlocal storage_error
                if not isinstance(context.sqlalchemy_exception, IntegrityError):
                    storage_error = True

            # Keep only bounded text in memory, match after the request, then discard it.
            # Forward no production data to the Host, assertions or model.
            stream = _BoundedOutput()
            formatted = logging.StreamHandler.format
            def observe_format(handler, record):
                result = formatted(handler, record)
                stream.write(result)
                return result
            from unittest.mock import patch
            with ExitStack() as resources:
                event.listen(self.User, 'before_insert', before_insert)
                resources.callback(event.remove, self.User, 'before_insert', before_insert)
                event.listen(engine, 'handle_error', on_error)
                resources.callback(event.remove, engine, 'handle_error', on_error)
                resources.enter_context(patch.object(logging.StreamHandler, 'format', observe_format))
                resources.enter_context(redirect_stdout(stream))
                resources.enter_context(redirect_stderr(stream))
                # A synthetic 500 produced by TestClient for a propagated exception is not an app response.
                resources.enter_context(patch.object(transport, 'raise_server_exceptions', True))
                resources.enter_context(patch.object(transport, 'app', observe_route))
                resources.enter_context(patch.object(routing, 'run_endpoint_function', observe_entry))
                observation = resources.enter_context(_observe_request(self.client, source))
                try:
                    response = self.client.post('/api/auth/signup', json=payload, follow_redirects=False)
                except Exception:
                    response = None
            status_class = response.status_code // 100 if response is not None else None
            channel = None
            if matched and entered and status_class == 2:
                channel = 'SUCCESS_RESPONSE'
            elif matched and not entered and status_class == 4:
                channel = 'REJECTION_RESPONSE'
            if storage_error and entered and status_class in (None, 5):
                channel = 'STORAGE_ERROR_RESPONSE'
            marks = exposure_checks.setdefault(self, {})
            def contains(content):
                # Match the actual supplied value (including JSON escapes) and the public encoded hash format.
                tokens = (password, json.dumps(password, ensure_ascii=True)[1:-1],
                          json.dumps(password, ensure_ascii=False)[1:-1])
                return any(token in content for token in (*tokens, *known_hashes)) or bool(re.search(
                    r'\$argon2(?:id|i|d)\$(?:v=\d+\$)?m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+', content))
            def record(name, content, truncated=False):
                leaked = contains(content)
                state = 'EXPOSED' if leaked else 'INCOMPLETE' if truncated else 'CHECKED'
                ranks = {'CHECKED': 0, 'INCOMPLETE': 1, 'EXPOSED': 2}
                if name not in marks or ranks[state] > ranks[marks[name]]:
                    marks[name] = state
                return leaked
            content = response.content if response is not None else b''
            headers = b'\n'.join(key + b': ' + value for key, value in response.headers.raw) if response is not None else b''
            content = headers + b'\n' + content
            body = content[:MAX_OUTPUT_BYTES].decode('utf-8', errors='replace')
            exposed = record(channel or 'OTHER_RESPONSE', body, response is None or len(content) > MAX_OUTPUT_BYTES)
            # A request log capture includes Python stream output and formatted stream-handler records.
            exposed_logs = record('STORAGE_ERROR_REQUEST_LOGS' if channel == 'STORAGE_ERROR_RESPONSE'
                                  else 'REQUEST_LOGS', stream.text(), stream.buffer.truncated)
            if diagnostics is not None:
                detail = observation.escaped
                if exposed and observation.parts:
                    locations = observation.locations(contains)
                    detail = {'locations': locations} if locations else None
                if detail:
                    bucket = diagnostics.setdefault(self, {}).setdefault(channel or 'OTHER_RESPONSE', [])
                    if detail not in bucket and len(bucket) < 4:
                        bucket.append(detail)
            if exposed or exposed_logs:
                self.fail('REQUEST_OUTPUT_EXPOSURE')
            if response is None:
                self.skipTest('REQUEST_RESPONSE_UNAVAILABLE')
            return response

    module = ModuleType(APP_FIXTURE_MODULE)
    module.__all__ = list(APP_FIXTURE_EXPORTS)
    module.AppTestCase = AppTestCase
    module.observe_request = lambda client: _observe_request(client, source)
    previous = sys.modules.get(APP_FIXTURE_MODULE)
    sys.modules[APP_FIXTURE_MODULE] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(APP_FIXTURE_MODULE, None)
        else:
            sys.modules[APP_FIXTURE_MODULE] = previous


def _new_test_password(length=12, kind="ascii"):
    """Fresh synthetic data only: no supplied literal, env, file or network input.

    Length and character family describe data, not whether the product should
    accept it. Empty, short and long cases use the very same runtime factory.
    """
    alphabets = {"ascii": string.ascii_letters + string.digits,
                 "unicode": "가나다라마éñΩλ", "digits": string.digits, "spaces": " "}
    if (type(length) is not int or not 0 <= length <= MAX_TEST_PASSWORD_LENGTH
            or type(kind) is not str or kind not in alphabets):
        raise ValueError("TEST_DATA_ARGUMENT_INVALID")
    return "".join(secrets.choice(alphabets[kind]) for _ in range(length))


@contextmanager
def _test_data_api(issued_passwords=None, exposure_checks=None, state=None):
    # Preload before untrusted source/tests join sys.path: an accidentally
    # same-named product/test file cannot shadow the Host-owned factory.
    # As with unittest itself, this is not isolation from malicious Python
    # deliberately mutating sys.modules inside its disposable container.
    missing = object()
    previous = sys.modules.get(TEST_DATA_MODULE, missing)
    module = ModuleType(TEST_DATA_MODULE)
    module.__all__ = list(TEST_DATA_EXPORTS)
    def new_password(length=12, kind='ascii'):
        value = _new_test_password(length, kind)
        if issued_passwords is not None and len(value) >= 8:
            issued_passwords.add(value)
        if state is not None and state.get('test') is not None:
            state.setdefault('httpTestSamples', set()).add(value)
        return value
    module.new_password = new_password
    def check_http_output(client, method, path, *, sensitive, **request):
        """Observe a test-authored HTTP request's response AND bounded request logs.

        This generic helper knows no app model, route, accepted status or DB
        schema. Only channel marks cross the container boundary. It does not
        claim that a stored hash, authorization policy or product is correct.
        """
        current = state.get('test') if state is not None else None
        if current is None or exposure_checks is None:
            raise ValueError('GENERATED_SECURITY_OBSERVATION_UNAVAILABLE')
        no_secret = sensitive is None or type(sensitive) is str and sensitive == ''
        current_sample = (type(sensitive) is str and state is not None
                          and sensitive in state.get('httpTestSamples', ())
                          and _contains_password(request, sensitive))
        qualified = (type(sensitive) is str and len(sensitive) >= 8 and issued_passwords is not None
                     and sensitive in issued_passwords and _contains_password(request, sensitive))
        if (not (no_secret or current_sample or qualified)
                or method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
                or type(path) is not str or not path.startswith('/') or path.startswith('//')):
            raise ValueError('SUPPLY_CURRENT_SYNTHETIC_SECRET_PRESENT_IN_REQUEST')
        request_method = getattr(client, 'request', None)
        if not callable(request_method):
            raise ValueError('HOST_HTTP_REQUEST_ARGUMENT_INVALID')
        try:
            signature = inspect.signature(request_method)
        except (TypeError, ValueError):
            signature = None  # An opaque client remains authoritative at invocation.
        if signature is not None:
            try:
                signature.bind(method, path, **request)
            except TypeError:
                raise ValueError('HOST_HTTP_REQUEST_ARGUMENT_INVALID') from None
        stream, response = _BoundedOutput(), None
        formatted = logging.StreamHandler.format
        def observe_format(handler, record):
            text = formatted(handler, record)
            stream.write(text)
            return text
        def record(channel, content, incomplete=False):
            text = content.decode('utf-8', errors='replace') if isinstance(content, bytes) else content
            escaped = json.dumps(sensitive, ensure_ascii=True)[1:-1] if qualified else None
            exposed = (qualified and (sensitive in text or escaped in text)) or re.search(r'\$(?:argon2(?:id|i|d)\$|2[aby]\$)[^\s"\x27<>]{12,}', text) is not None
            outcome = ('EXPOSED' if exposed else 'INCOMPLETE' if incomplete or not (qualified or no_secret)
                       else 'CHECKED' if qualified else 'NO_SAMPLE')
            marks = exposure_checks.setdefault(current, {})
            rank = {'NO_SAMPLE': 0, 'CHECKED': 1, 'INCOMPLETE': 2, 'EXPOSED': 3}
            if rank[outcome] >= rank.get(marks.get(channel), -1):
                marks[channel] = outcome
            return exposed
        from unittest.mock import patch
        exposed = False
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(logging.StreamHandler, 'format', observe_format))
                stack.enter_context(redirect_stdout(stream))
                stack.enter_context(redirect_stderr(stream))
                response = client.request(method, path, **request)
        finally:
            exposed |= record('REQUEST_LOGS', stream.text(), stream.buffer.truncated)
            content = response.content if response is not None else b''
            headers = b'\n'.join(key + b': ' + value for key, value in response.headers.raw) if response is not None else b''
            exposed |= record('OTHER_RESPONSE', content[:MAX_OUTPUT_BYTES] + headers[:MAX_OUTPUT_BYTES],
                response is None or len(content) + len(headers) > MAX_OUTPUT_BYTES)
            if exposed:
                raise AssertionError('REQUEST_OUTPUT_EXPOSURE')
        return response
    module.check_http_output = check_http_output
    sys.modules[TEST_DATA_MODULE] = module
    try:
        yield
    finally:
        if previous is missing:
            sys.modules.pop(TEST_DATA_MODULE, None)
        else:
            sys.modules[TEST_DATA_MODULE] = previous

_SAFE_PATTERN = re.compile(r"[A-Za-z0-9_*?\[\].-]{1,128}\.py\Z")
_SECRET_NAMES = frozenset({
    ".git", ".ssh", ".aws", ".codex", ".agents", ".gnupg", ".kube",
    ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "secrets", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "docker.sock", ".workspace.json",
})


def _safe_directory(value):
    # Mirror the Host's tests-relative lexical policy without importing the
    # application or inspecting any Host path in this standalone harness.
    if (type(value) is not str or not value or value.startswith("/") or "\\" in value
            or PureWindowsPath(value).drive or len(value.encode("utf-8")) > 4096
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        return False
    parts = value.split("/")
    return parts[0] == "tests" and len(parts) <= 64 and all(
        part not in {"", ".", ".."} and ":" not in part
        and part.casefold() not in _SECRET_NAMES
        and not part.casefold().startswith((".env", ".mcp-write-"))
        and not part.casefold().endswith((".pem", ".key", ".p12", ".pfx"))
        for part in parts
    )


class _HarnessError(Exception):
    """Stable internal marker; never returned with exception text or paths."""


class _DiscardStream(io.TextIOBase):
    def __init__(self, budget):
        self._budget = budget
        self.buffer = _DiscardBytes(budget)

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError("text required")
        self.buffer.write(value.encode("utf-8", errors="replace"))
        return len(value)


class _DiscardBytes:
    def __init__(self, budget):
        self._budget = budget

    def write(self, value):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("bytes required")
        self._budget[0] += len(value)
        if self._budget[0] > MAX_OUTPUT_BYTES:
            self._budget[1] = True
            raise _HarnessError()
        return len(value)

    def flush(self):
        return None


def _bounded(value, limit):
    return value.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


def _self_call(node, name):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == name and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'self')


def _fixture_result_accesses(method, members):
    """Direct, unmodified Host dict results only; do not follow aliases or dynamic flow."""
    if any(not isinstance(stmt, (ast.Assign, ast.Expr)) for stmt in method.body):
        return []
    parents = {id(child): parent for parent in ast.walk(method) for child in ast.iter_child_nodes(parent)}
    hash_calls = [n for stmt in method.body for n in ast.walk(stmt)
                  if _self_call(n, 'signup_hash_parameters')]
    accesses = []
    for assignment in method.body:
        if (not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1
                or not isinstance(assignment.targets[0], ast.Name)):
            continue
        helper = next((name for name in ('signup_email_state', 'signup_hash_parameters')
                       if name not in members and _self_call(assignment.value, name)), None)
        if helper is None:
            continue
        name = assignment.targets[0].id
        names = [n for n in ast.walk(method) if isinstance(n, ast.Name) and n.id == name]
        if sum(isinstance(n.ctx, ast.Store) for n in names) != 1:
            continue
        uses = [n for n in names if isinstance(n.ctx, ast.Load)]
        # Escaping, deleting, mutating, or passing the dict to arbitrary code invalidates the proof.
        if any(not isinstance(parents.get(id(n)), ast.Subscript)
               or parents[id(n)].value is not n or not isinstance(parents[id(n)].ctx, ast.Load)
               for n in uses) or any(isinstance(n.ctx, ast.Del) for n in names):
            continue
        for use in uses:
            subscript = parents[id(use)]
            if (subscript.lineno <= assignment.lineno or not isinstance(subscript.slice, ast.Constant)
                    or not isinstance(subscript.slice.value, str)):
                continue
            accesses.append({'member': helper, 'assignment': assignment, 'subscript': subscript,
                             'sourceLine': subscript.lineno,
                             'firstHashResult': helper == 'signup_hash_parameters' and bool(hash_calls)
                                 and assignment.value is hash_calls[0]})
    return accesses


def _method_fixture_issues(method, members):
    """Prove only known API misuse; never infer a product verdict from Source."""
    if method.decorator_list or not method.args.args or method.args.args[0].arg != 'self':
        return []

    def nodes(root):
        yield root
        for child in ast.iter_child_nodes(root):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                yield from nodes(child)

    local = list(nodes(method))
    # Dynamic or user-defined helpers remain authoritative. Do not guess whether a branch ran.
    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id in ('setattr', 'exec') for n in local):
        return []
    assigned = {n.attr for n in local if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                and isinstance(n.value, ast.Name) and n.value.id == 'self'}
    issues = []
    for node in local:
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id == 'self'
                and node.attr in ('fresh_email', 'new_password') and node.attr not in members | assigned):
            issues.append({'reasonCode': 'TEST_HOST_MEMBER_MISSING', 'sourceLine': node.lineno,
                           'member': node.attr})
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in APP_FIXTURE_METHODS
                and node.func.attr not in members and _self_call(node, node.func.attr)):
            spec = APP_FIXTURE_METHODS[node.func.attr]
            positional, keyword_only = spec['parameters'], spec['keywordOnly']
            types = {a['name']: a['type'] for a in spec['arguments']}
            unpacked = any(isinstance(arg, ast.Starred) for arg in node.args) or any(k.arg is None for k in node.keywords)
            names = [*positional[:len(node.args)], *(k.arg for k in node.keywords if k.arg is not None)]
            invalid = not unpacked and (len(node.args) > len(positional) or len(names) != len(set(names))
                or set(names) != set(positional) | set(keyword_only))
            values = [*zip(positional, node.args), *((k.arg, k.value) for k in node.keywords)]
            for parameter, value in values:
                if types.get(parameter) != 'str':
                    continue
                if (isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict))
                        or isinstance(value, ast.Constant) and not isinstance(value.value, str)):
                    invalid = True
            if invalid:
                issues.append({'reasonCode': 'TEST_HOST_ARGUMENT_INVALID', 'sourceLine': node.lineno,
                               'member': node.func.attr})
    # A direct helper assignment followed by a read of an impossible result key.
    # Do not infer through intervening calls, mutation, aliases or unknown overrides.
    for assignment, statement in zip(method.body, method.body[1:]):
        if (not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1
                or not isinstance(assignment.targets[0], ast.Name) or not isinstance(assignment.value, ast.Call)):
            continue
        helper = next((name for name in ('signup_email_state', 'signup_hash_parameters')
                       if name not in members and _self_call(assignment.value, name)), None)
        if helper is None:
            continue
        if any(isinstance(n, ast.Call) and not any(
                _self_call(n, name) and name not in members for name in
                ('assertEqual', 'assertTrue', 'assertFalse', 'assertGreater', 'assertGreaterEqual', 'assertLess', 'assertLessEqual'))
               for n in ast.walk(statement)):
            continue
        properties = APP_FIXTURE_METHODS[helper]['returns']['properties']
        for node in ast.walk(statement):
            if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name) and node.value.id == assignment.targets[0].id
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)
                    and node.slice.value not in properties):
                issues.append({'reasonCode': 'TEST_HOST_RESULT_KEY_UNKNOWN', 'sourceLine': node.lineno,
                               'member': helper, 'assignment': assignment, 'subscript': node})
    # A first hash observation cannot compare two accounts. Later calls do not mutate its return.
    # Unknown custom helpers can establish earlier observations; leave those methods to runtime.
    known_calls = all(not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute)
                      or not isinstance(n.func.value, ast.Name) or n.func.value.id != 'self'
                      or n.func.attr in APP_FIXTURE_METHODS or n.func.attr.startswith('assert') for n in local)
    if known_calls:
        for item in _fixture_result_accesses(method, members):
            if item['firstHashResult'] and item['subscript'].slice.value == 'distinctSalts':
                issues.append({**item, 'reasonCode': 'TEST_HOST_OPTIONAL_RESULT_UNAVAILABLE'})
    # Intentionally narrow: adjacent, straight-line unpack + numeric unittest assertion.
    # Iterating dictionary keys intentionally is valid, as are custom helper return types.
    for assignment, assertion in zip(method.body, method.body[1:]):
        if (not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1
                or not isinstance(assertion, ast.Expr)):
            continue
        unpack, check = assignment.targets[0], assertion.value
        if (_self_call(assignment.value, 'concurrent_signup_statuses')
                and 'concurrent_signup_statuses' not in members and 'assertIn' not in members
                and isinstance(unpack, (ast.Tuple, ast.List)) and len(unpack.elts) == 2
                and all(isinstance(var, ast.Name) for var in unpack.elts)
                and _self_call(check, 'assertIn') and len(check.args) == 2 and not check.keywords
                and isinstance(check.args[1], ast.Name) and check.args[1].id in {var.id for var in unpack.elts}):
            issues.append({'reasonCode': 'TEST_HOST_RETURN_MISUSED', 'sourceLine': assertion.lineno,
                           'member': 'concurrent_signup_statuses'})
        if not _self_call(assignment.value, 'signup_email_state') or 'signup_email_state' in members:
            continue
        if (not isinstance(unpack, (ast.Tuple, ast.List)) or len(unpack.elts) != 3
                or not all(isinstance(var, ast.Name) for var in unpack.elts)
                or len({var.id for var in unpack.elts}) != 3
                or not isinstance(check, ast.Call) or len(check.args) < 2
                or not isinstance(check.func, ast.Attribute)
                or check.func.attr not in ('assertEqual', 'assertGreater', 'assertGreaterEqual',
                                           'assertLess', 'assertLessEqual')
                or check.func.attr in members or not _self_call(check, check.func.attr)):
            continue
        if any(isinstance(var, ast.Name) and var.id in {n.id for n in unpack.elts}
               and isinstance(number, ast.Constant) and type(number.value) in (int, float, bool)
               for var, number in (check.args[:2], check.args[:2][::-1])):
            issues.append({'reasonCode': 'TEST_HOST_RETURN_MISUSED', 'sourceLine': assertion.lineno,
                           'member': 'signup_email_state', 'assignment': assignment,
                           'assertion': check})
    return issues


def app_fixture_source_issues(source):
    """AST only; unknown inheritance, rebinding and dynamic members get no speculative finding."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return []
    aliases = {alias.asname or alias.name for node in tree.body
               if isinstance(node, ast.ImportFrom) and node.module == APP_FIXTURE_MODULE and not node.level
               for alias in node.names if alias.name == 'AppTestCase'}
    if not aliases:
        return []
    # Anything binding the imported name a second time makes its identity uncertain.
    bindings = [n.id for node in tree.body for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
    other_bindings = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    imports = [alias.asname or alias.name.split('.')[0] for n in tree.body
               if isinstance(n, (ast.Import, ast.ImportFrom)) for alias in n.names]
    if any(name in bindings or name in other_bindings or imports.count(name) != 1 for name in aliases):
        return []
    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ('setattr', 'exec')
           for n in ast.walk(tree)):
        return []
    class_names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    if any(isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
           and isinstance(n.value, ast.Name) and n.value.id in aliases | class_names for n in ast.walk(tree)):
        return []
    classes, issues = {}, []
    for case in tree.body:
        if (not isinstance(case, ast.ClassDef) or case.decorator_list or case.keywords or len(case.bases) != 1
                or not isinstance(case.bases[0], ast.Name) or case.name in bindings):
            continue
        base = case.bases[0].id
        if base not in aliases and base not in classes:
            continue
        members = set(classes.get(base, ()))
        members.update(n.name for n in case.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
        members.update(n.id for node in case.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                       for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        members.update(n.attr for n in ast.walk(case) if isinstance(n, ast.Attribute)
                       and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) and n.value.id == 'self')
        if members & {'__getattr__', '__getattribute__', '__init_subclass__'}:
            continue
        classes[case.name] = members
        for method in case.body:
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                issues.extend({k: v for k, v in item.items() if k in ('reasonCode', 'sourceLine', 'member')}
                              for item in _method_fixture_issues(method, members))
    return issues[:32]


def _http_helper_usage_reason(test, err):
    """Only proven Host-helper argument failures, never response/app failures."""
    if err[0] not in (TypeError, ValueError):
        return None
    helper = getattr(sys.modules.get(TEST_DATA_MODULE), 'check_http_output', None)
    service = getattr(sys.modules.get(SERVICE_FIXTURE_MODULE), 'ServiceTestCase', None)
    try:
        method = inspect.getattr_static(type(test), test._testMethodName)
        frame = err[2]
        while frame and frame.tb_frame.f_code is not method.__code__:
            frame = frame.tb_next
        if frame is None:
            return None
        terminal = frame
        while terminal.tb_next:
            terminal = terminal.tb_next
        original = service.check_http_output if service is not None and isinstance(test, service) else None
        bound_is_original = original is not None and inspect.getattr_static(test, 'check_http_output') is original
        if err[0] is ValueError:
            if (helper is not None and terminal.tb_frame.f_code is helper.__code__
                    and err[1].args in (('SUPPLY_CURRENT_SYNTHETIC_SECRET_PRESENT_IN_REQUEST',),
                                       ('HOST_HTTP_REQUEST_ARGUMENT_INVALID',))):
                return 'NOT_RUN_FIXTURE_ARGUMENT_INVALID'
            if (bound_is_original and terminal.tb_frame.f_code is original.__code__
                    and terminal.tb_frame.f_locals.get('self') is test
                    and err[1].args == ('HOST_SERVICE_CLIENT_MISMATCH',)):
                return 'NOT_RUN_FIXTURE_ARGUMENT_INVALID'
            return None
        if terminal is not frame:
            return None  # Product/request/library TypeErrors remain measured failures.
        # Python rejects a bad signature before entering the helper. Prove the
        # exact CALL position and actual callee identity, then bind placeholders
        # to its signature. Never evaluate expressions or inspect input values.
        import dis
        position = next((i.positions for i in dis.get_instructions(method)
                         if i.offset == frame.tb_lasti), None)
        if position is None:
            return None
        lines, first = inspect.getsourcelines(method)
        indentation = len(lines[0]) - len(lines[0].lstrip())
        tree = ast.parse(textwrap.dedent(''.join(lines)))
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Call) or node.lineno + first - 1 != position.lineno
                    or node.col_offset + indentation != position.col_offset
                    or any(k.arg is None for k in node.keywords)
                    or any(isinstance(arg, ast.Starred) for arg in node.args)):
                continue
            function, leading = None, ()
            if (bound_is_original and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'check_http_output' and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == 'self'):
                function, leading = original, (test,)
            elif isinstance(node.func, ast.Name):
                actual = frame.tb_frame.f_locals.get(node.func.id, frame.tb_frame.f_globals.get(node.func.id))
                if actual is helper:
                    function = helper
            if function is not None:
                try:
                    inspect.signature(function).bind(*leading, *(object() for _ in node.args),
                        **{k.arg: object() for k in node.keywords})
                except TypeError:
                    return 'NOT_RUN_FIXTURE_ARGUMENT_INVALID'
    except (AttributeError, OSError, TypeError, ValueError, SyntaxError, RecursionError):
        pass
    return None


def _fixture_usage_reason(test, err):
    """Bind a closed repair code to the actual failing frame and the unchanged Host API."""
    # A bad generic-helper argument is a test preparation error, not a product
    # defect. Bind to the actual Host function frame; an app/test raising the
    # same words elsewhere cannot acquire this not-run classification.
    http_reason = _http_helper_usage_reason(test, err)
    if http_reason is not None:
        return http_reason
    fixture = getattr(sys.modules.get(APP_FIXTURE_MODULE), 'AppTestCase', None)
    if fixture is None or not isinstance(test, fixture) or err[0] not in (AttributeError, AssertionError, TypeError, ValueError, KeyError):
        return None
    try:
        if (inspect.getattr_static(type(test), '__getattribute__') is not object.__getattribute__
                or any('__getattr__' in vars(cls) for cls in type(test).__mro__)):
            return None
        method = inspect.getattr_static(type(test), test._testMethodName)
        frame = err[2]
        while frame and frame.tb_frame.f_code is not method.__code__:
            frame = frame.tb_next
        if frame is None:
            return None
        terminal = frame
        while terminal.tb_next:
            terminal = terminal.tb_next
        for name, marker in (('check_signup_output', 'SUPPLY_SYNTHETIC_PASSWORD_PRESENT_IN_REQUEST'),
                             ('signup_hash_parameters', 'SUPPLY_SYNTHETIC_SIGNUP_ACCOUNT')):
            original = getattr(fixture, name)
            if (inspect.getattr_static(test, name) is original and terminal.tb_frame.f_code is original.__code__
                    and terminal.tb_frame.f_locals.get('self') is test
                    and (err[0] is ValueError and err[1].args == (marker,)
                         or err[0] is TypeError and name == 'signup_hash_parameters'
                         and type(terminal.tb_frame.f_locals.get('password')) is not str)):
                return 'NOT_RUN_FIXTURE_ARGUMENT_INVALID'
        lines, first = inspect.getsourcelines(method)
        node = ast.parse(textwrap.dedent(''.join(lines))).body[0]
        members = set(vars(test)) | {key for cls in type(test).__mro__ if cls not in fixture.__mro__ for key in vars(cls)}
        candidates = _method_fixture_issues(node, members)
        candidates += [{**item, 'reasonCode': 'TEST_HOST_OPTIONAL_RESULT_UNAVAILABLE'}
                       for item in _fixture_result_accesses(node, members)
                       if item['subscript'].slice.value in
                       APP_FIXTURE_METHODS[item['member']]['returns'].get('optional', {})]
        for item in candidates:
            if frame.tb_lineno != first + item['sourceLine'] - 1:
                continue
            if item['reasonCode'] == 'TEST_HOST_MEMBER_MISSING':
                if (err[0] is AttributeError and getattr(err[1], 'obj', None) is test
                        and getattr(err[1], 'name', None) == item['member'] and frame.tb_next is None):
                    return 'NOT_RUN_FIXTURE_MEMBER_MISSING'
                continue
            if item['reasonCode'] in ('TEST_HOST_RESULT_KEY_UNKNOWN', 'TEST_HOST_OPTIONAL_RESULT_UNAVAILABLE'):
                helper = item['member']
                owner = frame.tb_frame.f_locals.get(item['assignment'].targets[0].id)
                properties = APP_FIXTURE_METHODS[helper]['returns']['properties']
                if (err[0] is KeyError and err[1].args == (item['subscript'].slice.value,)
                        and frame.tb_next is None and type(owner) is dict and set(owner) <= set(properties)
                        and item['subscript'].slice.value not in owner
                        and inspect.getattr_static(test, helper) is getattr(fixture, helper)):
                    return 'NOT_RUN_FIXTURE_RETURN_MISUSED'
                continue
            if item['member'] != 'signup_email_state':
                continue  # the existing integer-container runtime classifier handles concurrency
            if (inspect.getattr_static(test, 'signup_email_state') is not fixture.signup_email_state
                    or inspect.getattr_static(test, '_signup_email_state') is not fixture._signup_email_state):
                continue
            check = item['assertion']
            if inspect.getattr_static(test, check.func.attr) is not getattr(unittest.TestCase, check.func.attr):
                continue
            allowed = {getattr(unittest.TestCase, name).__code__ for name in
                       (check.func.attr, '_baseAssertEqual', 'fail')}
            trace, valid = frame.tb_next, frame.tb_next is not None
            while trace:
                valid = valid and trace.tb_frame.f_code in allowed and trace.tb_frame.f_locals.get('self') is test
                trace = trace.tb_next
            keys = tuple(APP_FIXTURE_METHODS['signup_email_state']['returns']['properties'])
            values = [frame.tb_frame.f_locals.get(var.id) for var in item['assignment'].targets[0].elts]
            if valid and all(type(v) is str and v == key for v, key in zip(values, keys)):
                return 'NOT_RUN_FIXTURE_RETURN_MISUSED'
    except (AttributeError, OSError, TypeError, IndexError, ValueError, SyntaxError, RecursionError):
        pass
    return None


def _status_used_as_container(test, err):
    """Prove the failing assertIn directly followed unpacking the Host helper's two integers."""
    if err[0] is not TypeError:
        return False
    fixture = getattr(sys.modules.get(APP_FIXTURE_MODULE), 'AppTestCase', None)
    if fixture is None or not isinstance(test, fixture):
        return False
    try:
        if inspect.getattr_static(test, 'concurrent_signup_statuses') is not fixture.concurrent_signup_statuses:
            return False
        method = inspect.getattr_static(type(test), test._testMethodName)
        frame = err[2]
        while frame and frame.tb_frame.f_code is not method.__code__:
            frame = frame.tb_next
        target = frame.tb_next if frame else None
        if (not target or target.tb_next or target.tb_frame.f_code is not unittest.TestCase.assertIn.__code__
                or type(target.tb_frame.f_locals.get('container')) is not int):
            return False
        lines, first = inspect.getsourcelines(method)
        body = ast.parse(textwrap.dedent(''.join(lines))).body[0].body
        line = frame.tb_lineno - first + 1
        # Deliberately recognize only adjacent top-level statements. Other causes of a TypeError,
        # including a product returning the wrong type, retain their ordinary measured outcome.
        for assignment, assertion in zip(body, body[1:]):
            if not isinstance(assertion, ast.Expr) or not assertion.lineno <= line <= assertion.end_lineno:
                continue
            if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
                return False
            unpack = assignment.targets[0]
            if not isinstance(unpack, (ast.Tuple, ast.List)) or len(unpack.elts) != 2:
                return False
            call, check = assignment.value, assertion.value
            def self_call(node, name):
                return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == name
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self')
            return (self_call(call, 'concurrent_signup_statuses') and self_call(check, 'assertIn')
                    and len(check.args) == 2 and isinstance(check.args[0], (ast.Constant, ast.Name))
                    and isinstance(check.args[1], ast.Name) and not check.keywords
                    and all(isinstance(var, ast.Name) for var in unpack.elts)
                    and check.args[1].id in {var.id for var in unpack.elts})
    except (AttributeError, OSError, TypeError, IndexError, ValueError, SyntaxError, RecursionError):
        return False
    return False


class _Result(unittest.TestResult):
    """Aggregate failed subtests into their parent, count class callbacks too."""

    def __init__(self, expected, capture_observations=False, roots=(), exposure_checks=None, issued_passwords=None, diagnostics=None, signup_checks=None, fixture_usage_repair=False, observer_state=None):
        super().__init__()
        self.diagnostics = diagnostics
        self.expected = expected
        self.seen = set()
        self.blocked = set()
        # Tests whose body never ran: an ``async def`` test in a plain TestCase
        # returns an un-awaited coroutine and unittest reports success.
        self.unawaited = set()
        # (label, resolved root) pairs telling where an error was raised. A
        # location is evidence for later classification, never a verdict.
        self.roots = tuple(roots)
        self.rows = {}
        self.active = {}
        self.started = 0
        self.finished = 0
        self.invalid = False
        self.capture_observations = capture_observations
        self.fixture_usage_repair = fixture_usage_repair and not capture_observations
        self.signup_checks = signup_checks
        self.exposure_checks = exposure_checks
        self.issued_passwords = issued_passwords
        self.observer_state = observer_state

    def _id(self, test):
        test_id = test.id()
        if (
            type(test_id) is not str or not test_id
            or len(test_id.encode("utf-8")) > MAX_ID_BYTES
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in test_id)
        ):
            raise _HarnessError()
        return test_id

    def startTest(self, test):
        super().startTest(test)
        if self.observer_state is not None:
            self.observer_state['test'] = test
            self.observer_state['httpTestSamples'] = set()
        if self.issued_passwords is not None:
            self.issued_passwords.clear()
        test_id = self._id(test)
        if test_id in self.rows or id(test) in self.active or len(self.rows) >= MAX_CASES:
            raise _HarnessError()
        if test_id not in self.expected:
            raise _HarnessError()
        self.rows[test_id] = {"testId": test_id, "outcome": None}
        self.active[id(test)] = test_id
        self.seen.add(test_id)
        self.started += 1
        method = getattr(test, getattr(test, "_testMethodName", ""), None)
        if inspect.iscoroutinefunction(method) and not isinstance(test, unittest.IsolatedAsyncioTestCase):
            self.unawaited.add(test_id)

    def stopTest(self, test):
        if self.observer_state is not None:
            self.observer_state.pop('test', None)
            self.observer_state.pop('httpTestSamples', None)
        test_id = self.active.pop(id(test), None)
        if test_id is None or self.rows[test_id]["outcome"] is None:
            self.invalid = True
        elif self.capture_observations and hasattr(test, 'detail_recorder'):
            try:
                rows = test.detail_recorder.rows
                if type(rows) is not list or not 1 <= len(rows) <= 256:
                    raise ValueError()
                projected = []
                for row in rows:
                    value = row['actual']
                    if not (value is None or type(value) is bool or type(value) is int and abs(value) <= 10 ** 12
                            or type(value) is list and len(value) <= 100
                            and all(type(v) is int and 0 <= v <= 999 for v in value)):
                        raise ValueError()
                    item = {k: row[k] for k in ('caseId', 'itemId', 'kind', 'actual', 'status', 'reason')}
                    if 'statusOrigin' in row:
                        origin = row['statusOrigin']
                        if (item['itemId'] != 'http_status' or item['kind'] not in ('assertion', 'precondition')
                                or item['status'] not in ('PASS', 'FAIL') or type(value) is not int
                                or not 500 <= value <= 599 or origin not in ('HTTP_RESPONSE', 'ESCAPED_EXCEPTION')
                                or (origin == 'ESCAPED_EXCEPTION' and value != 500)):
                            raise ValueError()
                        item['statusOrigin'] = origin
                    if 'handlerEntry' in row:
                        if (item['kind'] != 'precondition' or item['itemId'] != 'response_status'
                                or row['handlerEntry'] not in ('ENTERED', 'NOT_ENTERED', 'UNKNOWN')):
                            raise ValueError()
                        item['handlerEntry'] = row['handlerEntry']
                    if any(type(item[k]) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item[k])
                           for k in ('caseId', 'itemId', 'kind', 'status')):
                        raise ValueError()
                    if item['reason'] is not None and (type(item['reason']) is not str
                            or not re.fullmatch(r'[A-Z_]{1,64}', item['reason'])):
                        raise ValueError()
                    if 'diagnostic' in row:
                        item['diagnostic'] = row['diagnostic']
                    projected.append(item)
                self.rows[test_id]['observations'] = projected
            except Exception:
                self.rows[test_id]['observationError'] = 'DETAIL_RECORD_INVALID'
        if self.signup_checks is not None:
            facts = self.signup_checks.pop(test, None)
            if facts and test_id in self.rows:
                self.rows[test_id]['signupChecks'] = facts
        if self.exposure_checks is not None:
            marks = self.exposure_checks.pop(test, {})
            if marks and test_id in self.rows:
                self.rows[test_id]["outputChecks"] = marks
        if self.diagnostics is not None:
            diagnostic = self.diagnostics.pop(test, None)
            if diagnostic and test_id in self.rows:
                self.rows[test_id]['outputDiagnostics'] = diagnostic
        self.finished += 1
        super().stopTest(test)

    def _outcome(self, test, outcome, details=None):
        test_id = self.active.get(id(test))
        if test_id is None:
            # setUpClass/tearDownClass/module fixture errors and class skips.
            test_id = self._id(test)
            if test_id in self.rows or len(self.rows) >= MAX_CASES:
                raise _HarnessError()
            self.rows[test_id] = {"testId": test_id, "outcome": None}
            if test_id.startswith("setUpClass (") and test_id.endswith(")"):
                class_name = test_id[len("setUpClass ("):-1]
                self.blocked.update(name for name, identity in self.expected.items() if identity[0] == class_name)
            elif test_id.startswith("setUpModule (") and test_id.endswith(")"):
                module_name = test_id[len("setUpModule ("):-1]
                self.blocked.update(name for name, identity in self.expected.items() if identity[1] == module_name)
        row = self.rows[test_id]
        rank = {None: -1, "PASS": 0, "SKIP": 1, "FAIL": 2}
        if rank[outcome] >= rank[row["outcome"]]:
            row["outcome"] = outcome
            if details is not None:
                row["details"] = _bounded(details, MAX_DETAILS_BYTES)

    def _origin(self, traceback):
        """TEST, PRODUCT or LIBRARY: the deepest traceback frame inside a known root."""
        origin, depth = "LIBRARY", 0
        while traceback is not None and depth < 256:
            try:
                filename = str(Path(traceback.tb_frame.f_code.co_filename).resolve())
            except (OSError, ValueError, RuntimeError):
                filename = ""
            # The most specific root wins (a snapshot's tests/ sits inside its Source).
            matched = [(len(root), label) for label, root in self.roots if filename.startswith(root + os.sep)]
            if matched:
                origin = max(matched)[1]
            traceback, depth = traceback.tb_next, depth + 1
        return origin

    def _error(self, err):
        return _bounded(err[0].__name__ + ": " + str(err[1]) + " [origin=" + self._origin(err[2]) + "]",
                        MAX_DETAILS_BYTES)

    def _failure_facts(self, err):
        """Project only exception class, source coordinates and proven HTTP comparisons.

        Never copy frame locals, exception messages, assertion values or SQL to
        the public diagnostic. A missing table is a symptom, not blame assigned
        to the product: a test may have failed to enter the app's lifespan.
        """
        allowed = {'AssertionError', 'OperationalError', 'IntegrityError', 'ProgrammingError',
            'StatementError', 'DatabaseError', 'DataError', 'RuntimeError', 'ValueError', 'TypeError',
            'AttributeError', 'ImportError', 'ModuleNotFoundError', 'KeyError'}
        name = err[0].__name__
        data = {'kind': 'ASSERTION' if issubclass(err[0], AssertionError) else 'EXCEPTION',
                'exceptionType': name if name in allowed else 'OTHER', 'origin': self._origin(err[2])}
        if name == 'OperationalError' and 'no such table:' in str(err[1]):
            data['cause'] = 'DATABASE_TABLE_MISSING'
        trace, frames, equality, membership = err[2], [], None, None
        while trace is not None and len(frames) < 256:
            frames.append(trace)
            if trace.tb_frame.f_code is unittest.TestCase.assertEqual.__code__:
                first, second = (trace.tb_frame.f_locals.get(k) for k in ('first', 'second'))
                if all(type(v) is int and 100 <= v <= 599 for v in (first, second)):
                    equality = first, second
            elif trace.tb_frame.f_code is unittest.TestCase.assertIn.__code__:
                member, container = (trace.tb_frame.f_locals.get(k) for k in ('member', 'container'))
                if (type(member) is int and 100 <= member <= 599
                        and type(container) in (tuple, list, set, frozenset) and 1 <= len(container) <= 16
                        and all(type(v) is int and 100 <= v <= 599 for v in container)):
                    membership = member, sorted(set(container))
            trace = trace.tb_next
        for frame in frames:
            try:
                path = Path(frame.tb_frame.f_code.co_filename).resolve()
                matches = [(len(root), label) for label, root in self.roots
                           if str(path).startswith(root + os.sep)]
                if not matches or not path.is_file() or path.stat().st_size > MAX_OUTPUT_BYTES:
                    continue
                raw = path.read_bytes()
                data['location'] = {'sourceSha256': hashlib.sha256(raw).hexdigest(), 'line': frame.tb_lineno}
                if max(matches)[1] != 'TEST' or self.capture_observations:
                    continue
                for node in ast.walk(ast.parse(raw)):
                    if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                            or node.func.attr not in ('assertEqual', 'assertIn') or len(node.args) < 2
                            or not node.lineno <= frame.tb_lineno <= node.end_lineno):
                        continue
                    if node.func.attr == 'assertIn' and membership is not None:
                        argument, expected = node.args[:2]
                        if (isinstance(argument, ast.Attribute) and argument.attr == 'status_code'
                                and isinstance(expected, (ast.Tuple, ast.List, ast.Set))
                                and 1 <= len(expected.elts) <= 16
                                and all(isinstance(v, ast.Constant) and type(v.value) is int
                                        and 100 <= v.value <= 599 for v in expected.elts)
                                and sorted(set(v.value for v in expected.elts)) == membership[1]):
                            data['comparison'] = {'actualStatus': membership[0], 'expectedStatuses': membership[1]}
                        continue
                    if node.func.attr != 'assertEqual' or equality is None:
                        continue
                    for index, argument in enumerate(node.args[:2]):
                        expected = node.args[1 - index]
                        if (isinstance(argument, ast.Attribute) and argument.attr == 'status_code'
                                and isinstance(expected, ast.Constant) and type(expected.value) is int
                                and expected.value == equality[1 - index]):
                            data['comparison'] = {'actualStatus': equality[index], 'expectedStatus': equality[1 - index]}
            except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError):
                continue
        return data

    def _record_failure(self, test, err):
        row = self.rows.get(self.active.get(id(test)))
        if row is not None and row['outcome'] == 'FAIL':
            row['failureDiagnostic'] = self._failure_facts(err)

    def _setup_error(self, test, err):
        """Record failure before the body, without deciding whether the product caused it.

        Use unittest's actual setup frame, not exception text or a function named
        setUp in product code. Keep the preparation failure as a separate row,
        as for class/module fixtures, and the declared case as explicitly not run.
        """
        test_id = self.active.get(id(test))
        if test_id is None:
            return False
        setup_codes = {unittest.TestCase._callSetUp.__code__,
                       unittest.IsolatedAsyncioTestCase._callSetUp.__code__}
        other_phase_codes = {getattr(case, name).__code__
                             for case in (unittest.TestCase, unittest.IsolatedAsyncioTestCase)
                             for name in ('_callTestMethod', '_callTearDown', '_callCleanup')}
        traceback, depth = err[2], 0
        while traceback is not None and depth < 256:
            frame = traceback.tb_frame
            if frame.f_code in other_phase_codes and frame.f_locals.get('self') is test:
                return False  # A body/cleanup may explicitly call a setup helper.
            if frame.f_code in setup_codes and frame.f_locals.get('self') is test:
                fixture_id = 'setUp (' + test_id + ')'
                if (fixture_id in self.rows or len(self.rows) >= MAX_CASES
                        or len(fixture_id.encode('utf-8')) > MAX_ID_BYTES):
                    raise _HarnessError()
                self.rows[fixture_id] = {
                    'testId': fixture_id, 'outcome': 'FAIL',
                    'details': _bounded('[phase=SETUP] ' + self._error(err), MAX_DETAILS_BYTES),
                }
                self._outcome(test, 'SKIP', 'NOT_RUN_SETUP_FAILED')
                return True
            traceback, depth = traceback.tb_next, depth + 1
        return False

    def addSuccess(self, test):
        if self.active.get(id(test)) in self.unawaited:
            self._outcome(test, "SKIP", "NOT_RUN_ASYNC_NOT_AWAITED")
        else:
            self._outcome(test, "PASS")

    def addFailure(self, test, err):
        if not self._setup_error(test, err):
            reason = _fixture_usage_reason(test, err) if self.fixture_usage_repair else None
            self._outcome(test, 'SKIP' if reason else 'FAIL', reason or self._error(err))
            self._record_failure(test, err)

    def addError(self, test, err):
        if not self.capture_observations and _status_used_as_container(test, err):
            self._outcome(test, 'SKIP', 'NOT_RUN_HELPER_ASSERTION')
        elif not self._setup_error(test, err):
            reason = _fixture_usage_reason(test, err) if self.fixture_usage_repair else None
            self._outcome(test, 'SKIP' if reason else 'FAIL', reason or self._error(err))
            self._record_failure(test, err)

    def addSkip(self, test, reason):
        parent = getattr(test, "test_case", None)
        # A test-authored skip cannot claim that the trusted runner verified a helper misuse.
        details = str(reason)
        self._outcome(parent if parent is not None else test, "SKIP",
                      'test skipped' if details in ('NOT_RUN_HELPER_ASSERTION', 'NOT_RUN_FIXTURE_ARGUMENT_INVALID', 'NOT_RUN_FIXTURE_MEMBER_MISSING',
                                                    'NOT_RUN_FIXTURE_RETURN_MISUSED') else details)

    def addExpectedFailure(self, test, err):
        self._outcome(test, "SKIP", "expected failure")

    def addUnexpectedSuccess(self, test):
        self._outcome(test, "FAIL", "unexpected success")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._outcome(test, "FAIL", self._error(err))
            self._record_failure(test, err)

    def report(self):
        if (self.invalid or self.active or self.started != self.finished or not self.rows
                or self.seen | self.blocked != set(self.expected)):
            raise _HarnessError()
        # A class/module fixture failure blocked these tests: keep every one as
        # an explicit not-run row next to the fixture row, never drop it.
        for test_id in sorted(self.blocked - self.seen):
            if test_id in self.rows or len(self.rows) >= MAX_CASES:
                raise _HarnessError()
            class_name, module_name = self.expected[test_id]
            fixtures = (self.rows.get("setUpClass (" + class_name + ")"), self.rows.get("setUpModule (" + module_name + ")"))
            failed = any(row is not None and row["outcome"] == "FAIL" for row in fixtures)
            self.rows[test_id] = {"testId": test_id, "outcome": "SKIP",
                                  "details": "NOT_RUN_SETUP_FAILED" if failed else "NOT_RUN_FIXTURE_SKIPPED"}
        rows = list(self.rows.values())
        counts = {name: sum(row["outcome"] == outcome for row in rows)
                  for name, outcome in (("passed", "PASS"), ("failed", "FAIL"), ("skipped", "SKIP"))}
        if sum(counts.values()) != len(rows):
            raise _HarnessError()
        return {"format": "unittest-v1", "total": len(rows), **counts, "tests": rows}


def _expected_cases(suite):
    expected = {}

    def visit(node, depth):
        if depth > 64:
            raise _HarnessError()
        if isinstance(node, unittest.TestSuite):
            for child in node:
                visit(child, depth + 1)
        elif isinstance(node, unittest.TestCase):
            test_id = node.id()
            if (type(test_id) is not str or not test_id or test_id in expected
                    or len(test_id.encode("utf-8")) > MAX_ID_BYTES or len(expected) >= MAX_CASES):
                raise _HarnessError()
            case_type = type(node)
            expected[test_id] = (case_type.__module__ + "." + case_type.__qualname__, case_type.__module__)
        else:
            raise _HarnessError()

    visit(suite, 0)
    if not expected:
        raise _HarnessError()
    return expected


def run_tests(test_root, source_root, pattern="test_*.py", *, capture_observations=False, capture_output_checks=False, fixture_usage_repair=False):
    # The implementation lives in the immutable runner input, already covered
    # by input receipts and QA/Security policy hashes. Never amend test source.
    checks = {} if capture_output_checks else None
    issued = set() if capture_output_checks else None
    diagnostics = {} if capture_output_checks else None
    signup_checks = {} if capture_output_checks else None
    observer_state = {}
    with (_test_data_api(issued, checks, observer_state),
          _app_test_api(source_root, checks, issued, diagnostics, signup_checks),
          _service_app_test_api(source_root)):
        return _run_tests(test_root, source_root, pattern, capture_observations=capture_observations, exposure_checks=checks, issued_passwords=issued, diagnostics=diagnostics, signup_checks=signup_checks, fixture_usage_repair=fixture_usage_repair, observer_state=observer_state)


def _run_tests(test_root, source_root, pattern="test_*.py", *, capture_observations=False, exposure_checks=None, issued_passwords=None, diagnostics=None, signup_checks=None, fixture_usage_repair=False, observer_state=None):
    """Run explicit trusted-fixture tests or, in the Container, mounted tests.

    Return (report, exit_code), where report is None and exit is 2 for empty,
    discovery, incomplete, oversized, or framework failures. Test prints are
    discarded, never interpreted as report bytes. This is not an attestation
    boundary against Python tests intentionally modifying the runner itself.
    """
    budget = [0, False]
    original_stdout, original_stderr = sys.stdout, sys.stderr
    original_path = sys.path[:]
    try:
        sys.stdout = _DiscardStream(budget)
        sys.stderr = _DiscardStream(budget)
        root = Path(test_root)
        source = Path(source_root)
        if (type(pattern) is not str or _SAFE_PATTERN.fullmatch(pattern) is None
                or not root.is_dir() or not source.is_dir()):
            raise _HarnessError()
        sys.path.insert(0, str(source))
        loader = unittest.TestLoader()
        suite = loader.discover(str(root), pattern=pattern, top_level_dir=str(root))
        if loader.errors or not 1 <= suite.countTestCases() <= MAX_CASES or budget[1]:
            raise _HarnessError()
        result = _Result(_expected_cases(suite), capture_observations,
                         roots=(("TEST", str(root.resolve())), ("PRODUCT", str(source.resolve()))), exposure_checks=exposure_checks, issued_passwords=issued_passwords, diagnostics=diagnostics, signup_checks=signup_checks, fixture_usage_repair=fixture_usage_repair, observer_state=observer_state)
        suite.run(result)
        if loader.errors or result.shouldStop or budget[1] or result.testsRun != result.started:
            raise _HarnessError()
        report = result.report()
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise _HarnessError()
        return report, 1 if report["failed"] else 0
    except BaseException:
        return None, 2
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        sys.path[:] = original_path


@contextmanager
def _discard_file_descriptors():
    """Suppress direct os.write(1/2) as well as the Python streams in the CLI."""
    saved_stdout = saved_stderr = devnull = None
    try:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        if devnull is not None:
            os.close(devnull)


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--kind", choices=("SNAPSHOT", "QA_TESTS", "PROTECTED"), required=True)
    parser.add_argument("--security-output-checks", "--qa-output-checks", dest="security_output_checks", action="store_true")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--pattern", required=True)
    try:
        options = parser.parse_args(argv)
        if not _safe_directory(options.directory) or _SAFE_PATTERN.fullmatch(options.pattern) is None:
            raise _HarnessError()
        test_root = Path("/snapshot" if options.kind == "SNAPSHOT" else "/inputs") / options.directory
        with _discard_file_descriptors():
            report, exit_code = run_tests(test_root, Path("/snapshot"), options.pattern,
                                          capture_observations=options.kind == 'PROTECTED', capture_output_checks=options.kind == 'QA_TESTS' and options.security_output_checks,
                                          fixture_usage_repair=options.kind == 'QA_TESTS')
    except BaseException:
        report, exit_code = None, 2
    payload = report if report is not None else {"error": "TEST_RUNNER_ERROR"}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Synthetic Source ingress credentials; no real secrets or Source execution."""

import io
import tarfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from orchestrator.core.source_credentials import (
    SourceCredentialError, validate_source_archive_credentials,
    validate_source_bytes, validate_source_text,
)
from orchestrator.sandbox.contracts import SandboxError, SandboxErrorCode
from orchestrator.sandbox.materialization import _archive_entries, _inputs_entries


_SYNTHETIC_SECRET = "SYNTHETIC_SOURCE_CREDENTIAL_NOT_REAL_1234"


def archive(content, *, name="assets/config.bin"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as target:
        member = tarfile.TarInfo(name)
        member.mode = 0o644
        member.uid = member.gid = member.mtime = 0
        member.uname = member.gname = ""
        member.size = len(content)
        target.addfile(member, io.BytesIO(content))
    return output.getvalue()


class SourceCredentialsTests(unittest.TestCase):
    def assert_denied(self, operation, value):
        with self.assertRaises(SourceCredentialError) as caught:
            operation(value)
        self.assertEqual(str(caught.exception), "SOURCE_CREDENTIAL_DENIED")
        self.assertNotIn(_SYNTHETIC_SECRET, str(caught.exception))

    def test_recognizable_credential_literals_reject_without_echo(self):
        for label in ("password", "password_hash", "api_key", "API_TOKEN", "client_secret",
                      "proxy_authorization", "비밀번호", "암호", "토큰"):
            with self.subTest(label=label):
                self.assert_denied(validate_source_text, f'{label} = "{_SYNTHETIC_SECRET}"')

    def test_ordinary_password_variables_preserve_original_bytes(self):
        source = ("# 보통 코드\r\npassword = request.password\r\n"
                  "password_hash = hash_password(password)\r\n"
                  "api_key = os.environ.get('SERVICE_KEY')\r\n")
        validate_source_text(source)
        self.assertEqual(sanitize_content({"content": source}, source_fields=("content",))["content"], source)

    def test_source_tools_reuse_common_guard(self):
        with self.assertRaises(LLMRuntimeError) as caught:
            sanitize_content({"content": f'api_key="{_SYNTHETIC_SECRET}"'}, source_fields=("content",))
        self.assertEqual(caught.exception.code, LLMErrorCode.TOOL_POLICY)

    def test_typed_and_mapping_credential_literals_reject(self):
        for source in (
            'api_key: str = "SYNTHETIC_TYPED_SECRET"',
            'const apiKey: string = "SYNTHETIC_TYPED_SECRET";',
            'password: Optional[str] = "SYNTHETIC_TYPED_SECRET"',
            '{"apiKey": "SYNTHETIC_MAPPING_SECRET"}',
        ):
            self.assert_denied(validate_source_text, source)
        validate_source_text("password: str = request.password")

    def test_recognizable_unlabelled_keys_and_private_key_reject(self):
        values = (
            'value = "sk-proj-' + "SYNTHETIC" * 5 + '"',
            'value = "AKIA' + "0" * 16 + '"',
            'value = "ghp_' + "SYNTHETIC" * 4 + '"',
            'value = "AIza' + "0" * 35 + '"',
            'value = "Bearer SYNTHETIC_NOT_REAL"',
            'value = "eyJsynthetic.eyJsynthetic.synthetic"',
            'value = "$argon2id$v=19$m=1,t=1,p=1$synthetic$synthetic"',
            "-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----",
        )
        for index, value in enumerate(values):
            with self.subTest(value_index=index):
                self.assert_denied(validate_source_text, value)

    def test_common_escaped_and_compatibility_forms_reject(self):
        for source in (
            r'api\u005fkey = "SYNTHETIC_ESCAPED_SECRET"',
            r'api\x5fkey = "SYNTHETIC_ESCAPED_SECRET"',
            'ａｐｉ＿ｋｅｙ = "SYNTHETIC_NORMALIZED_SECRET"',
            'value = "sk-" + "' + "SYNTHETIC" * 5 + '"',
            'value = "sk-" "' + "SYNTHETIC" * 5 + '"',
        ):
            self.assert_denied(validate_source_text, source)

    def test_text_encoding_and_binary_extension_cannot_bypass(self):
        for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding):
                value = f'비밀번호 = "{_SYNTHETIC_SECRET}"'.encode(encoding)
                self.assert_denied(validate_source_bytes, value)
                self.assert_denied(validate_source_archive_credentials, archive(value))

    def test_embedded_ascii_credential_in_binary_rejects(self):
        for value in (
            b"\xff\x80" + f'api_key="{_SYNTHETIC_SECRET}"'.encode() + b"\xfe\x81",
            b"\xff" + f'api_key="{_SYNTHETIC_SECRET}"'.encode("utf-16-le") + b"\xfe",
        ):
            self.assert_denied(validate_source_bytes, value)

    def test_normal_binary_assets_and_password_variables_are_unchanged(self):
        for value in (bytes(range(256)), b"\x89PNG\r\n\x1a\n\x00\xff\x80binary", b"password=request.password\r\n"):
            validate_source_bytes(value)
            data = archive(value)
            validate_source_archive_credentials(data)
            self.assertEqual(_archive_entries(data), (("assets/config.bin", value, 0o644),))

    def test_registered_host_secret_rejects_even_without_known_prefix(self):
        with patch("orchestrator.core.security._KNOWN_SECRETS", (_SYNTHETIC_SECRET,)):
            self.assert_denied(validate_source_text, f'value="{_SYNTHETIC_SECRET}"')
            self.assert_denied(validate_source_bytes, b"\xff" + _SYNTHETIC_SECRET.encode() + b"\xfe")

    def test_url_literal_credentials_reject_but_runtime_auth_expressions_remain_valid(self):
        for value in (
            'url="https://user:SYNTHETIC_PASSWORD@host.invalid/path"',
            'url="https://host.invalid/path?api_key=SYNTHETIC_KEY"',
            'url="https://host.invalid/path?%61pi%5fkey=SYNTHETIC_KEY"',
            'url=f"https://host.invalid/{path}?token=SYNTHETIC_KEY"',
            'url=f"https://{username}:SYNTHETIC_PASSWORD@host.invalid/path"',
        ):
            self.assert_denied(validate_source_text, value)
        for value in (
            'url=f"https://host.invalid/path?token={token}"',
            'url=f"https://user:{request.password}@host.invalid/path"',
            'url=`https://host.invalid/path?token=${token}`',
            'url="https://host.invalid/path?token=" + token',
            'url="https://host.invalid/path?page=1"',
        ):
            validate_source_text(value)

    def test_encoded_and_nested_source_url_credentials_cannot_hide(self):
        uri = "https://host.invalid/path?api_key=SYNTHETIC_ENCODED_KEY"
        for _ in range(3):
            uri = quote(uri, safe="")
            self.assert_denied(validate_source_text, 'url="' + uri + '"')
        self.assert_denied(validate_source_text,
                           'url="https://outer.invalid/?redirect=https://inner.invalid/?token=SYNTHETIC_NESTED_KEY"')

    def test_sandbox_source_and_host_inputs_deny_before_materialization(self):
        content = f'api_key="{_SYNTHETIC_SECRET}"'.encode()
        for operation in (lambda: _archive_entries(archive(content)),
                          lambda: _inputs_entries({"fixture.bin": content})):
            with self.assertRaises(SandboxError) as caught:
                operation()
            self.assertEqual(caught.exception.code, SandboxErrorCode.PATH)
            self.assertNotIn(_SYNTHETIC_SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

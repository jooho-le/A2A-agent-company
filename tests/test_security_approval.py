"""Approved Host identity only; inert criteria, no product test or execution."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import CodeType, FunctionType
import unittest
from unittest.mock import patch

from agents.platform.security_approval import (
    ApprovedSecurityAssessor, SecurityApprovalError,
    calculate_implementation_sha256, load_approved_assessor,
)


def fixture_assessor(request):
    """A contract fixture, not an actual security assessment."""
    return None


async def fixture_async_assessor(request):
    return None


def fixture_nested_code_assessor(request):
    def identity(value):
        return value
    return identity(request)


def fixture_wrong_arity(first, second):
    return None


def fixture_default(request=None):
    return None


def fixture_keyword_default(request, *, extra=None):
    return None


def fixture_variadic(request, *extras):
    return None


def fixture_generator(request):
    yield request


class SecurityApprovalTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-security-approval-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.approval_file = self.directory / "approval.json"
        self.module_file = Path(__file__).resolve()
        self.support_file = self.directory / "support.py"
        self.support_file.write_text("# inert approved Host dependency fixture; never imported\n", encoding="utf-8")
        self.files = (
            ("assessor-module", self.module_file, sha256(self.module_file.read_bytes()).hexdigest()),
            ("support", self.support_file, sha256(self.support_file.read_bytes()).hexdigest()),
        )
        self.data = {
            "schemaVersion": 1, "verifierId": "inert-fixture-assessor", "verifierVersion": "1.0.0",
            "criteriaRef": "criteria://fixture/inert-v1", "scenarioContractSha256": "e" * 64,
            "criteria": {"mode": "inert-fixture-only", "rules": [{"name": "no-product-criteria"}]},
            "implementationSha256": calculate_implementation_sha256(fixture_assessor, self.files),
            "implementationFiles": [{"name": name, "path": str(path) if path == self.module_file else path.name,
                                     "sha256": digest} for name, path, digest in self.files],
        }

    def write(self, data=None):
        self.approval_file.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")
        return self.approval_file

    def load(self, data=None, assessor=fixture_assessor):
        return load_approved_assessor(self.write(data), assessor)

    def rejected(self, operation, *arguments):
        with self.assertRaises(SecurityApprovalError) as caught:
            operation(*arguments)
        self.assertEqual(caught.exception.code, "SECURITY_APPROVAL_INVALID")
        self.assertEqual(str(caught.exception), caught.exception.code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertNotIn("private-fixture-secret", str(caught.exception))

    def test_complete_approval_is_frozen_inert_and_host_paths_stay_private(self):
        with patch.object(os, "system", side_effect=AssertionError("no process")), \
                patch("importlib.import_module", side_effect=AssertionError("no dynamic import")):
            approved = self.load()
            self.assertIsNone(approved.verify_integrity())
        self.assertIs(type(approved), ApprovedSecurityAssessor)
        self.assertIs(approved.assess, fixture_assessor)
        self.assertEqual(repr(approved), "ApprovedSecurityAssessor()")
        self.assertEqual(approved.approval_path, self.approval_file)
        self.assertEqual(approved.implementation_paths, tuple(item[1] for item in self.files))
        self.assertEqual(approved.scenario_contract_sha256, "e" * 64)
        self.assertNotIn(str(self.directory), json.dumps(approved.descriptor))
        self.assertNotIn(str(self.module_file), json.dumps(approved.descriptor))
        with self.assertRaises(FrozenInstanceError):
            approved._assessor = fixture_async_assessor

    def test_constructor_is_private_and_performs_no_file_io(self):
        with patch.object(os, "open", side_effect=AssertionError("inert")):
            self.rejected(ApprovedSecurityAssessor)

    def test_criteria_and_descriptor_are_deeply_detached_fresh_copies(self):
        approved = self.load()
        before = approved.approval_sha256
        criteria = approved.criteria
        criteria["rules"][0]["name"] = "caller mutation"
        descriptor = approved.descriptor
        descriptor["implementationFiles"][0]["sha256"] = "0" * 64
        self.assertEqual(approved.criteria, self.data["criteria"])
        self.assertEqual(approved.approval_sha256, before)
        expected = sha256(json.dumps(self.data["criteria"], ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(approved.criteria_sha256, expected)

    def test_all_fields_required_and_unknown_keys_are_not_accepted(self):
        for key in self.data:
            data = {name: value for name, value in self.data.items() if name != key}
            with self.subTest(missing=key):
                self.rejected(self.load, data)
        for key in ("assessor", "module", "callback", "apiKey", "schema_version"):
            self.rejected(self.load, {**self.data, key: "private-fixture-secret"})

    def test_schema_version_is_not_coerced(self):
        for value in (True, False, "1", 1.0, 0, 2, None):
            with self.subTest(value=value):
                self.rejected(self.load, {**self.data, "schemaVersion": value})

    def test_json_duplicates_nonfinite_and_invalid_encoding_rejected(self):
        raw = json.dumps(self.data)
        for content in ("{\"schemaVersion\":1," + raw[1:], raw.replace("1.0.0", "1.0.0", 1).replace(
                '"criteria": {', '"criteria": {"bad": NaN,', 1), "```json\n" + raw + "\n```"):
            self.approval_file.write_text(content, encoding="utf-8")
            self.rejected(load_approved_assessor, self.approval_file, fixture_assessor)
        self.approval_file.write_bytes(b"\xff")
        self.rejected(load_approved_assessor, self.approval_file, fixture_assessor)

    def test_criteria_must_be_nonempty_bounded_json_without_secrets(self):
        nested = {"value": "leaf"}
        for _ in range(20):
            nested = {"nested": nested}
        for criteria in ({}, [], None, {"api_key": "private-fixture-secret"},
                         {"tooLong": "x" * 70_000}, nested, {"items": [None] * 4097}):
            with self.subTest(criteria_type=type(criteria)):
                self.rejected(self.load, {**self.data, "criteria": criteria})

    def test_labels_and_criteria_reference_are_logical_metadata_not_host_paths(self):
        for field, values in (
            ("verifierId", ("", "name\n", str(self.directory), None, True)),
            ("verifierVersion", ("", "one two", "a/b", None, True)),
            ("criteriaRef", ("", str(self.approval_file), "file:///private/test", "javascript:bad", "urn:",
                              "criteria://", "criteria://foo bar", "criteria://operator:private-fixture-secret@policy/v1",
                              None, True)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.rejected(self.load, {**self.data, field: value})

    def test_all_sha256_fields_are_exact_lowercase_hex(self):
        for field in ("scenarioContractSha256", "implementationSha256"):
            for value in (None, True, "A" * 64, "g" * 64, "a" * 63, "a" * 65, "sha256:" + "a" * 64):
                with self.subTest(field=field, value=value):
                    self.rejected(self.load, {**self.data, field: value})

    def test_manifest_items_are_closed_and_names_ascii_unique(self):
        for changed in ({"extra": "forbidden"}, {"name": "../module"}, {"name": "한글"},
                        {"name": ""}, {"sha256": "A" * 64}, {"path": "https://invalid/module.py"}):
            data = deepcopy(self.data)
            data["implementationFiles"][0].update(changed)
            self.rejected(self.load, data)
        for key in ("name", "path", "sha256"):
            data = deepcopy(self.data)
            del data["implementationFiles"][0][key]
            self.rejected(self.load, data)
        data = deepcopy(self.data)
        data["implementationFiles"][1]["name"] = data["implementationFiles"][0]["name"].upper()
        self.rejected(self.load, data)
        self.rejected(self.load, {**self.data, "implementationFiles": []})
        self.rejected(self.load, {**self.data, "implementationFiles": self.data["implementationFiles"] * 17})

    def test_callback_own_loaded_module_must_be_in_the_declared_manifest(self):
        files = (self.files[1],)
        self.rejected(calculate_implementation_sha256, fixture_assessor, files)
        data = deepcopy(self.data)
        data["implementationFiles"] = data["implementationFiles"][1:]
        self.rejected(self.load, data)

    def test_non_function_nested_closure_lambda_and_non_unary_callbacks_rejected(self):
        captured = "inert"
        def nested(request):
            return None
        def closure(request):
            return captured
        class CallableInstance:
            def __call__(self, request):
                return None
        for assessor in (None, object(), CallableInstance(), partial(fixture_assessor), lambda request: None,
                         nested, closure, fixture_wrong_arity, fixture_default, fixture_keyword_default,
                         fixture_variadic, fixture_generator, self.test_constructor_is_private_and_performs_no_file_io):
            with self.subTest(assessor_type=type(assessor)):
                self.rejected(calculate_implementation_sha256, assessor, self.files)

    def test_spoofing_nested_function_qualname_cannot_restore_top_level_identity(self):
        def nested(request):
            return None
        nested.__qualname__ = nested.__name__
        self.rejected(calculate_implementation_sha256, nested, self.files)

    def test_unary_named_async_function_is_supported_without_invoking_it(self):
        data = deepcopy(self.data)
        data["implementationSha256"] = calculate_implementation_sha256(fixture_async_assessor, self.files)
        approved = self.load(data, fixture_async_assessor)
        self.assertIs(approved.assess, fixture_async_assessor)
        approved.verify_integrity()

    def test_explicit_module_globals_not_spoofed_function_dictionary(self):
        spoofed = FunctionType(fixture_assessor.__code__, dict(fixture_assessor.__globals__), fixture_assessor.__name__)
        self.rejected(calculate_implementation_sha256, spoofed, self.files)

    def test_code_hash_is_independent_of_filename_and_line_numbers_recursively(self):
        def relabel(code):
            constants = tuple(relabel(value) if type(value) is CodeType else value for value in code.co_consts)
            return code.replace(co_filename="/private/operator-only/relocated.py", co_firstlineno=987,
                                co_linetable=b"", co_consts=constants)
        clone = FunctionType(relabel(fixture_nested_code_assessor.__code__),
                             fixture_nested_code_assessor.__globals__, fixture_nested_code_assessor.__name__)
        self.assertEqual(calculate_implementation_sha256(fixture_nested_code_assessor, self.files),
                         calculate_implementation_sha256(clone, self.files))

    def test_manifest_order_is_not_semantic_approval_change(self):
        self.assertEqual(calculate_implementation_sha256(fixture_assessor, self.files),
                         calculate_implementation_sha256(fixture_assessor, self.files[::-1]))
        first = self.load()
        data = deepcopy(self.data)
        data["implementationFiles"].reverse()
        second = self.load(data)
        self.assertEqual(first.descriptor, second.descriptor)
        self.assertEqual(first.approval_sha256, second.approval_sha256)

    def test_criteria_and_approval_version_change_approval_identity(self):
        before = self.load().approval_sha256
        for field, value in (("criteria", {"mode": "other-inert-criteria"}), ("verifierVersion", "2.0.0"),
                             ("criteriaRef", "urn:other:fixture"), ("scenarioContractSha256", "f" * 64)):
            self.assertNotEqual(self.load({**self.data, field: value}).approval_sha256, before)

    def test_disk_file_drift_is_rejected_by_already_loaded_approval(self):
        approved = self.load()
        self.support_file.write_text("# changed inert Host implementation fixture\n", encoding="utf-8")
        self.rejected(approved.verify_integrity)
        self.rejected(load_approved_assessor, self.approval_file, fixture_assessor)

    def test_approval_criteria_file_drift_is_rejected(self):
        approved = self.load()
        self.write({**self.data, "criteria": {"mode": "different criteria"}})
        self.rejected(approved.verify_integrity)

    def test_loaded_callback_code_replacement_is_rejected_without_disk_change(self):
        approved = self.load()
        previous = fixture_assessor.__code__
        try:
            fixture_assessor.__code__ = previous.replace(co_consts=(*previous.co_consts, "inert changed code"))
            self.rejected(approved.verify_integrity)
        finally:
            fixture_assessor.__code__ = previous
        approved.verify_integrity()

    def test_symlink_file_and_parent_are_denied_for_loader_and_per_call(self):
        self.write()
        alias = self.directory / "approval-alias.json"
        alias.symlink_to(self.approval_file)
        self.rejected(load_approved_assessor, alias, fixture_assessor)
        outside = self.directory / "actual"
        outside.mkdir()
        copy = outside / "support.py"
        copy.write_bytes(self.support_file.read_bytes())
        parent = self.directory / "linked"
        parent.symlink_to(outside, target_is_directory=True)
        data = deepcopy(self.data)
        data["implementationFiles"][1]["path"] = "linked/support.py"
        self.rejected(self.load, data)
        approved = self.load()
        self.support_file.rename(self.directory / "saved-support.py")
        self.support_file.symlink_to(copy)
        self.rejected(approved.verify_integrity)

    def test_same_inode_under_two_file_names_is_not_a_manifest(self):
        alias = self.directory / "alias.py"
        os.link(self.support_file, alias)
        duplicated = (*self.files, ("another", alias, self.files[1][2]))
        self.rejected(calculate_implementation_sha256, fixture_assessor, duplicated)

    def test_non_python_directory_and_oversized_files_denied(self):
        for name, size in (("not-python.txt", 1), ("too-large.py", 1_048_577)):
            path = self.directory / name
            path.write_bytes(b"#" * size)
            files = (self.files[0], ("invalid", path, sha256(path.read_bytes()).hexdigest()))
            self.rejected(calculate_implementation_sha256, fixture_assessor, files)
        self.rejected(calculate_implementation_sha256, fixture_assessor,
                      (self.files[0], ("directory", self.directory / "folder.py", "a" * 64)))

    def test_empty_init_py_is_an_approved_hashed_regular_dependency(self):
        empty = self.directory / "__init__.py"
        empty.write_bytes(b"")
        empty_hash = sha256(b"").hexdigest()
        files = (*self.files, ("package-init", empty, empty_hash))
        data = deepcopy(self.data)
        data["implementationFiles"].append({"name": "package-init", "path": "__init__.py", "sha256": empty_hash})
        data["implementationSha256"] = calculate_implementation_sha256(fixture_assessor, files)
        approved = self.load(data)
        approved.verify_integrity()
        self.assertIn(empty, approved.implementation_paths)
        self.assertIn({"name": "package-init", "sha256": empty_hash}, approved.descriptor["implementationFiles"])
        empty.write_bytes(b"# newly nonempty package init\n")
        self.rejected(approved.verify_integrity)

    def test_empty_dependency_still_requires_exact_hash_and_assessor_module(self):
        empty = self.directory / "__init__.py"
        empty.write_bytes(b"")
        self.rejected(calculate_implementation_sha256, fixture_assessor,
            (*self.files, ("package-init", empty, "0" * 64)))
        self.rejected(calculate_implementation_sha256, fixture_assessor,
            (("package-init", empty, sha256(b"").hexdigest()),))

    def test_total_implementation_bytes_are_bounded_even_when_each_file_fits(self):
        content = b"#" * 1_048_576
        digest = sha256(content).hexdigest()
        extras = []
        for index in range(4):
            path = self.directory / f"inert-{index}.py"
            path.write_bytes(content)
            extras.append((f"inert-{index}", path, digest))
        self.rejected(calculate_implementation_sha256, fixture_assessor, (self.files[0], *extras))
        data = deepcopy(self.data)
        data["implementationFiles"] = [data["implementationFiles"][0],
            *({"name": name, "path": path.name, "sha256": item_hash} for name, path, item_hash in extras)]
        self.rejected(self.load, data)

    def test_criteria_total_bytes_are_bounded_across_multiple_values(self):
        criteria = {f"field-{index}": "x" * 8192 for index in range(9)}
        self.rejected(self.load, {**self.data, "criteria": criteria})

    def test_exact_file_count_bound_and_missing_manifest_fields(self):
        self.rejected(calculate_implementation_sha256, fixture_assessor,
                      tuple((f"name-{index}", self.module_file, self.files[0][2]) for index in range(33)))
        data = deepcopy(self.data)
        data["implementationFiles"][0]["sha256"] = None
        self.rejected(self.load, data)

    def test_oversized_approval_file_is_denied_before_parse(self):
        self.approval_file.write_bytes(b" " * 262_145)
        self.rejected(load_approved_assessor, self.approval_file, fixture_assessor)

    def test_approval_whitespace_and_object_order_are_not_a_policy_change(self):
        approved = self.load()
        self.approval_file.write_text(json.dumps(dict(reversed(list(self.data.items()))), indent=4), encoding="utf-8")
        approved.verify_integrity()

    def test_each_integrity_verification_re_reads_approval_and_implementation(self):
        approved = self.load()
        with patch.object(os, "read", wraps=os.read) as read:
            approved.verify_integrity()
        self.assertGreaterEqual(read.call_count, 6)

    def test_fifo_cannot_block_host_approval_read(self):
        fifo = self.directory / "fifo.py"
        os.mkfifo(fifo)
        self.rejected(calculate_implementation_sha256, fixture_assessor,
                      (self.files[0], ("fifo", fifo, "a" * 64)))

    def test_file_hash_and_helper_arguments_require_valid_exact_shapes(self):
        for files in (list(self.files), (), ((self.files[0][0], self.files[0][1]),),
                      ((self.files[0][0], str(self.files[0][1]), self.files[0][2]),),
                      (self.files[0], ("support", self.support_file, "b" * 64))):
            self.rejected(calculate_implementation_sha256, fixture_assessor, files)

    def test_privileged_forged_approval_is_revalidated_before_use(self):
        approved = self.load()
        data = deepcopy(self.data)
        data["implementationSha256"] = "b" * 64
        object.__setattr__(approved, "_definition_json", json.dumps(data))
        self.rejected(approved.verify_integrity)
        forged = object.__new__(ApprovedSecurityAssessor)
        self.rejected(forged.verify_integrity)

    def test_loading_and_integrity_do_not_create_or_modify_files(self):
        self.write()
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.directory.rglob("*") if path.is_file()}
        approved = load_approved_assessor(self.approval_file, fixture_assessor)
        approved.verify_integrity()
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

"""Offline exact unified-diff tests: no product command or file mutation."""

from dataclasses import replace
import difflib
import unittest
from unittest.mock import patch as mock_patch

from mcp_tools.core.catalog import MAX_FILE_BYTES
from mcp_tools.tools.patching import (
    FilePatch, MAX_PATCH_FILES, MAX_PATCH_HUNKS, MAX_PATCH_LINES,
    PatchError, PatchHunk, PatchLine, apply_file_patch, parse_patch,
)


def diff(old: str, new: str, path: str = "source/app.py") -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path,
    ))


def single(old: str = "old", new: str = "new", path: str = "source/app.py") -> str:
    return f"--- {path}\n+++ {path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


class MCPUnifiedPatchTests(unittest.TestCase):
    def apply_one(self, text, original):
        patches = parse_patch(text)
        self.assertEqual(len(patches), 1)
        return apply_file_patch(patches[0], original)

    def assert_error(self, operation, code="PATCH_FAILED"):
        with self.assertRaises(PatchError) as raised:
            operation()
        error = raised.exception
        self.assertEqual(error.code, code)
        self.assertEqual(str(error), code)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_modify_with_exact_context_and_utf8(self):
        old = "# 회원가입\npassword = user.password\nreturn password\n"
        new = "# 회원가입\npassword = user.password\nreturn hash_password(password)\n"
        self.assertEqual(self.apply_one(diff(old, new), old.encode()), new.encode())

    def test_optional_git_headers_and_index_do_not_change_permissions(self):
        for mode in ("", " 100644", " 100755"):
            text = (
                "diff --git a/source/app.py b/source/app.py\n"
                f"index abcdef1..1234567{mode}\n"
                + single(path="a/source/app.py").replace("+++ a/", "+++ b/")
            )
            with self.subTest(mode=mode):
                self.assertEqual(self.apply_one(text, b"old\n"), b"new\n")

    def test_plain_git_prefixed_headers(self):
        text = single(path="a/source/app.py").replace("+++ a/", "+++ b/")
        parsed = parse_patch(text)[0]
        self.assertEqual(parsed.path, "source/app.py")
        self.assertEqual(parsed.old_path, "source/app.py")
        self.assertEqual(parsed.new_path, "source/app.py")
        self.assertEqual(apply_file_patch(parsed, b"old\n"), b"new\n")

    def test_unicode_and_spaces_in_plain_headers_preserved(self):
        name = "source/한글 디렉터리/ 가입.py"
        parsed = parse_patch(single(path=name))[0]
        self.assertEqual(parsed.path, name)
        self.assertEqual(apply_file_patch(parsed, b"old\n"), b"new\n")

    def test_unicode_and_spaces_in_git_headers_preserved(self):
        name = "source/한글 dir/file name.py"
        text = f"diff --git a/{name} b/{name}\n" + single(path=f"a/{name}").replace("+++ a/", "+++ b/")
        self.assertEqual(parse_patch(text)[0].path, name)
        self.assertEqual(self.apply_one(text, b"old\n"), b"new\n")

    def test_add_file(self):
        text = "--- /dev/null\n+++ source/new.py\n@@ -0,0 +1,2 @@\n+first\n+second\n"
        parsed = parse_patch(text)[0]
        self.assertIsNone(parsed.old_path)
        self.assertEqual(apply_file_patch(parsed, None), b"first\nsecond\n")

    def test_git_add_regular_file_mode(self):
        text = (
            "diff --git a/source/new.py b/source/new.py\n"
            "new file mode 100644\nindex 0000000..1234567\n"
            "--- /dev/null\n+++ b/source/new.py\n@@ -0,0 +1 @@\n+hello\n"
        )
        self.assertEqual(self.apply_one(text, None), b"hello\n")

    def test_delete_file(self):
        text = "--- source/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-first\n-second\n"
        parsed = parse_patch(text)[0]
        self.assertIsNone(parsed.new_path)
        self.assertIsNone(apply_file_patch(parsed, b"first\nsecond\n"))

    def test_git_delete_regular_file_mode(self):
        text = (
            "diff --git a/source/old.py b/source/old.py\n"
            "deleted file mode 100644\nindex 1234567..0000000\n"
            "--- a/source/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-hello\n"
        )
        self.assertIsNone(self.apply_one(text, b"hello\n"))

    def test_modify_file_to_empty_is_not_deletion(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -1 +0,0 @@\n-old\n"
        self.assertEqual(self.apply_one(text, b"old\n"), b"")

    def test_modify_existing_empty_file(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -0,0 +1 @@\n+new\n"
        self.assertEqual(self.apply_one(text, b""), b"new\n")

    def test_insert_at_start_middle_and_end(self):
        old = "first\nsecond\nthird\n"
        for new in (
            "new\n" + old,
            "first\nnew\nsecond\nthird\n",
            old + "new\n",
        ):
            with self.subTest(new=new):
                self.assertEqual(self.apply_one(diff(old, new), old.encode()), new.encode())

    def test_zero_context_insertion_positions(self):
        old = b"first\nsecond\nthird\n"
        for position in range(4):
            text = f"--- source/app.py\n+++ source/app.py\n@@ -{position},0 +{position + 1} @@\n+new\n"
            lines = old.splitlines(keepends=True)
            lines.insert(position, b"new\n")
            with self.subTest(position=position):
                self.assertEqual(self.apply_one(text, old), b"".join(lines))

    def test_zero_context_deletion_positions(self):
        old = b"first\nsecond\nthird\n"
        for position, value in enumerate(("first", "second", "third")):
            text = f"--- source/app.py\n+++ source/app.py\n@@ -{position + 1} +{position},0 @@\n-{value}\n"
            lines = old.splitlines(keepends=True)
            del lines[position]
            with self.subTest(position=position):
                self.assertEqual(self.apply_one(text, old), b"".join(lines))

    def test_multiple_hunks_adjusted_positions(self):
        old = "".join(f"line {i}\n" for i in range(30))
        new = old.replace("line 1\n", "replace\nextra\n").replace("line 28\n", "ending\n")
        self.assertEqual(self.apply_one(diff(old, new), old.encode()), new.encode())

    def test_multifile_modify_add_delete(self):
        text = single() + (
            "--- /dev/null\n+++ source/new.py\n@@ -0,0 +1 @@\n+new\n"
            "--- source/dead.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-dead\n"
        )
        patches = parse_patch(text)
        self.assertEqual(tuple(item.path for item in patches), ("source/app.py", "source/new.py", "source/dead.py"))
        self.assertEqual(apply_file_patch(patches[0], b"old\n"), b"new\n")
        self.assertEqual(apply_file_patch(patches[1], None), b"new\n")
        self.assertIsNone(apply_file_patch(patches[2], b"dead\n"))

    def test_no_newline_markers_for_both_sides(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        self.assertEqual(self.apply_one(text, b"old"), b"new")

    def test_add_or_remove_final_newline_exactly(self):
        add = "--- source/app.py\n+++ source/app.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+old\n"
        remove = "--- source/app.py\n+++ source/app.py\n@@ -1 +1 @@\n-old\n+old\n\\ No newline at end of file\n"
        self.assertEqual(self.apply_one(add, b"old"), b"old\n")
        self.assertEqual(self.apply_one(remove, b"old\n"), b"old")

    def test_add_and_delete_unterminated_file(self):
        add = "--- /dev/null\n+++ source/new.py\n@@ -0,0 +1 @@\n+new\n\\ No newline at end of file\n"
        delete = "--- source/new.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-new\n\\ No newline at end of file\n"
        self.assertEqual(self.apply_one(add, None), b"new")
        self.assertIsNone(self.apply_one(delete, b"new"))

    def test_final_context_without_newline(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -1,2 +1,2 @@\n-old\n+new\n final\n\\ No newline at end of file\n"
        self.assertEqual(self.apply_one(text, b"old\nfinal"), b"new\nfinal")

    def test_terminal_no_newline_marker_may_omit_its_own_lf(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file"
        self.assertEqual(self.apply_one(text, b"old\n"), b"new")

    def test_crlf_file_body_and_control_chars_are_not_normalized(self):
        old = b"old\r\ncontrol\x0b\x0c\r\n"
        text = "--- source/app.py\n+++ source/app.py\n@@ -1,2 +1,2 @@\n-old\r\n+new\r\n control\x0b\x0c\r\n"
        self.assertEqual(self.apply_one(text, old), b"new\r\ncontrol\x0b\x0c\r\n")

    def test_literal_diff_looking_code_is_not_metadata(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -1,2 +1,2 @@\n--- strange\n-diff --git x y\n+++ changed\n+diff --git dangerous\n"
        self.assertEqual(self.apply_one(text, b"-- strange\ndiff --git x y\n"), b"++ changed\ndiff --git dangerous\n")

    def test_paths_are_lexically_authorized(self):
        invalid = (
            "/source/app.py", "../source/app.py", "source/../app.py",
            "source/./app.py", "source//app.py", "source/app.py/", "source",
            "source\\app.py", "C:/source/app.py", "file://source/app.py",
            "snapshots/app.py", "planning/app.py", "outputs/qa/test.py",
            "source/.git/config", "source/.env", "source/.ENV.local",
            "source/secrets/token", "source/credentials.json", "source/key.PEM",
            "source/" + "x" * 4096,
        )
        for path in invalid:
            with self.subTest(path=path):
                self.assert_error(lambda: parse_patch(single(path=path)), "PATH_DENIED")

    def test_invalid_second_header_is_also_authorized(self):
        text = single().replace("+++ source/app.py", "+++ source/.env")
        self.assert_error(lambda: parse_patch(text), "PATH_DENIED")

    def test_unsafe_optional_git_header_is_not_ignored(self):
        for header in (
            "diff --git a/../outside b/source/app.py\n",
            "diff --git a/source/.env b/source/.env\n",
        ):
            with self.subTest(header=header):
                self.assert_error(lambda: parse_patch(header + single()), "PATH_DENIED")

    def test_renames_duplicate_paths_and_git_path_mismatch_rejected(self):
        values = (
            single().replace("+++ source/app.py", "+++ source/other.py"),
            "diff --git a/source/one.py b/source/two.py\n" + single(),
            "diff --git a/source/other.py b/source/other.py\n" + single(),
            single() + single(),
        )
        for text in values:
            with self.subTest(text=text):
                self.assert_error(lambda: parse_patch(text))

    def test_mode_binary_and_other_metadata_rejected(self):
        headers = (
            "old mode 100644\nnew mode 100755\n",
            "new file mode 100755\n", "deleted file mode 100755\n",
            "similarity index 100%\nrename from source/old.py\nrename to source/new.py\n",
            "GIT binary patch\nliteral 4\n", "Binary files a/x and b/x differ\n",
            "index unknown..hash\n", "index 1234567..abcdef1 120000\n",
            "index 1234567..abcdef1\nindex 1234567..abcdef1\n",
        )
        for metadata in headers:
            text = "diff --git a/source/app.py b/source/app.py\n" + metadata + single()
            with self.subTest(metadata=metadata):
                self.assert_error(lambda: parse_patch(text))

    def test_mode_metadata_must_match_null_header_semantics(self):
        for mode in ("new file mode 100644\n", "deleted file mode 100644\n"):
            text = "diff --git a/source/app.py b/source/app.py\n" + mode + single()
            self.assert_error(lambda: parse_patch(text))

    def test_mode_index_cannot_conflict_with_regular_add_or_delete_metadata(self):
        added = (
            "diff --git a/source/new.py b/source/new.py\n"
            "new file mode 100644\nindex 0000000..1234567 100755\n"
            "--- /dev/null\n+++ b/source/new.py\n@@ -0,0 +1 @@\n+new\n"
        )
        deleted = (
            "diff --git a/source/old.py b/source/old.py\n"
            "deleted file mode 100644\nindex 1234567..0000000 100755\n"
            "--- a/source/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        )
        for text in (added, deleted):
            self.assert_error(lambda: parse_patch(text))

    def test_quoted_git_names_and_timestamp_headers_not_interpreted(self):
        values = (
            'diff --git "a/source/app.py" "b/source/app.py"\n' + single(),
            single().replace("--- source/app.py", '--- "source/app.py"'),
            single().replace("--- source/app.py", "--- source/app.py\t2026-10-08 00:00:00"),
        )
        for text in values:
            with self.subTest(text=text):
                self.assert_error(lambda: parse_patch(text))

    def test_no_preamble_trailing_noise_or_partial_diff(self):
        for text in ("", "hello\n" + single(), single() + "\n", "--- source/app.py\n", single()[:-1], single().replace("@@ -1 +1 @@", "@@@ -1 +1 @@@")):
            with self.subTest(text=text):
                self.assert_error(lambda: parse_patch(text))

    def test_malformed_hunk_counts_and_headers_rejected(self):
        for header in (
            "@@ -0 +1 @@", "@@ -1 +0 @@", "@@ -0,0 +0,0 @@",
            "@@ -1,2 +1 @@", "@@ -1 +1,2 @@", "@@ -1,0 +1 @@",
            "@@ --1 +1 @@", "@@ -10000000 +1 @@", "@@ -1048578 +1 @@",
            "@@ -1 +1 @", "@@ -1 +1 @@suffix",
        ):
            with self.subTest(header=header):
                self.assert_error(lambda: parse_patch(single().replace("@@ -1 +1 @@", header)))

    def test_empty_context_only_and_extra_body_rejected(self):
        values = (
            "--- /dev/null\n+++ /dev/null\n@@ -1 +1 @@\n-old\n+new\n",
            "--- source/app.py\n+++ source/app.py\n@@ -1 +1 @@\n same\n",
            single() + "+extra\n",
            single().replace("-old\n", "?old\n"),
        )
        for text in values:
            self.assert_error(lambda: parse_patch(text))

    def test_hunk_heading_is_allowed_but_not_a_command(self):
        text = single().replace("@@ -1 +1 @@", "@@ -1 +1 @@ function($VALUE)")
        with mock_patch("subprocess.run", side_effect=AssertionError("must not execute")), mock_patch("os.open", side_effect=AssertionError("must not open")):
            self.assertEqual(self.apply_one(text, b"old\n"), b"new\n")

    def test_hunk_content_mismatch_never_searches_or_fuzzes(self):
        parsed = parse_patch(single())[0]
        for original in (b"other\n", b"other\nold\n", b"old\r\n", b"old", b" old\n"):
            with self.subTest(original=original):
                self.assert_error(lambda: apply_file_patch(parsed, original))

    def test_new_hunk_position_is_verified(self):
        parsed = parse_patch(single().replace("+1 @@", "+2 @@"))[0]
        self.assert_error(lambda: apply_file_patch(parsed, b"old\n"))

    def test_old_hunk_position_is_verified(self):
        parsed = parse_patch(single().replace("-1 +", "-2 +"))[0]
        self.assert_error(lambda: apply_file_patch(parsed, b"old\n"))

    def test_overlapping_or_out_of_order_hunks_rejected(self):
        header = "--- source/app.py\n+++ source/app.py\n"
        for body in (
            "@@ -1 +1 @@\n-old\n+new\n@@ -1 +2 @@\n-old\n+new\n",
            "@@ -2 +2 @@\n-last\n+new\n@@ -1 +1 @@\n-first\n+new\n",
        ):
            parsed = parse_patch(header + body)[0]
            self.assert_error(lambda: apply_file_patch(parsed, b"first\nlast\n"))

    def test_add_cannot_replace_existing_file_and_modify_requires_original(self):
        added = parse_patch("--- /dev/null\n+++ source/new.py\n@@ -0,0 +1 @@\n+new\n")[0]
        self.assert_error(lambda: apply_file_patch(added, b""))
        self.assert_error(lambda: apply_file_patch(parse_patch(single())[0], None))

    def test_delete_requires_complete_original_removal(self):
        parsed = parse_patch("--- source/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n")[0]
        self.assert_error(lambda: apply_file_patch(parsed, b"old\ntail\n"))

    def test_no_newline_markers_are_strict(self):
        values = (
            single().replace("-old\n", "\\ No newline at end of file\n-old\n"),
            single() + "\\ No newline at end of file\n\\ No newline at end of file\n",
            single() + "\\ No newline at end of file BAD\n",
            single().replace("+new\n", "+\n\\ No newline at end of file\n"),
        )
        for text in values:
            with self.subTest(text=text):
                self.assert_error(lambda: parse_patch(text))

    def test_no_newline_marker_may_not_put_unterminated_line_before_more_output(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -1 +1,2 @@\n-old\n+new\n\\ No newline at end of file\n+tail\n"
        self.assert_error(lambda: apply_file_patch(parse_patch(text)[0], b"old\n"))

    def test_no_newline_context_cannot_describe_non_eof(self):
        text = "--- source/app.py\n+++ source/app.py\n@@ -1,2 +1,2 @@\n same\n\\ No newline at end of file\n-old\n+new\n"
        self.assert_error(lambda: apply_file_patch(parse_patch(text)[0], b"same\nold\n"))

    def test_binary_and_invalid_utf8_inputs_rejected_without_exception_body(self):
        for text in (single(new="\x00secret"), single(new="\ud800secret")):
            self.assert_error(lambda: parse_patch(text))
        parsed = parse_patch(single())[0]
        for original in (b"\xff", b"old\x00\n", bytearray(b"old\n"), "old\n"):
            self.assert_error(lambda: apply_file_patch(parsed, original))

    def test_patch_input_byte_limit_applies_to_utf8_not_characters(self):
        text = single(new="가" * (MAX_FILE_BYTES // 3 + 1))
        self.assertLess(len(text), MAX_FILE_BYTES)
        self.assert_error(lambda: parse_patch(text))

    def test_original_and_output_limits(self):
        self.assert_error(lambda: apply_file_patch(parse_patch(single())[0], b"x" * (MAX_FILE_BYTES + 1)))
        parsed = parse_patch("--- source/app.py\n+++ source/app.py\n@@ -0,0 +1 @@\n+new\n")[0]
        self.assert_error(lambda: apply_file_patch(parsed, b"x" * (MAX_FILE_BYTES - 1) + b"\n"))

    def test_finite_file_hunk_and_line_limits(self):
        text = "".join(single(path=f"source/f{index}.py") for index in range(MAX_PATCH_FILES + 1))
        self.assert_error(lambda: parse_patch(text))
        text = "--- source/app.py\n+++ source/app.py\n" + "@@ -1 +1 @@\n-old\n+new\n" * (MAX_PATCH_HUNKS + 1)
        self.assert_error(lambda: parse_patch(text))
        self.assert_error(lambda: parse_patch("\n" * (MAX_PATCH_LINES + 1)))

    def test_parsed_values_are_frozen_and_hide_source_repr(self):
        parsed = parse_patch(single(new="private-source-text"))[0]
        self.assertNotIn("private-source-text", repr(parsed))
        self.assertNotIn("private-source-text", repr(parsed.hunks[0]))
        self.assertNotIn("private-source-text", repr(parsed.hunks[0].lines[-1]))
        with self.assertRaises(AttributeError):
            parsed.path = "source/other.py"

    def test_public_apply_revalidates_forged_dataclasses(self):
        valid = parse_patch(single())[0]
        malformed = (
            replace(valid, path="source/.env"),
            replace(valid, old_path="source/other.py"),
            replace(valid, old_path=None, new_path=None),
            replace(valid, hunks=[]),
            replace(valid, hunks=()),
            replace(valid, hunks=(replace(valid.hunks[0], old_count=True),)),
            replace(valid, hunks=(replace(valid.hunks[0], lines=(PatchLine("?", b"old\n"),)),)),
            replace(valid, hunks=(replace(valid.hunks[0], lines=(PatchLine("-", b"old\n"), PatchLine("+", b"new\nmore\n"))),)),
            replace(valid, hunks=(replace(valid.hunks[0], lines=(PatchLine("-", b"old\n"), PatchLine("+", b""))),)),
            replace(valid, hunks=(replace(valid.hunks[0], lines=(PatchLine("-", b"old\n"), PatchLine("+", b"\xff\n"))),)),
        )
        for index, parsed in enumerate(malformed):
            with self.subTest(index=index):
                self.assert_error(lambda: apply_file_patch(parsed, b"old\n"), "PATH_DENIED" if index == 0 else "PATCH_FAILED")

    def test_representative_edit_combinations_match_difflib(self):
        old_lines = [f"line {index}\n" for index in range(12)]
        for position in range(len(old_lines)):
            for deleted in range(4):
                new_lines = old_lines.copy()
                new_lines[position:position + deleted] = ["inserted 한글\n"]
                old = "".join(old_lines)
                new = "".join(new_lines)
                with self.subTest(position=position, deleted=deleted):
                    self.assertEqual(self.apply_one(diff(old, new), old.encode()), new.encode())


if __name__ == "__main__":
    unittest.main()

"""`commontrace lesson edit`: Markdown-first editing that keeps governance intact."""
import os
import sys

from commontrace import frontmatter, lesson_io
from commontrace.cli import main

FILLED = """## Rule
Retry an upload that failed with 503 using exponential backoff and jitter.

## Why
Twelve traces show transient 503s that succeeded on a later attempt.

## How to apply
Back off 1s, 2s, 4s with jitter; stop after five attempts.

## Counter-examples
A 400 response means the payload is invalid; retrying will not help.
"""


def _store(tmp_path):
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    assert main(["lesson", "new", "--dest", root, "--slug", "lesson_edit_me", "--description",
                 "Retry uploads with backoff", "--domain", "other", "--agent-type", "general",
                 "--applies-when", "an upload fails with 503", "--do-not-apply-when", "the payload is invalid",
                 "--importance", "3", "--importance-rationale", "common"]) == 0
    return root


def _editor(tmp_path, code):
    script = tmp_path / "fake_editor.py"
    script.write_text("import sys\npath = sys.argv[1]\ntext = open(path).read()\n" + code
                      + "\nopen(path, 'w').write(text)\n")
    return f"{sys.executable} {script}"


def _replace_body(new_body):
    return f"head, _sep, _old = text.partition('\\n---\\n')\ntext = head + '\\n---\\n\\n' + {new_body!r}"


def test_body_edit_is_written_and_journaled(tmp_path, capsys):
    root = _store(tmp_path)
    assert main(["lesson", "edit", "edit_me", "--dest", root, "--editor", _editor(tmp_path, _replace_body(FILLED))]) == 0
    fm, body = frontmatter.read(lesson_io.lesson_path(root, "edit_me"))
    assert "exponential backoff" in body and fm["status"] == "review"
    history = lesson_io.history(root, "lesson_edit_me")
    assert history and history[-1]["reason"] == "edited by hand"


def test_unchanged_save_writes_nothing(tmp_path, capsys):
    root = _store(tmp_path)
    before = len(lesson_io.read_revisions(root)[0])
    assert main(["lesson", "edit", "edit_me", "--dest", root, "--editor", _editor(tmp_path, "")]) == 0
    assert "No content change" in capsys.readouterr().out
    assert len(lesson_io.read_revisions(root)[0]) == before


def test_status_cannot_be_changed_by_hand(tmp_path, capsys):
    root = _store(tmp_path)
    code = "text = text.replace('status: review', 'status: active')"
    assert main(["lesson", "edit", "edit_me", "--dest", root, "--editor", _editor(tmp_path, code)]) == 1
    assert "cannot change status" in capsys.readouterr().err
    assert frontmatter.read(lesson_io.lesson_path(root, "edit_me"))[0]["status"] == "review"


def test_secret_in_the_edit_is_refused(tmp_path, capsys):
    root = _store(tmp_path)
    leaked = FILLED + "\nKey: AKIA" + "IOSFODNN7EXAMPLE\n"
    assert main(["lesson", "edit", "edit_me", "--dest", root, "--editor", _editor(tmp_path, _replace_body(leaked))]) == 1
    assert "AKIA" not in open(lesson_io.lesson_path(root, "edit_me")).read()


def test_editing_an_active_lesson_returns_it_to_review(tmp_path, capsys):
    root = _store(tmp_path)
    assert main(["lesson", "edit", "edit_me", "--dest", root, "--editor", _editor(tmp_path, _replace_body(FILLED))]) == 0
    assert main(["lesson", "approve", "edit_me", "--dest", root, "--rationale", "reviewed"]) == 0
    assert frontmatter.read(lesson_io.lesson_path(root, "edit_me"))[0]["status"] == "active"
    capsys.readouterr()
    changed = FILLED.replace("five attempts", "three attempts")
    assert main(["lesson", "edit", "edit_me", "--dest", root, "--editor", _editor(tmp_path, _replace_body(changed))]) == 0
    assert "back in review" in capsys.readouterr().out
    fm, body = frontmatter.read(lesson_io.lesson_path(root, "edit_me"))
    assert fm["status"] == "review" and "three attempts" in body


def test_failed_editor_and_missing_lesson(tmp_path, capsys):
    root = _store(tmp_path)
    assert main(["lesson", "edit", "edit_me", "--dest", root, "--editor", f"{sys.executable} -c 'import sys; sys.exit(3)'"]) == 1
    assert main(["lesson", "edit", "nope", "--dest", root, "--editor", "true"]) == 1
    leftovers = [n for n in os.listdir(os.path.join(root, "memory")) if n.startswith("lesson-edit-")]
    assert leftovers == []

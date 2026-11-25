"""QA B27: writes whose text needs a newer Python than the server's parser are refused with a specific message."""

from __future__ import annotations

import sys

import pytest

from codenav_mcp.pysource import PythonSyntaxError, check_syntax


def _refusal(text: str, filename: str) -> str:
	with pytest.raises(PythonSyntaxError) as info:
		check_syntax(text, filename)
	return str(info.value)


def _assert_refusal_shape(message: str, filename: str) -> None:
	v = sys.version_info
	assert message.startswith(f"{filename}: not written: it does not parse with this server's Python ")
	assert f"Python {v.major}.{v.minor}.{v.micro} parser" in message
	assert "run the server under a newer Python (3.14 or later)" in message
	assert "\n" not in message
	assert "Traceback" not in message


@pytest.mark.skipif(sys.version_info >= (3, 13), reason="this interpreter parses PEP 696 defaults")
def test_b27_pep696_type_parameter_default_is_refused_with_parser_version():
	# given — a type parameter default is Python 3.13 syntax
	text = "class Box[T = int]:\n\tpass\n"
	# when
	message = _refusal(text, "pkg/box.py")
	# then
	_assert_refusal_shape(message, "pkg/box.py")
	assert "(line 1: " in message
	assert "PEP 696" in message


@pytest.mark.skipif(sys.version_info >= (3, 14), reason="this interpreter parses the 3.14 syntax")
def test_b27_except_without_parentheses_is_refused_with_parser_version():
	# given — `except A, B:` without parentheses is Python 3.14 syntax (PEP 758)
	text = "try:\n\tpass\nexcept A, B:\n\tpass\n"
	# when
	message = _refusal(text, "lib/handlers.py")
	# then
	_assert_refusal_shape(message, "lib/handlers.py")
	assert "(line 3: " in message
	assert "PEP 758" in message


@pytest.mark.skipif(sys.version_info >= (3, 14), reason="this interpreter parses t-strings")
def test_b27_t_string_is_refused_with_parser_version():
	# given — template strings (PEP 750) are Python 3.14 syntax
	text = 'name = "x"\nmessage = t"hello {name}"\n'
	# when
	message = _refusal(text, "templates.py")
	# then
	_assert_refusal_shape(message, "templates.py")
	assert "(line 2: " in message
	assert 'a `t"..."` string' in message


def test_b27_real_syntax_error_gets_the_same_refusal_and_its_line():
	# given — an ordinary error, not newer syntax
	text = "x = 1\ndef broken(:\n\tpass\n"
	# when
	message = _refusal(text, "pkg/lib.py")
	# then
	_assert_refusal_shape(message, "pkg/lib.py")
	assert "(line 2: " in message
	assert "Otherwise fix the syntax error at line 2." in message

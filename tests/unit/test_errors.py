import pytest

from kritamcp.errors import HTTP_STATUS, CommandError


def test_code_message_details():
    e = CommandError("ambiguous", "two layers named 'Hair'", {"matches": ["{a}", "{b}"]})
    assert (e.code, e.message, str(e)) == ("ambiguous", "two layers named 'Hair'", "two layers named 'Hair'")
    assert e.http_status == 409
    assert e.to_dict() == {"code": "ambiguous", "message": "two layers named 'Hair'",
                           "details": {"matches": ["{a}", "{b}"]}}


def test_one_argument_form_is_gone():
    with pytest.raises(TypeError):
        CommandError("No path specified")


def test_unknown_code_rejected():
    with pytest.raises(ValueError):
        CommandError("nope", "x")


def test_status_table_matches_d2():
    assert HTTP_STATUS == {"bad_request": 400, "forbidden_path": 403, "not_found": 404,
                           "ambiguous": 409, "conflict": 409, "too_large": 413,
                           "krita_error": 500, "timeout": 504}

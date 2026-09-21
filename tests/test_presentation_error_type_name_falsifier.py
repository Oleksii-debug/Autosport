from autosport.presentation_error import safe_exception_text


_SECRET = "TYPE-NAME-SECRET-7f31"


def test_hostile_exception_class_name_cannot_reach_operator_text() -> None:
    hostile_type = type(
        f"ProviderError_{_SECRET}\nAuthorization",
        (RuntimeError,),
        {},
    )

    rendered = safe_exception_text(hostile_type("message detail is intentionally irrelevant"))

    assert _SECRET not in rendered
    assert "Authorization" not in rendered
    assert "\n" not in rendered

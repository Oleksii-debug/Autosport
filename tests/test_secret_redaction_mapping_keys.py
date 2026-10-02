from __future__ import annotations

from autosport.secret_redaction import REDACTED, redact_operator_value


def test_explicit_secret_embedded_in_mapping_key_is_redacted() -> None:
    secret = "AS-MAPPING-KEY-SECRET-7f31"
    payload = {
        f"provider-{secret}-error": {
            "status": "rejected",
        }
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == {
        f"provider-{REDACTED}-error": {
            "status": "rejected",
        }
    }
    assert secret not in repr(redacted)


def test_environment_secret_used_as_mapping_key_is_redacted(
    monkeypatch,
) -> None:
    secret = "AS-MAPPING-ENV-SECRET-291c"
    monkeypatch.setenv("AUTOSPORT_TEST_API_KEY", secret)

    redacted = redact_operator_value({secret: "provider-error"})

    assert redacted == {REDACTED: "provider-error"}
    assert secret not in repr(redacted)


def test_sensitive_label_contract_is_preserved_when_key_is_not_secret() -> None:
    redacted = redact_operator_value(
        {
            "api_key": "provider-secret",
            "ordinary_key": "ordinary-value",
        }
    )

    assert redacted == {
        "api_key": REDACTED,
        "ordinary_key": "ordinary-value",
    }

def test_redacted_mapping_keys_do_not_collapse_or_overwrite_ordinary_keys() -> None:
    first = "AS-MAPPING-COLLISION-FIRST-61d2"
    second = "AS-MAPPING-COLLISION-SECOND-93a4"
    payload = {
        first: "first-record",
        second: "second-record",
        REDACTED: "ordinary-record",
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(first, second),
    )

    assert redacted[REDACTED] == "ordinary-record"
    assert redacted[f"{REDACTED}#2"] == "first-record"
    assert redacted[f"{REDACTED}#3"] == "second-record"
    assert len(redacted) == 3
    assert first not in repr(redacted)
    assert second not in repr(redacted)


def test_same_redacted_embedded_key_spelling_preserves_both_records() -> None:
    first = "AS-MAPPING-INNER-FIRST-d481"
    second = "AS-MAPPING-INNER-SECOND-a172"
    payload = {
        f"provider-{first}-error": "first-record",
        f"provider-{second}-error": "second-record",
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(first, second),
    )

    base = f"provider-{REDACTED}-error"
    assert redacted[base] == "first-record"
    assert redacted[f"{base}#2"] == "second-record"
    assert len(redacted) == 2
    assert first not in repr(redacted)
    assert second not in repr(redacted)


def test_configured_secret_embedded_in_bytes_mapping_key_is_redacted() -> None:
    secret = "AS-MAPPING-BYTES-SECRET-7f31"
    payload = {
        f"provider-{secret}-error".encode("utf-8"): "provider-error",
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == {f"provider-{REDACTED}-error": "provider-error"}
    assert secret not in repr(redacted)


def test_bytes_sensitive_label_redacts_its_value() -> None:
    redacted = redact_operator_value({b"api_key": "provider-secret"})

    assert redacted == {"api_key": REDACTED}
    assert "provider-secret" not in repr(redacted)


def test_invalid_utf8_bytes_mapping_key_fails_closed() -> None:
    redacted = redact_operator_value({b"\xffapi_key": "provider-secret"})

    assert redacted == {REDACTED: REDACTED}
    assert "provider-secret" not in repr(redacted)
    assert "\\xff" not in repr(redacted)


def test_bytes_presentation_key_cannot_overwrite_ordinary_string_key() -> None:
    payload = {
        REDACTED: "ordinary-record",
        REDACTED.encode("utf-8"): "bytes-record",
    }

    redacted = redact_operator_value(payload)

    assert redacted == {
        REDACTED: "ordinary-record",
        f"{REDACTED}#2": "bytes-record",
    }
    assert len(redacted) == 2

def test_configured_secret_inside_tuple_mapping_key_is_redacted() -> None:
    secret = "AS-MAPPING-TUPLE-SECRET-91d7"

    redacted = redact_operator_value(
        {(secret,): "provider-error"},
        extra_secret_values=(secret,),
    )

    assert redacted == {(REDACTED,): "provider-error"}
    assert secret not in repr(redacted)


def test_tuple_mapping_key_recursively_redacts_bytes_and_sensitive_labels() -> None:
    secret = "AS-MAPPING-TUPLE-BYTES-a771"
    key = (f"provider-{secret}".encode("utf-8"), "api_key")

    redacted = redact_operator_value(
        {key: "provider-secret"},
        extra_secret_values=(secret,),
    )

    assert redacted == {(f"provider-{REDACTED}", "api_key"): REDACTED}
    assert secret not in repr(redacted)
    assert "provider-secret" not in repr(redacted)


def test_colliding_redacted_tuple_keys_preserve_both_records_without_secret() -> None:
    first = "AS-TUPLE-COLLISION-FIRST-c881"
    second = "AS-TUPLE-COLLISION-SECOND-e272"

    redacted = redact_operator_value(
        {
            (first,): "first-record",
            (second,): "second-record",
        },
        extra_secret_values=(first, second),
    )

    assert redacted[(REDACTED,)] == "first-record"
    assert redacted[((REDACTED,), 2)] == "second-record"
    assert len(redacted) == 2
    assert first not in repr(redacted)
    assert second not in repr(redacted)


def test_configured_secret_embedded_in_bytes_value_is_redacted() -> None:
    secret = "AS-MAPPING-BYTES-VALUE-SECRET-4a12"
    payload = {
        "provider_detail": f"provider-{secret}-error".encode("utf-8"),
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == {
        "provider_detail": f"provider-{REDACTED}-error".encode("utf-8"),
    }
    assert secret.encode("utf-8") not in redacted["provider_detail"]


def test_invalid_utf8_bytes_value_fails_closed_without_raw_bytes() -> None:
    payload = {"provider_detail": b"prefix-\xff-secret"}

    redacted = redact_operator_value(payload)

    assert redacted == {"provider_detail": REDACTED.encode("utf-8")}
    assert b"\xff" not in redacted["provider_detail"]


def test_configured_secret_embedded_in_bytearray_value_is_redacted() -> None:
    secret = "AS-MAPPING-BYTEARRAY-VALUE-SECRET-7c21"
    payload = {
        "provider_detail": bytearray(f"provider-{secret}-error".encode("utf-8")),
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == {
        "provider_detail": bytearray(f"provider-{REDACTED}-error".encode("utf-8")),
    }
    assert secret.encode("utf-8") not in bytes(redacted["provider_detail"])


def test_invalid_utf8_bytearray_value_fails_closed_without_raw_bytes() -> None:
    payload = {"provider_detail": bytearray(b"prefix-\xff-secret")}

    redacted = redact_operator_value(payload)

    assert redacted == {
        "provider_detail": bytearray(REDACTED.encode("utf-8")),
    }
    assert b"\xff" not in bytes(redacted["provider_detail"])


def test_configured_secret_embedded_in_memoryview_value_is_redacted() -> None:
    secret = "AS-MAPPING-MEMORYVIEW-VALUE-SECRET-90af"
    payload = {
        "provider_detail": memoryview(f"provider-{secret}-error".encode("utf-8")),
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    rendered = bytes(redacted["provider_detail"])
    assert rendered == f"provider-{REDACTED}-error".encode("utf-8")
    assert secret.encode("utf-8") not in rendered


def test_invalid_utf8_memoryview_value_fails_closed_without_raw_bytes() -> None:
    payload = {"provider_detail": memoryview(b"prefix-\xff-secret")}

    redacted = redact_operator_value(payload)

    assert bytes(redacted["provider_detail"]) == REDACTED.encode("utf-8")
    assert b"\xff" not in bytes(redacted["provider_detail"])


def test_configured_secret_embedded_in_set_value_is_redacted() -> None:
    secret = "AS-MAPPING-SET-VALUE-SECRET-26b1"
    payload = {
        "provider_detail": {
            "ordinary",
            f"provider-{secret}-error",
        },
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == {
        "provider_detail": {
            "ordinary",
            f"provider-{REDACTED}-error",
        },
    }
    assert secret not in repr(redacted)


def test_configured_secret_embedded_in_frozenset_value_is_redacted() -> None:
    secret = "AS-MAPPING-FROZENSET-VALUE-SECRET-1e44"
    payload = {
        "provider_detail": frozenset(
            {
                "ordinary",
                f"provider-{secret}-error",
            }
        ),
    }

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == {
        "provider_detail": frozenset(
            {
                "ordinary",
                f"provider-{REDACTED}-error",
            }
        ),
    }
    assert secret not in repr(redacted)


def test_self_referential_list_value_fails_closed() -> None:
    payload: list[object] = []
    payload.append(payload)

    redacted = redact_operator_value(payload)

    assert redacted == [REDACTED]


def test_self_referential_mapping_value_fails_closed() -> None:
    payload: dict[str, object] = {}
    payload["self"] = payload

    redacted = redact_operator_value(payload)

    assert redacted == {"self": REDACTED}


def test_secret_bearing_cyclic_list_redacts_secret_and_cycle() -> None:
    secret = "AS-CYCLIC-VALUE-SECRET-61bd"
    payload: list[object] = [f"provider-{secret}-error"]
    payload.append(payload)

    redacted = redact_operator_value(
        payload,
        extra_secret_values=(secret,),
    )

    assert redacted == [f"provider-{REDACTED}-error", REDACTED]
    assert secret not in repr(redacted)


def test_deep_list_value_exhausts_depth_budget_fail_closed() -> None:
    payload: object = "ordinary-leaf"
    for _ in range(release_depth := 80):
        payload = [payload]

    redacted = redact_operator_value(payload)

    current = redacted
    observed_depth = 0
    while isinstance(current, list):
        assert len(current) == 1
        current = current[0]
        observed_depth += 1
    assert observed_depth < release_depth
    assert current == REDACTED


def test_wide_list_value_exhausts_node_budget_fail_closed() -> None:
    payload = list(range(10_100))

    redacted = redact_operator_value(payload)

    assert redacted == REDACTED


class _HostileMappingKey:
    def __hash__(self) -> int:
        return 17731

    def __str__(self) -> str:
        raise AssertionError("custom mapping key __str__ must not run")

    def __repr__(self) -> str:
        raise AssertionError("custom mapping key __repr__ must not run")


def test_unsupported_hashable_mapping_key_fails_closed_without_rendering_it() -> None:
    hostile_key = _HostileMappingKey()

    redacted = redact_operator_value({hostile_key: "provider-secret"})

    assert redacted == {REDACTED: REDACTED}
    assert "provider-secret" not in repr(redacted)


def test_tuple_with_unsupported_component_fails_closed_without_rendering_component() -> None:
    hostile_key = _HostileMappingKey()

    redacted = redact_operator_value({("provider", hostile_key): "provider-secret"})

    assert redacted == {("provider", REDACTED): REDACTED}
    assert "provider-secret" not in repr(redacted)



def test_deep_tuple_mapping_key_exhausts_depth_budget_fail_closed() -> None:
    key: object = "ordinary-leaf"
    for _ in range(1_500):
        key = (key,)

    redacted = redact_operator_value({key: "provider-secret"})

    assert redacted == {REDACTED: REDACTED}
    assert "provider-secret" not in repr(redacted)


def test_wide_tuple_mapping_key_exhausts_node_budget_fail_closed() -> None:
    key = tuple(range(300))

    redacted = redact_operator_value({key: "provider-secret"})

    assert redacted == {REDACTED: REDACTED}
    assert "provider-secret" not in repr(redacted)


def test_shallow_builtin_tuple_mapping_key_preserves_structure() -> None:
    payload = {("market", 7, None, True, 1.5, 2 + 3j): "ordinary-value"}

    redacted = redact_operator_value(payload)

    assert redacted == payload


class _HostileStructuredValue:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def __str__(self) -> str:
        raise AssertionError("custom structured value __str__ must not run")

    def __repr__(self) -> str:
        raise AssertionError("custom structured value __repr__ must not run")


def test_unsupported_custom_mapping_value_fails_closed_without_rendering_it() -> None:
    secret = "AS-CUSTOM-VALUE-SECRET-81ad"
    value = _HostileStructuredValue(secret)

    redacted = redact_operator_value({"provider_detail": value})

    assert redacted == {"provider_detail": REDACTED}
    assert secret not in repr(redacted)


def test_unsupported_custom_values_fail_closed_inside_supported_containers() -> None:
    secret = "AS-CUSTOM-CONTAINER-VALUE-SECRET-9b42"
    value = _HostileStructuredValue(secret)

    redacted = redact_operator_value(
        {
            "list": [value],
            "tuple": (value,),
            "set": {value},
            "frozenset": frozenset({value}),
        }
    )

    assert redacted == {
        "list": [REDACTED],
        "tuple": (REDACTED,),
        "set": {REDACTED},
        "frozenset": frozenset({REDACTED}),
    }
    assert secret not in repr(redacted)


def test_exact_inert_builtin_scalar_values_remain_stable() -> None:
    payload = {
        "none": None,
        "bool": True,
        "int": 7,
        "float": 1.25,
        "complex": 2 + 3j,
    }

    assert redact_operator_value(payload) == payload
class _HostileDictValue(dict):
    def items(self):
        raise AssertionError("custom dict subclass items() must not run")


class _HostileListValue(list):
    def __iter__(self):
        raise AssertionError("custom list subclass __iter__ must not run")


class _HostileTupleValue(tuple):
    def __iter__(self):
        raise AssertionError("custom tuple subclass __iter__ must not run")


def test_custom_container_subclasses_fail_closed_without_dispatching_user_code() -> None:
    payload = {
        "mapping": _HostileDictValue({"api_key": "provider-secret"}),
        "list": _HostileListValue(["provider-secret"]),
        "tuple": _HostileTupleValue(("provider-secret",)),
    }

    redacted = redact_operator_value(payload)

    assert redacted == {
        "mapping": REDACTED,
        "list": REDACTED,
        "tuple": REDACTED,
    }
    assert "provider-secret" not in repr(redacted)

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


_LOADS_CODE_KEY = "_autosport_test_original_loads_code"
_LOADS_CALLED_KEY = "_autosport_test_hostile_loads_called"
_DUMP_CODE_KEY = "_autosport_test_original_dump_code"
_DUMP_CALLED_KEY = "_autosport_test_hostile_dump_called"
_NAMED_TEMP_CODE_KEY = "_autosport_test_original_named_temp_code"
_NAMED_TEMP_CALLED_KEY = "_autosport_test_hostile_named_temp_called"


def _hostile_loads(*args, **kwargs):
    namespace = globals()
    namespace[_LOADS_CALLED_KEY] = True
    loads.__code__ = namespace[_LOADS_CODE_KEY]
    return loads(*args, **kwargs)


def _hostile_dump(*args, **kwargs):
    namespace = globals()
    namespace[_DUMP_CALLED_KEY] = True
    dump.__code__ = namespace[_DUMP_CODE_KEY]
    return dump(*args, **kwargs)


def _hostile_named_temporary_file(*args, **kwargs):
    namespace = globals()
    namespace[_NAMED_TEMP_CALLED_KEY] = True
    NamedTemporaryFile.__code__ = namespace[_NAMED_TEMP_CODE_KEY]
    return NamedTemporaryFile(*args, **kwargs)


def _install_same_object_code_substitution(module, function_name: str, hostile_code, code_key: str, called_key: str):
    function = vars(module)[function_name]
    original_code = function.__code__
    vars(module)[code_key] = original_code
    vars(module)[called_key] = False
    function.__code__ = hostile_code
    return function, original_code


def _restore_same_object_code_substitution(module, function, original_code, code_key: str, called_key: str) -> bool:
    function.__code__ = original_code
    called = bool(vars(module).pop(called_key, False))
    vars(module).pop(code_key, None)
    return called


def test_path_load_rejects_same_object_json_loads_code_substitution(tmp_path: Path) -> None:
    """Identity-preserving json.loads code mutation must not reach snapshot parsing."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    function, original_code = _install_same_object_code_substitution(
        json,
        "loads",
        _hostile_loads.__code__,
        _LOADS_CODE_KEY,
        _LOADS_CALLED_KEY,
    )
    try:
        with pytest.raises(ValueError, match="executable|authority"):
            paper.PaperBook.load(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            json,
            function,
            original_code,
            _LOADS_CODE_KEY,
            _LOADS_CALLED_KEY,
        )

    assert hostile_called is False


def test_save_rejects_same_object_json_dump_code_before_durable_mutation(tmp_path: Path) -> None:
    """Serializer code mutation must fail before snapshot/witness publication begins."""

    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")

    function, original_code = _install_same_object_code_substitution(
        json,
        "dump",
        _hostile_dump.__code__,
        _DUMP_CODE_KEY,
        _DUMP_CALLED_KEY,
    )
    try:
        with pytest.raises(ValueError, match="executable|authority"):
            book.save(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            json,
            function,
            original_code,
            _DUMP_CODE_KEY,
            _DUMP_CALLED_KEY,
        )

    assert hostile_called is False
    assert not path.exists()
    assert not witness_path.exists()


def test_save_rejects_same_object_named_temporary_file_code_before_durable_mutation(
    tmp_path: Path,
) -> None:
    """The Python tempfile member used by canonical save is executable authority too."""

    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")

    function, original_code = _install_same_object_code_substitution(
        tempfile,
        "NamedTemporaryFile",
        _hostile_named_temporary_file.__code__,
        _NAMED_TEMP_CODE_KEY,
        _NAMED_TEMP_CALLED_KEY,
    )
    try:
        with pytest.raises(ValueError, match="executable|authority"):
            book.save(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            tempfile,
            function,
            original_code,
            _NAMED_TEMP_CODE_KEY,
            _NAMED_TEMP_CALLED_KEY,
        )

    assert hostile_called is False
    assert not path.exists()
    assert not witness_path.exists()

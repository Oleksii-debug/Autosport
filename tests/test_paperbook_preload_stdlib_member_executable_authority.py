from __future__ import annotations

import functools
import json
import os
import tempfile
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper


_LOADS_CODE_KEY = "_autosport_test_original_loads_code"
_LOADS_CALLED_KEY = "_autosport_test_hostile_loads_called"
_DUMP_CODE_KEY = "_autosport_test_original_dump_code"
_DUMP_CALLED_KEY = "_autosport_test_hostile_dump_called"
_DUMPS_CODE_KEY = "_autosport_test_original_dumps_code"
_DUMPS_CALLED_KEY = "_autosport_test_hostile_dumps_called"
_NAMED_TEMP_CODE_KEY = "_autosport_test_original_named_temp_code"
_NAMED_TEMP_CALLED_KEY = "_autosport_test_hostile_named_temp_called"
_MKSTEMP_CODE_KEY = "_autosport_test_original_mkstemp_code"
_MKSTEMP_CALLED_KEY = "_autosport_test_hostile_mkstemp_called"
_ABSPATH_CODE_KEY = "_autosport_test_original_abspath_code"
_ABSPATH_CALLED_KEY = "_autosport_test_hostile_abspath_called"
_DECODER_CODE_KEY = "_autosport_test_original_decoder_code"
_DECODER_CALLED_KEY = "_autosport_test_hostile_decoder_called"
_ENCODER_CODE_KEY = "_autosport_test_original_encoder_code"
_ENCODER_CALLED_KEY = "_autosport_test_hostile_encoder_called"
_WRAPPER_CODE_KEY = "_autosport_test_original_wrapper_enter_code"
_WRAPPER_CALLED_KEY = "_autosport_test_hostile_wrapper_enter_called"
_WRAPS_CODE_KEY = "_autosport_test_original_wraps_code"
_WRAPS_CALLED_KEY = "_autosport_test_hostile_wraps_called"
_HOSTILE_GLOBAL_GETATTR_CALLED = False


def _hostile_loads(*args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_loads_called"] = True
    loads.__code__ = namespace["_autosport_test_original_loads_code"]
    return loads(*args, **kwargs)


def _hostile_dump(*args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_dump_called"] = True
    dump.__code__ = namespace["_autosport_test_original_dump_code"]
    return dump(*args, **kwargs)


def _hostile_dumps(*args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_dumps_called"] = True
    dumps.__code__ = namespace["_autosport_test_original_dumps_code"]
    return dumps(*args, **kwargs)


def _hostile_named_temporary_file(*args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_named_temp_called"] = True
    NamedTemporaryFile.__code__ = namespace["_autosport_test_original_named_temp_code"]
    return NamedTemporaryFile(*args, **kwargs)


def _hostile_mkstemp(*args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_mkstemp_called"] = True
    mkstemp.__code__ = namespace["_autosport_test_original_mkstemp_code"]
    return mkstemp(*args, **kwargs)


def _hostile_abspath(*args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_abspath_called"] = True
    abspath.__code__ = namespace["_autosport_test_original_abspath_code"]
    return abspath(*args, **kwargs)


def _hostile_json_decoder_decode(self, payload, *args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_decoder_called"] = True
    JSONDecoder.decode.__code__ = namespace["_autosport_test_original_decoder_code"]
    return JSONDecoder.decode(self, payload, *args, **kwargs)


def _hostile_json_encoder_iterencode(self, value, *args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_encoder_called"] = True
    JSONEncoder.iterencode.__code__ = namespace["_autosport_test_original_encoder_code"]
    return JSONEncoder.iterencode(self, value, *args, **kwargs)


def _hostile_temp_wrapper_enter(self):
    namespace = globals()
    namespace["_autosport_test_hostile_wrapper_enter_called"] = True
    _TemporaryFileWrapper.__enter__.__code__ = namespace[
        "_autosport_test_original_wrapper_enter_code"
    ]
    return _TemporaryFileWrapper.__enter__(self)


def _hostile_wraps(*args, **kwargs):
    namespace = globals()
    namespace["_autosport_test_hostile_wraps_called"] = True
    wraps.__code__ = namespace["_autosport_test_original_wraps_code"]
    return wraps(*args, **kwargs)


def _hostile_global_getattr(*args, **kwargs):
    global _HOSTILE_GLOBAL_GETATTR_CALLED
    _HOSTILE_GLOBAL_GETATTR_CALLED = True
    return getattr(*args, **kwargs)


def _install_same_object_code_substitution(
    module,
    function_name: str,
    hostile_code,
    code_key: str,
    called_key: str,
):
    function = vars(module)[function_name]
    original_code = function.__code__
    vars(module)[code_key] = original_code
    vars(module)[called_key] = False
    function.__code__ = hostile_code
    return function, original_code


def _restore_same_object_code_substitution(
    module,
    function,
    original_code,
    code_key: str,
    called_key: str,
) -> bool:
    function.__code__ = original_code
    called = bool(vars(module).pop(called_key, False))
    vars(module).pop(code_key, None)
    return called


def _install_method_code_substitution(
    owner,
    method_name: str,
    hostile_code,
    code_key: str,
    called_key: str,
):
    function = vars(owner)[method_name]
    module = __import__(function.__module__, fromlist=["*"])
    original_code = function.__code__
    vars(module)[code_key] = original_code
    vars(module)[called_key] = False
    function.__code__ = hostile_code
    return module, function, original_code


def test_path_load_ignores_same_object_json_loads_code_substitution(tmp_path: Path) -> None:
    """Identity-preserving public json.loads mutation cannot reach witness/parser parsing."""

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
        loaded = paper.PaperBook.load(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            json,
            function,
            original_code,
            _LOADS_CODE_KEY,
            _LOADS_CALLED_KEY,
        )

    assert hostile_called is False
    assert loaded.balance == book.balance


def test_save_ignores_same_object_json_dump_code_and_publishes_canonical_snapshot(
    tmp_path: Path,
) -> None:
    """Public json.dump code mutation cannot retarget canonical serializer dispatch."""

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
    assert path.exists()
    assert witness_path.exists()
    assert paper.PaperBook.load(path).balance == book.balance


def test_save_ignores_same_object_json_dumps_code_for_witness_publication(
    tmp_path: Path,
) -> None:
    """Public json.dumps code mutation cannot retarget witness digest/publication."""

    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")

    function, original_code = _install_same_object_code_substitution(
        json,
        "dumps",
        _hostile_dumps.__code__,
        _DUMPS_CODE_KEY,
        _DUMPS_CALLED_KEY,
    )
    try:
        book.save(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            json,
            function,
            original_code,
            _DUMPS_CODE_KEY,
            _DUMPS_CALLED_KEY,
        )

    assert hostile_called is False
    assert path.exists()
    assert witness_path.exists()
    assert paper.PaperBook.load(path).balance == book.balance


def test_save_ignores_same_object_named_temporary_file_code_and_publishes_canonical_snapshot(
    tmp_path: Path,
) -> None:
    """The Python tempfile member used by canonical save is detached executable authority."""

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
    assert path.exists()
    assert witness_path.exists()
    assert paper.PaperBook.load(path).balance == book.balance


def test_save_ignores_same_object_mkstemp_code_for_outer_authority_stage(
    tmp_path: Path,
) -> None:
    """The witness guard's outer staging creator must not dispatch through public mkstemp."""

    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")

    function, original_code = _install_same_object_code_substitution(
        tempfile,
        "mkstemp",
        _hostile_mkstemp.__code__,
        _MKSTEMP_CODE_KEY,
        _MKSTEMP_CALLED_KEY,
    )
    try:
        book.save(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            tempfile,
            function,
            original_code,
            _MKSTEMP_CODE_KEY,
            _MKSTEMP_CALLED_KEY,
        )

    assert hostile_called is False
    assert path.exists()
    assert witness_path.exists()
    assert paper.PaperBook.load(path).balance == book.balance


def test_path_load_ignores_same_object_abspath_code_for_witness_identity(
    tmp_path: Path,
) -> None:
    """Snapshot identity must not dispatch through the mutable public os.path.abspath."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    function, original_code = _install_same_object_code_substitution(
        os.path,
        "abspath",
        _hostile_abspath.__code__,
        _ABSPATH_CODE_KEY,
        _ABSPATH_CALLED_KEY,
    )
    try:
        loaded = paper.PaperBook.load(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            os.path,
            function,
            original_code,
            _ABSPATH_CODE_KEY,
            _ABSPATH_CALLED_KEY,
        )

    assert hostile_called is False
    assert loaded.balance == book.balance


def test_path_load_rejects_same_object_json_decoder_code_substitution_before_dispatch(
    tmp_path: Path,
) -> None:
    """Detached json.loads may not traverse a retargeted shared JSONDecoder method."""

    path = tmp_path / "paper-book.json"
    book = paper.PaperBook("100")
    book.save(path)

    module, function, original_code = _install_method_code_substitution(
        json.JSONDecoder,
        "decode",
        _hostile_json_decoder_decode.__code__,
        _DECODER_CODE_KEY,
        _DECODER_CALLED_KEY,
    )
    try:
        with pytest.raises(ValueError, match="transitive|executable|authority"):
            paper.PaperBook.load(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            module,
            function,
            original_code,
            _DECODER_CODE_KEY,
            _DECODER_CALLED_KEY,
        )

    assert hostile_called is False


def test_save_rejects_same_object_json_encoder_code_before_durable_mutation(
    tmp_path: Path,
) -> None:
    """Detached json.dump/dumps may not traverse a retargeted JSONEncoder method."""

    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")

    module, function, original_code = _install_method_code_substitution(
        json.JSONEncoder,
        "iterencode",
        _hostile_json_encoder_iterencode.__code__,
        _ENCODER_CODE_KEY,
        _ENCODER_CALLED_KEY,
    )
    try:
        with pytest.raises(ValueError, match="transitive|executable|authority"):
            book.save(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            module,
            function,
            original_code,
            _ENCODER_CODE_KEY,
            _ENCODER_CALLED_KEY,
        )

    assert hostile_called is False
    assert not path.exists()
    assert not witness_path.exists()


def test_save_rejects_same_object_tempfile_wrapper_code_before_durable_mutation(
    tmp_path: Path,
) -> None:
    """Detached NamedTemporaryFile may not traverse mutable wrapper class methods."""

    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")

    module, function, original_code = _install_method_code_substitution(
        tempfile._TemporaryFileWrapper,
        "__enter__",
        _hostile_temp_wrapper_enter.__code__,
        _WRAPPER_CODE_KEY,
        _WRAPPER_CALLED_KEY,
    )
    try:
        with pytest.raises(ValueError, match="transitive|executable|authority"):
            book.save(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            module,
            function,
            original_code,
            _WRAPPER_CODE_KEY,
            _WRAPPER_CALLED_KEY,
        )

    assert hostile_called is False
    assert not path.exists()
    assert not witness_path.exists()


def test_save_rejects_same_object_functools_wraps_code_before_durable_mutation(
    tmp_path: Path,
) -> None:
    """Nested module-member Python helpers remain part of persistence authority."""

    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")

    function, original_code = _install_same_object_code_substitution(
        functools,
        "wraps",
        _hostile_wraps.__code__,
        _WRAPS_CODE_KEY,
        _WRAPS_CALLED_KEY,
    )
    try:
        with pytest.raises(ValueError, match="transitive|executable|authority"):
            book.save(path)
    finally:
        hostile_called = _restore_same_object_code_substitution(
            functools,
            function,
            original_code,
            _WRAPS_CODE_KEY,
            _WRAPS_CALLED_KEY,
        )

    assert hostile_called is False
    assert not path.exists()
    assert not witness_path.exists()


def test_save_rejects_late_builtin_shadow_before_durable_mutation(tmp_path: Path) -> None:
    """A new module global must not shadow a composition-time builtin dependency."""

    global _HOSTILE_GLOBAL_GETATTR_CALLED
    _HOSTILE_GLOBAL_GETATTR_CALLED = False
    path = tmp_path / "paper-book.json"
    witness_path = guard._witness_path(path)
    book = paper.PaperBook("100")
    namespace = vars(tempfile)
    assert "getattr" not in namespace
    namespace["getattr"] = _hostile_global_getattr
    try:
        with pytest.raises(ValueError, match="transitive|builtin|authority"):
            book.save(path)
    finally:
        namespace.pop("getattr", None)

    assert _HOSTILE_GLOBAL_GETATTR_CALLED is False
    assert not path.exists()
    assert not witness_path.exists()

import random
import struct

import pytest

from autosport.windows_pe_integrity import (
    PEPolicy,
    PEValidationError,
    validate_pe32plus_amd64,
)


def _align(value: int, n: int) -> int:
    return (value + n - 1) // n * n


def build_valid_pe(*, two_sections: bool = False) -> bytes:
    pe = 0x80
    optional_size = 0xF0
    section_count = 2 if two_sections else 1
    section_table = pe + 4 + 20 + optional_size
    headers = _align(section_table + section_count * 40, 0x200)

    text_raw = headers
    text_raw_size = 0x200
    text_va = 0x1000
    text_vs = 0x180

    rdata_raw = text_raw + text_raw_size
    rdata_raw_size = 0x200
    rdata_va = 0x2000
    rdata_vs = 0x100

    size_image = 0x3000 if two_sections else 0x2000
    file_size = (
        rdata_raw + rdata_raw_size if two_sections else text_raw + text_raw_size
    )
    data = bytearray(file_size)

    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe)
    data[pe : pe + 4] = b"PE\0\0"
    coff = pe + 4
    struct.pack_into(
        "<HHIIIHH",
        data,
        coff,
        0x8664,
        section_count,
        0,
        0,
        0,
        optional_size,
        0x0022,
    )

    opt = coff + 20
    struct.pack_into("<H", data, opt + 0, 0x20B)
    struct.pack_into("<I", data, opt + 16, text_va)
    struct.pack_into("<I", data, opt + 20, text_va)
    struct.pack_into("<Q", data, opt + 24, 0x140000000)
    struct.pack_into("<I", data, opt + 32, 0x1000)
    struct.pack_into("<I", data, opt + 36, 0x200)
    struct.pack_into("<HHHHHH", data, opt + 40, 6, 0, 0, 0, 6, 0)
    struct.pack_into("<I", data, opt + 52, 0)
    struct.pack_into("<I", data, opt + 56, size_image)
    struct.pack_into("<I", data, opt + 60, headers)
    struct.pack_into("<I", data, opt + 64, 0)
    struct.pack_into("<H", data, opt + 68, 3)
    struct.pack_into("<H", data, opt + 70, 0x8160)
    struct.pack_into(
        "<QQQQ",
        data,
        opt + 72,
        0x100000,
        0x1000,
        0x100000,
        0x1000,
    )
    struct.pack_into("<I", data, opt + 104, 0)
    struct.pack_into("<I", data, opt + 108, 16)

    sec = section_table
    data[sec : sec + 8] = b".text\0\0\0"
    struct.pack_into(
        "<IIIIIIHHI",
        data,
        sec + 8,
        text_vs,
        text_va,
        text_raw_size,
        text_raw,
        0,
        0,
        0,
        0,
        0x60000020,
    )
    data[text_raw : text_raw + 8] = b"\xC3" + b"\x90" * 7

    if two_sections:
        sec += 40
        data[sec : sec + 8] = b".rdata\0\0"
        struct.pack_into(
            "<IIIIIIHHI",
            data,
            sec + 8,
            rdata_vs,
            rdata_va,
            rdata_raw_size,
            rdata_raw,
            0,
            0,
            0,
            0,
            0x40000040,
        )
        data[rdata_raw : rdata_raw + 8] = b"autosport"

    return bytes(data)


def mutate_u16(blob: bytes, offset: int, value: int) -> bytes:
    b = bytearray(blob)
    struct.pack_into("<H", b, offset, value)
    return bytes(b)


def mutate_u32(blob: bytes, offset: int, value: int) -> bytes:
    b = bytearray(blob)
    struct.pack_into("<I", b, offset, value)
    return bytes(b)


def mutate_u64(blob: bytes, offset: int, value: int) -> bytes:
    b = bytearray(blob)
    struct.pack_into("<Q", b, offset, value)
    return bytes(b)


def offsets(blob: bytes):
    pe = struct.unpack_from("<I", blob, 0x3C)[0]
    coff = pe + 4
    opt = coff + 20
    optional_size = struct.unpack_from("<H", blob, coff + 16)[0]
    sec = opt + optional_size
    return pe, coff, opt, sec


def assert_rejected(blob: bytes, match: str | None = None):
    with pytest.raises(PEValidationError, match=match):
        validate_pe32plus_amd64(blob)


def test_valid_minimal_amd64_pe32plus():
    info = validate_pe32plus_amd64(build_valid_pe())
    assert info.machine == 0x8664
    assert info.number_of_sections == 1
    assert info.sections[0].name == ".text"
    assert info.sections[0].executable


def test_valid_two_section_image():
    info = validate_pe32plus_amd64(build_valid_pe(two_sections=True))
    assert [x.name for x in info.sections] == [".text", ".rdata"]


@pytest.mark.parametrize("cut", [0, 1, 63, 0x80, 0x98, 0x187])
def test_truncation_fails_closed(cut):
    blob = build_valid_pe()
    assert_rejected(blob[:cut])


def test_core_header_falsifiers():
    blob = build_valid_pe()
    pe, coff, opt, sec = offsets(blob)
    cases = [
        (b"ZZ" + blob[2:], "MZ"),
        (mutate_u32(blob, 0x3C, 0x20), "overlaps DOS"),
        (blob[:pe] + b"PX\0\0" + blob[pe + 4 :], "PE signature"),
        (mutate_u16(blob, coff + 0, 0x14C), "unexpected machine"),
        (mutate_u16(blob, coff + 2, 0), "section count"),
        (mutate_u16(blob, coff + 2, 97), "section count"),
        (mutate_u16(blob, opt + 0, 0x10B), "not PE32"),
        (mutate_u16(blob, coff + 18, 0x20), "EXECUTABLE_IMAGE"),
        (mutate_u16(blob, coff + 18, 0x2022), "DLL image"),
        (mutate_u16(blob, coff + 18, 0x1022), "system image"),
        (mutate_u16(blob, opt + 68, 1), "unsupported subsystem"),
        (mutate_u32(blob, opt + 52, 1), "Win32VersionValue"),
        (mutate_u32(blob, opt + 104, 1), "LoaderFlags"),
        (mutate_u64(blob, opt + 24, 0x140000001), "ImageBase"),
    ]
    for candidate, msg in cases:
        assert_rejected(candidate, msg)


def test_alignment_and_image_size_falsifiers():
    blob = build_valid_pe()
    _, _, opt, sec = offsets(blob)
    cases = [
        mutate_u32(blob, opt + 36, 768),
        mutate_u32(blob, opt + 32, 0x100),
        mutate_u32(blob, opt + 56, 0x1800),
        mutate_u32(blob, opt + 60, 0x300),
        mutate_u32(blob, sec + 12, 0x1800),
        mutate_u32(blob, sec + 16, 0x180),
        mutate_u32(blob, sec + 20, 0x210),
    ]
    for candidate in cases:
        assert_rejected(candidate)


def test_entrypoint_must_be_in_executable_section():
    blob = build_valid_pe(two_sections=True)
    _, _, opt, sec = offsets(blob)
    assert_rejected(mutate_u32(blob, opt + 16, 0), "entry point is zero")
    assert_rejected(mutate_u32(blob, opt + 16, 0x2500), "exactly one section")
    assert_rejected(mutate_u32(blob, opt + 16, 0x2000), "not executable")


def test_overlapping_raw_and_virtual_sections_rejected():
    blob = build_valid_pe(two_sections=True)
    _, _, _, sec = offsets(blob)
    sec2 = sec + 40
    assert_rejected(
        mutate_u32(blob, sec2 + 20, 0x200),
        "overlaps headers|overlapping ranges|raw-data pointers",
    )
    assert_rejected(
        mutate_u32(blob, sec2 + 12, 0x1000),
        "strictly ascending|overlapping ranges",
    )


def test_writable_executable_section_rejected_by_policy():
    blob = build_valid_pe()
    _, _, _, sec = offsets(blob)
    wx = mutate_u32(blob, sec + 36, 0xE0000020)
    assert_rejected(wx, "writable and executable")
    info = validate_pe32plus_amd64(
        wx,
        PEPolicy(forbid_write_execute_sections=False),
    )
    assert info.sections[0].writable and info.sections[0].executable


def test_declared_directory_must_fit_optional_header_and_image():
    blob = build_valid_pe()
    _, _, opt, _ = offsets(blob)
    assert_rejected(mutate_u32(blob, opt + 108, 17), "exceeds optional header")
    b = bytearray(blob)
    struct.pack_into("<II", b, opt + 112, 0x5000, 0x20)
    assert_rejected(bytes(b), "exceeds SizeOfImage")


def test_security_directory_uses_file_offset_and_alignment():
    blob = build_valid_pe()
    _, _, opt, _ = offsets(blob)
    cert_offset = len(blob)
    cert_size = 0x100
    b = bytearray(blob + b"\0" * 0x200)
    struct.pack_into("<IHH", b, cert_offset, cert_size, 0x0200, 0x0002)
    struct.pack_into("<II", b, opt + 112 + 4 * 8, cert_offset, cert_size)
    validate_pe32plus_amd64(bytes(b))
    struct.pack_into("<II", b, opt + 112 + 4 * 8, cert_offset + 1, cert_size)
    assert_rejected(bytes(b), "certificate table")


def test_malformed_certificate_record_chain_rejected():
    blob = build_valid_pe()
    _, _, opt, _ = offsets(blob)
    cert_offset = len(blob)
    b = bytearray(blob + b"\0" * 8)
    struct.pack_into("<II", b, opt + 112 + 4 * 8, cert_offset, 8)
    assert_rejected(bytes(b), "WIN_CERTIFICATE length")


def test_non_bytes_input_rejected():
    with pytest.raises(PEValidationError, match="immutable bytes"):
        validate_pe32plus_amd64(bytearray(build_valid_pe()))  # type: ignore[arg-type]


def test_adversarial_mutation_campaign_100k_rejects_guaranteed_invalid_cases():
    base = build_valid_pe(two_sections=True)
    pe, coff, opt, sec = offsets(base)
    sec2 = sec + 40
    mutators = [
        lambda b, r: mutate_u16(b, coff + 0, 0x14C),
        lambda b, r: mutate_u16(b, coff + 2, 0),
        lambda b, r: mutate_u16(b, opt + 0, 0x10B),
        lambda b, r: mutate_u16(b, coff + 18, 0x20),
        lambda b, r: mutate_u16(b, opt + 68, 1),
        lambda b, r: mutate_u32(b, opt + 104, 1),
        lambda b, r: mutate_u64(b, opt + 24, 0x140000001),
        lambda b, r: mutate_u32(b, opt + 36, 768),
        lambda b, r: mutate_u32(b, opt + 32, 0x100),
        lambda b, r: mutate_u32(b, opt + 56, 0x1800),
        lambda b, r: mutate_u32(b, opt + 60, 0x300),
        lambda b, r: mutate_u32(b, opt + 16, 0),
        lambda b, r: mutate_u32(b, opt + 16, 0x2000),
        lambda b, r: mutate_u32(b, sec + 12, 0x1800),
        lambda b, r: mutate_u32(b, sec + 16, 0x180),
        lambda b, r: mutate_u32(b, sec + 20, 0x210),
        lambda b, r: mutate_u32(b, sec + 36, 0xE0000020),
        lambda b, r: mutate_u32(b, sec2 + 12, 0x1000),
        lambda b, r: mutate_u32(b, sec2 + 20, 0x200),
        lambda b, r: mutate_u32(b, opt + 108, 17),
        lambda b, r: mutate_u32(b, coff + 8, 0x200),
        lambda b, r: mutate_u32(b, sec + 24, 0x200),
        lambda b, r: mutate_u32(mutate_u32(b, sec + 8, 0x300), opt + 16, 0x1200),
    ]
    rnd = random.Random(0xA5705)
    for _ in range(100_000):
        candidate = mutators[rnd.randrange(len(mutators))](base, rnd)
        with pytest.raises(PEValidationError):
            validate_pe32plus_amd64(candidate)


def test_size_of_image_must_match_last_mapped_extent():
    blob = build_valid_pe()
    _, _, opt, _ = offsets(blob)
    assert_rejected(mutate_u32(blob, opt + 56, 0x3000), "mapped extent")


def test_raw_data_order_must_follow_section_order():
    blob = build_valid_pe(two_sections=True)
    _, _, _, sec = offsets(blob)
    sec2 = sec + 40
    # Move .rdata before .text while keeping it header-safe and aligned.
    candidate = mutate_u32(blob, sec2 + 20, 0x200)
    # .text starts at 0x200 for this fixture, so equality violates strict raw ordering.
    assert_rejected(candidate, "raw-data pointers|overlapping ranges")


def test_utf8_section_name_is_accepted_and_invalid_utf8_rejected():
    blob = bytearray(build_valid_pe())
    _, _, _, sec = offsets(blob)
    blob[sec : sec + 8] = "étext".encode("utf-8").ljust(8, b"\0")
    info = validate_pe32plus_amd64(bytes(blob))
    assert info.sections[0].name == "étext"

    blob[sec : sec + 8] = b"\xfftext\x00\x00\x00"
    assert_rejected(bytes(blob), "valid UTF-8")


def test_zero_data_directory_count_is_valid():
    blob = build_valid_pe()
    _, _, opt, _ = offsets(blob)
    info = validate_pe32plus_amd64(mutate_u32(blob, opt + 108, 0))
    assert info.number_of_rva_and_sizes == 0


def test_partial_zero_data_directory_rejected():
    blob = bytearray(build_valid_pe())
    _, _, opt, _ = offsets(blob)
    struct.pack_into("<II", blob, opt + 112, 0x1000, 0)
    assert_rejected(bytes(blob), "partial zero")


def test_mapped_data_directory_inside_text_is_valid():
    blob = bytearray(build_valid_pe())
    _, _, opt, _ = offsets(blob)
    struct.pack_into("<II", blob, opt + 112, 0x1000, 0x20)
    validate_pe32plus_amd64(bytes(blob))


def test_certificate_directory_may_not_overlap_raw_section():
    blob = bytearray(build_valid_pe())
    _, _, opt, _ = offsets(blob)
    struct.pack_into("<II", blob, opt + 112 + 4 * 8, 0x200, 0x100)
    assert_rejected(bytes(blob), "certificate table overlaps")



def test_size_of_headers_must_equal_rounded_header_extent():
    blob = build_valid_pe()
    _, _, opt, _ = offsets(blob)
    candidate = mutate_u32(blob + b"\0" * 0x200, opt + 60, 0x400)
    assert_rejected(candidate, "rounded header extent")


def test_section_virtual_addresses_must_be_adjacent():
    blob = build_valid_pe(two_sections=True)
    _, _, _, sec = offsets(blob)
    sec2 = sec + 40
    candidate = bytearray(blob + b"\0" * 0x1000)
    struct.pack_into("<I", candidate, sec2 + 12, 0x3000)
    _, _, opt, _ = offsets(candidate)
    struct.pack_into("<I", candidate, opt + 56, 0x4000)
    assert_rejected(bytes(candidate), "not adjacent")


def test_reserved_and_global_ptr_data_directories():
    blob = bytearray(build_valid_pe())
    _, _, opt, _ = offsets(blob)
    struct.pack_into("<II", blob, opt + 112 + 7 * 8, 0x1000, 0x20)
    assert_rejected(bytes(blob), "reserved and must be zero")

    blob = bytearray(build_valid_pe())
    struct.pack_into("<II", blob, opt + 112 + 8 * 8, 0x1000, 0)
    validate_pe32plus_amd64(bytes(blob))
    struct.pack_into("<II", blob, opt + 112 + 8 * 8, 0x1000, 1)
    assert_rejected(bytes(blob), "Global Ptr.*size must be zero")

    blob = bytearray(build_valid_pe())
    struct.pack_into("<II", blob, opt + 112 + 15 * 8, 0x1000, 0x20)
    assert_rejected(bytes(blob), "reserved and must be zero")

def test_coff_symbol_table_fields_rejected_for_image():
    blob = build_valid_pe()
    _, coff, _, _ = offsets(blob)
    assert_rejected(
        mutate_u32(blob, coff + 8, 0x200),
        "COFF symbol table",
    )
    assert_rejected(
        mutate_u32(blob, coff + 12, 1),
        "COFF symbol table",
    )


def test_image_section_coff_relocation_and_line_fields_rejected():
    blob = build_valid_pe()
    _, _, _, sec = offsets(blob)
    cases = (
        mutate_u32(blob, sec + 24, 0x200),
        mutate_u32(blob, sec + 28, 0x200),
        mutate_u16(blob, sec + 32, 1),
        mutate_u16(blob, sec + 34, 1),
    )
    for candidate in cases:
        assert_rejected(candidate, "COFF relocation|COFF line-number")


def test_pe_header_requires_8_byte_alignment():
    blob = bytearray(build_valid_pe())
    blob[0x80:0x80] = b"\0" * 4
    struct.pack_into("<I", blob, 0x3C, 0x84)
    assert_rejected(bytes(blob), "8-byte aligned")


def _build_valid_low_alignment_pe() -> bytes:
    blob = bytearray(build_valid_pe())
    _, _, opt, sec = offsets(blob)
    struct.pack_into("<I", blob, opt + 16, 0x200)
    struct.pack_into("<I", blob, opt + 32, 0x200)
    struct.pack_into("<I", blob, opt + 36, 0x200)
    struct.pack_into("<I", blob, opt + 56, 0x400)
    struct.pack_into("<I", blob, sec + 12, 0x200)
    return bytes(blob)


def test_low_section_alignment_requires_raw_offset_equal_rva():
    valid = _build_valid_low_alignment_pe()
    validate_pe32plus_amd64(valid)

    candidate = bytearray(valid + b"\0" * 0x200)
    _, _, _, sec = offsets(candidate)
    struct.pack_into("<I", candidate, sec + 20, 0x400)
    assert_rejected(bytes(candidate), "raw file offset equal RVA")


def test_entry_point_must_be_backed_by_file_bytes():
    blob = build_valid_pe()
    _, _, opt, sec = offsets(blob)
    candidate = mutate_u32(blob, sec + 8, 0x300)
    candidate = mutate_u32(candidate, opt + 16, 0x1200)
    assert_rejected(candidate, "not backed by file bytes")


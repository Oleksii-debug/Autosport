from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterable


class PEValidationError(ValueError):
    """Raised when a Windows PE image violates the release semantic contract."""


@dataclass(frozen=True)
class PEPolicy:
    machine: int = 0x8664  # IMAGE_FILE_MACHINE_AMD64
    allowed_subsystems: frozenset[int] = frozenset({2, 3})  # GUI, CUI
    max_sections: int = 96
    max_image_size: int = 2 * 1024 * 1024 * 1024
    require_executable_image: bool = True
    forbid_dll: bool = True
    forbid_system: bool = True
    forbid_write_execute_sections: bool = True
    require_nonzero_entry_point: bool = True
    require_entry_point_in_executable_section: bool = True


@dataclass(frozen=True)
class PESection:
    name: str
    virtual_size: int
    virtual_address: int
    raw_size: int
    raw_pointer: int
    characteristics: int

    @property
    def mapped_size(self) -> int:
        return max(self.virtual_size, self.raw_size)

    @property
    def virtual_end(self) -> int:
        return self.virtual_address + self.mapped_size

    @property
    def raw_end(self) -> int:
        return self.raw_pointer + self.raw_size

    @property
    def executable(self) -> bool:
        return bool(self.characteristics & 0x20000000)  # IMAGE_SCN_MEM_EXECUTE

    @property
    def writable(self) -> bool:
        return bool(self.characteristics & 0x80000000)  # IMAGE_SCN_MEM_WRITE


@dataclass(frozen=True)
class PEInfo:
    machine: int
    number_of_sections: int
    characteristics: int
    image_base: int
    section_alignment: int
    file_alignment: int
    size_of_image: int
    size_of_headers: int
    subsystem: int
    dll_characteristics: int
    address_of_entry_point: int
    number_of_rva_and_sizes: int
    sections: tuple[PESection, ...]


_IMAGE_FILE_EXECUTABLE_IMAGE = 0x0002
_IMAGE_FILE_SYSTEM = 0x1000
_IMAGE_FILE_DLL = 0x2000
_PE32_PLUS_MAGIC = 0x020B
_PE_SIGNATURE = b"PE\0\0"
_PAGE_SIZE = 4096
_SECTION_HEADER_SIZE = 40
_FIXED_PE32_PLUS_FIELDS = 112


def _fail(message: str) -> None:
    raise PEValidationError(message)


def _need(data: bytes, offset: int, size: int, what: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) - size:
        _fail(f"truncated {what}")


def _u16(data: bytes, offset: int, what: str) -> int:
    _need(data, offset, 2, what)
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int, what: str) -> int:
    _need(data, offset, 4, what)
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int, what: str) -> int:
    _need(data, offset, 8, what)
    return struct.unpack_from("<Q", data, offset)[0]


def _is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def _aligned(value: int, alignment: int) -> bool:
    return alignment > 0 and value % alignment == 0


def _ranges_do_not_overlap(ranges: Iterable[tuple[int, int, str]]) -> None:
    ordered = sorted((start, end, label) for start, end, label in ranges if end > start)
    for (a0, a1, an), (b0, b1, bn) in zip(ordered, ordered[1:]):
        if b0 < a1:
            _fail(f"overlapping ranges: {an} and {bn}")

def _validate_certificate_table_records(data: bytes, offset: int, size: int) -> None:
    end = offset + size
    cursor = offset
    while cursor < end:
        if cursor % 8:
            _fail("certificate table entry is not 8-byte aligned")
        if cursor > end - 8:
            _fail("certificate table has truncated WIN_CERTIFICATE header")
        length = _u32(data, cursor, "WIN_CERTIFICATE.dwLength")
        if length < 8:
            _fail("WIN_CERTIFICATE length is smaller than header")
        rounded = (length + 7) // 8 * 8
        next_cursor = cursor + rounded
        if next_cursor <= cursor or next_cursor > end:
            _fail("WIN_CERTIFICATE records exceed certificate table size")
        cursor = next_cursor
    if cursor != end:
        _fail("WIN_CERTIFICATE records do not fill certificate table size")


def validate_pe32plus_amd64(data: bytes, policy: PEPolicy = PEPolicy()) -> PEInfo:
    """Validate a bounded Windows x64 PE32+ application image.

    This is a fail-closed structural/semantic release oracle, not a replacement for
    Windows loader execution, Authenticode verification, AV scanning, or human/NVDA
    acceptance.
    """
    if not isinstance(data, bytes):
        _fail("image must be immutable bytes")
    if len(data) < 64:
        _fail("file too small for DOS header")
    if data[:2] != b"MZ":
        _fail("missing DOS MZ signature")

    pe_offset = _u32(data, 0x3C, "DOS e_lfanew")
    if pe_offset < 0x40:
        _fail("PE header overlaps DOS header")
    if pe_offset % 8:
        _fail("PE header is not 8-byte aligned")
    _need(data, pe_offset, 4 + 20, "PE signature and COFF header")
    if data[pe_offset : pe_offset + 4] != _PE_SIGNATURE:
        _fail("missing PE signature")

    coff = pe_offset + 4
    machine = _u16(data, coff + 0, "COFF Machine")
    section_count = _u16(data, coff + 2, "COFF NumberOfSections")
    pointer_to_symbols = _u32(data, coff + 8, "COFF PointerToSymbolTable")
    number_of_symbols = _u32(data, coff + 12, "COFF NumberOfSymbols")
    optional_size = _u16(data, coff + 16, "COFF SizeOfOptionalHeader")
    characteristics = _u16(data, coff + 18, "COFF Characteristics")

    if machine != policy.machine:
        _fail(f"unexpected machine: 0x{machine:04x}")
    if section_count < 1 or section_count > policy.max_sections:
        _fail("invalid section count")
    if pointer_to_symbols or number_of_symbols:
        _fail("COFF symbol table must be absent from executable image")
    if optional_size < _FIXED_PE32_PLUS_FIELDS:
        _fail("optional header too small for PE32+ fixed fields")

    opt = coff + 20
    _need(data, opt, optional_size, "optional header")
    if _u16(data, opt, "OptionalHeader.Magic") != _PE32_PLUS_MAGIC:
        _fail("image is not PE32+")

    entry = _u32(data, opt + 16, "AddressOfEntryPoint")
    image_base = _u64(data, opt + 24, "ImageBase")
    section_alignment = _u32(data, opt + 32, "SectionAlignment")
    file_alignment = _u32(data, opt + 36, "FileAlignment")
    win32_version_value = _u32(data, opt + 52, "Win32VersionValue")
    size_of_image = _u32(data, opt + 56, "SizeOfImage")
    size_of_headers = _u32(data, opt + 60, "SizeOfHeaders")
    subsystem = _u16(data, opt + 68, "Subsystem")
    dll_characteristics = _u16(data, opt + 70, "DllCharacteristics")
    loader_flags = _u32(data, opt + 104, "LoaderFlags")
    directory_count = _u32(data, opt + 108, "NumberOfRvaAndSizes")

    if policy.require_executable_image and not (characteristics & _IMAGE_FILE_EXECUTABLE_IMAGE):
        _fail("IMAGE_FILE_EXECUTABLE_IMAGE not set")
    if policy.forbid_dll and (characteristics & _IMAGE_FILE_DLL):
        _fail("DLL image is not an application executable")
    if policy.forbid_system and (characteristics & _IMAGE_FILE_SYSTEM):
        _fail("system image is not an application executable")
    if subsystem not in policy.allowed_subsystems:
        _fail(f"unsupported subsystem: {subsystem}")
    if win32_version_value != 0:
        _fail("Win32VersionValue must be zero")
    if loader_flags != 0:
        _fail("LoaderFlags must be zero")
    if image_base == 0 or image_base % 0x10000 != 0:
        _fail("ImageBase must be a nonzero 64 KiB multiple")

    if not _is_power_of_two(file_alignment):
        _fail("FileAlignment must be a power of two")
    if section_alignment < _PAGE_SIZE:
        if file_alignment != section_alignment:
            _fail("low SectionAlignment requires equal FileAlignment")
    elif not (512 <= file_alignment <= 65536):
        _fail("FileAlignment outside 512..65536")
    if section_alignment < file_alignment:
        _fail("SectionAlignment smaller than FileAlignment")

    if size_of_image == 0 or size_of_image > policy.max_image_size:
        _fail("SizeOfImage out of bounds")
    if not _aligned(size_of_image, section_alignment):
        _fail("SizeOfImage is not SectionAlignment-aligned")
    if size_of_headers == 0 or not _aligned(size_of_headers, file_alignment):
        _fail("SizeOfHeaders is not FileAlignment-aligned")
    if size_of_headers > len(data):
        _fail("SizeOfHeaders exceeds file length")

    max_directories = (optional_size - _FIXED_PE32_PLUS_FIELDS) // 8
    if directory_count > max_directories:
        _fail("NumberOfRvaAndSizes exceeds optional header")

    section_table = opt + optional_size
    table_size = section_count * _SECTION_HEADER_SIZE
    _need(data, section_table, table_size, "section table")
    min_headers = section_table + table_size
    if size_of_headers < min_headers:
        _fail("SizeOfHeaders does not cover section table")
    expected_size_of_headers = (
        (min_headers + file_alignment - 1) // file_alignment * file_alignment
    )
    if size_of_headers != expected_size_of_headers:
        _fail("SizeOfHeaders does not match rounded header extent")

    sections: list[PESection] = []
    raw_ranges: list[tuple[int, int, str]] = []
    virtual_ranges: list[tuple[int, int, str]] = []
    previous_va = -1
    previous_virtual_end = -1
    previous_raw_pointer = -1

    for index in range(section_count):
        off = section_table + index * _SECTION_HEADER_SIZE
        raw_name = data[off : off + 8].split(b"\0", 1)[0]
        try:
            name = raw_name.decode("utf-8") or f"section#{index + 1}"
        except UnicodeDecodeError:
            _fail(f"section#{index + 1} name is not valid UTF-8")

        virtual_size = _u32(data, off + 8, f"{name}.VirtualSize")
        virtual_address = _u32(data, off + 12, f"{name}.VirtualAddress")
        raw_size = _u32(data, off + 16, f"{name}.SizeOfRawData")
        raw_pointer = _u32(data, off + 20, f"{name}.PointerToRawData")
        pointer_to_relocations = _u32(data, off + 24, f"{name}.PointerToRelocations")
        pointer_to_linenumbers = _u32(data, off + 28, f"{name}.PointerToLinenumbers")
        number_of_relocations = _u16(data, off + 32, f"{name}.NumberOfRelocations")
        number_of_linenumbers = _u16(data, off + 34, f"{name}.NumberOfLinenumbers")
        sec_chars = _u32(data, off + 36, f"{name}.Characteristics")

        if pointer_to_relocations or number_of_relocations:
            _fail(f"{name} contains COFF relocation fields in executable image")
        if pointer_to_linenumbers or number_of_linenumbers:
            _fail(f"{name} contains COFF line-number fields in executable image")

        if virtual_address < size_of_headers:
            _fail(f"{name} virtual address overlaps headers")
        if not _aligned(virtual_address, section_alignment):
            _fail(f"{name} virtual address misaligned")
        if virtual_address <= previous_va:
            _fail("section virtual addresses are not strictly ascending")
        if previous_virtual_end >= 0:
            expected_va = (
                (previous_virtual_end + section_alignment - 1)
                // section_alignment
                * section_alignment
            )
            if virtual_address != expected_va:
                _fail("section virtual addresses are not adjacent")
        previous_va = virtual_address

        if raw_size:
            if not _aligned(raw_size, file_alignment):
                _fail(f"{name} raw size misaligned")
            if not _aligned(raw_pointer, file_alignment):
                _fail(f"{name} raw pointer misaligned")
            if raw_pointer < size_of_headers:
                _fail(f"{name} raw data overlaps headers")
            if section_alignment < _PAGE_SIZE and raw_pointer != virtual_address:
                _fail(f"{name} low SectionAlignment requires raw file offset equal RVA")
            raw_end = raw_pointer + raw_size
            if raw_end < raw_pointer or raw_end > len(data):
                _fail(f"{name} raw data exceeds file")
            if raw_pointer <= previous_raw_pointer:
                _fail("section raw-data pointers are not strictly ascending")
            previous_raw_pointer = raw_pointer
            raw_ranges.append((raw_pointer, raw_end, name))
        elif raw_pointer != 0:
            _fail(f"{name} has raw pointer with zero raw size")

        mapped_size = max(virtual_size, raw_size)
        if mapped_size == 0:
            _fail(f"{name} has zero mapped size")
        virtual_end = virtual_address + mapped_size
        if virtual_end < virtual_address or virtual_end > size_of_image:
            _fail(f"{name} virtual span exceeds SizeOfImage")
        virtual_ranges.append((virtual_address, virtual_end, name))
        previous_virtual_end = virtual_end

        if policy.forbid_write_execute_sections:
            executable = bool(sec_chars & 0x20000000)
            writable = bool(sec_chars & 0x80000000)
            if executable and writable:
                _fail(f"{name} is writable and executable")

        sections.append(
            PESection(
                name=name,
                virtual_size=virtual_size,
                virtual_address=virtual_address,
                raw_size=raw_size,
                raw_pointer=raw_pointer,
                characteristics=sec_chars,
            )
        )

    _ranges_do_not_overlap(raw_ranges)
    _ranges_do_not_overlap(virtual_ranges)

    max_virtual_end = max((s.virtual_end for s in sections), default=size_of_headers)
    expected_size_of_image = (
        (max(max_virtual_end, size_of_headers) + section_alignment - 1)
        // section_alignment
        * section_alignment
    )
    if size_of_image != expected_size_of_image:
        _fail("SizeOfImage does not match aligned mapped extent")

    if policy.require_nonzero_entry_point and entry == 0:
        _fail("application entry point is zero")
    if entry >= size_of_image:
        _fail("entry point lies outside SizeOfImage")
    if policy.require_entry_point_in_executable_section:
        owners = [s for s in sections if s.virtual_address <= entry < s.virtual_end]
        if len(owners) != 1:
            _fail("entry point is not owned by exactly one section")
        if not owners[0].executable:
            _fail("entry point section is not executable")
        entry_offset = entry - owners[0].virtual_address
        if owners[0].raw_size == 0 or entry_offset >= owners[0].raw_size:
            _fail("entry point is not backed by file bytes")

    # Data directories are RVA/size pairs except Security, whose first field is a file offset.
    directory_base = opt + _FIXED_PE32_PLUS_FIELDS
    for index in range(directory_count):
        addr = _u32(
            data,
            directory_base + index * 8,
            f"DataDirectory[{index}].VirtualAddress",
        )
        size = _u32(
            data,
            directory_base + index * 8 + 4,
            f"DataDirectory[{index}].Size",
        )
        if index in {7, 15}:  # reserved data-directory entries
            if addr != 0 or size != 0:
                _fail(f"DataDirectory[{index}] is reserved and must be zero")
            continue
        if index == 8:  # Global Ptr: RVA may be nonzero, Size must be zero
            if size != 0:
                _fail("Global Ptr data-directory size must be zero")
            if addr == 0:
                continue
            mapped = addr < size_of_headers or any(
                section.virtual_address <= addr < section.virtual_end
                for section in sections
            )
            if not mapped:
                _fail("Global Ptr data-directory RVA is not mapped")
            continue
        if addr == 0 and size == 0:
            continue
        if addr == 0 or size == 0:
            _fail(f"DataDirectory[{index}] has partial zero range")
        end = addr + size
        if end < addr:
            _fail(f"DataDirectory[{index}] overflows")
        if index == 4:  # certificate table uses file offsets, not RVA
            if addr % 8 != 0 or end > len(data):
                _fail("certificate table is misaligned or exceeds file")
            if addr < size_of_headers or any(
                not (end <= r0 or addr >= r1) for r0, r1, _ in raw_ranges
            ):
                _fail("certificate table overlaps mapped image bytes")
            _validate_certificate_table_records(data, addr, size)
        else:
            if end > size_of_image:
                _fail(f"DataDirectory[{index}] exceeds SizeOfImage")
            mapped = end <= size_of_headers or any(
                s.virtual_address <= addr and end <= s.virtual_end for s in sections
            )
            if not mapped:
                _fail(f"DataDirectory[{index}] is not fully mapped")

    return PEInfo(
        machine=machine,
        number_of_sections=section_count,
        characteristics=characteristics,
        image_base=image_base,
        section_alignment=section_alignment,
        file_alignment=file_alignment,
        size_of_image=size_of_image,
        size_of_headers=size_of_headers,
        subsystem=subsystem,
        dll_characteristics=dll_characteristics,
        address_of_entry_point=entry,
        number_of_rva_and_sizes=directory_count,
        sections=tuple(sections),
    )

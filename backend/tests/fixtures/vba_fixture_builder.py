"""Builds real, spec-valid .xlsm fixture files with genuine VBA macros, for
the Virtual Run test suite. Hand-constructs a minimal MS-OVBA compressed
container and a minimal MS-CFB (OLE2) compound file for xl/vbaProject.bin,
then splices it into an openpyxl-built .xlsx package. Test-only utility --
never imported by product code, and does not depend on Excel/COM.
"""

from __future__ import annotations

import io
import struct
import zipfile
from openpyxl import Workbook


# ---------------------------------------------------------------------------
# MS-OVBA 2.4.1 compression (all-literal encoding: correct, not space-optimal)
# ---------------------------------------------------------------------------

def _compress_chunk(data: bytes) -> bytes:
    """One CompressedChunk for <=4096 decompressed bytes (MS-OVBA 2.4.1.1.4).
    Uses an all-literal "compressed" encoding (no back-references -- not
    space-optimal, but simple and spec-valid) when that fits the 12-bit
    CompressedChunkSize field; an all-literal encoding of close to 4096
    bytes cannot (flag-byte overhead pushes it over 4095), so that case
    falls back to the "uncompressed" chunk representation instead
    (MS-OVBA 2.4.1.1.6: exactly 4096 raw bytes, flag=0)."""
    if len(data) > 4096:
        raise ValueError("a single chunk holds at most 4096 decompressed bytes")
    chunk_data = bytearray()
    for offset in range(0, len(data), 8):
        group = data[offset:offset + 8]
        chunk_data.append(0x00)  # FlagByte: every bit 0 => all literal tokens
        chunk_data.extend(group)
    compressed_chunk_size = len(chunk_data) - 1  # header field: size-3, header itself is 2 bytes
    if compressed_chunk_size <= 0xFFF:
        header = compressed_chunk_size | (0b011 << 12) | (1 << 15)  # flag=1 (compressed)
        return struct.pack("<H", header) + bytes(chunk_data)
    raw = data.ljust(4096, b"\x00")
    header = 0xFFF | (0b011 << 12)  # size field fixed at 4095, flag=0 (uncompressed)
    return struct.pack("<H", header) + raw


def compress_container(data: bytes, min_container_bytes: int = 0) -> bytes:
    """Compress ``data`` (<=4096 bytes) as a CompressedContainer. If
    ``min_container_bytes`` is given, an extra all-zero padding chunk is
    appended so the total on-disk size reaches that minimum -- used so a
    stream's declared size clears the 4096-byte mini-stream cutoff (see
    build_compound_file). Padding is invisible to real readers: nothing
    that reads this container ever looks past the real content it expects."""
    if len(data) > 4096:
        raise ValueError("single-chunk builder only supports <=4096 bytes")
    out = b"\x01" + _compress_chunk(data)
    while len(out) < min_container_bytes:
        out += _compress_chunk(b"\x00" * 4096)
    return out


# ---------------------------------------------------------------------------
# dir stream builder (MS-OVBA 2.3.4.2)
# ---------------------------------------------------------------------------

def _record(record_id: int, payload: bytes) -> bytes:
    return struct.pack("<H", record_id) + struct.pack("<L", len(payload)) + payload


def build_dir_stream(module_name: str, module_stream_name: str, text_offset: int) -> bytes:
    out = bytearray()
    out += _record(0x0001, struct.pack("<L", 0x00000001))  # PROJECTSYSKIND
    out += _record(0x0002, struct.pack("<L", 0x00000409))  # PROJECTLCID
    out += _record(0x0014, struct.pack("<L", 0x00000409))  # PROJECTLCIDINVOKE
    out += struct.pack("<H", 0x0003) + struct.pack("<L", 2) + struct.pack("<H", 1252)  # PROJECTCODEPAGE
    name_bytes = b"VBAProject"
    out += _record(0x0004, name_bytes)  # PROJECTNAME
    # PROJECTDOCSTRING: DocString(empty) + Reserved(0x0040) + DocStringUnicode(empty)
    out += struct.pack("<H", 0x0005) + struct.pack("<L", 0) + struct.pack("<H", 0x0040) + struct.pack("<L", 0)
    # PROJECTHELPFILEPATH: HelpFile1(empty) + Reserved(0x003D) + HelpFile2(empty)
    out += struct.pack("<H", 0x0006) + struct.pack("<L", 0) + struct.pack("<H", 0x003D) + struct.pack("<L", 0)
    out += _record(0x0007, struct.pack("<L", 0))  # PROJECTHELPCONTEXT
    out += _record(0x0008, struct.pack("<L", 0))  # PROJECTLIBFLAGS
    # PROJECTVERSION: Id(2) + Reserved(4, MUST be 4) + VersionMajor(4) + VersionMinor(2)
    out += struct.pack("<H", 0x0009) + struct.pack("<L", 4) + struct.pack("<L", 1) + struct.pack("<H", 0)
    # PROJECTCONSTANTS: Constants(empty) + Reserved(0x003C) + ConstantsUnicode(empty)
    out += struct.pack("<H", 0x000C) + struct.pack("<L", 0) + struct.pack("<H", 0x003C) + struct.pack("<L", 0)
    # No REFERENCE records: go straight to PROJECTMODULES (id doubles as the loop's terminator check)
    out += struct.pack("<H", 0x000F)  # PROJECTMODULES id
    out += struct.pack("<L", 0x0002)  # PROJECTMODULES size
    out += struct.pack("<H", 1)       # modules_count = 1
    out += struct.pack("<H", 0x0013) + struct.pack("<L", 0x0002) + struct.pack("<H", 0xFFFF)  # cookie record

    # --- MODULE record ---
    name_b = module_name.encode("mbcs" if False else "latin-1")
    stream_b = module_stream_name.encode("latin-1")
    out += _record(0x0019, name_b)  # MODULENAME
    # MODULESTREAMNAME: name + Reserved(0x0032) + NameUnicode
    stream_unicode = module_stream_name.encode("utf-16-le")
    out += (struct.pack("<H", 0x001A) + struct.pack("<L", len(stream_b)) + stream_b
            + struct.pack("<H", 0x0032) + struct.pack("<L", len(stream_unicode)) + stream_unicode)
    out += struct.pack("<H", 0x0031) + struct.pack("<L", 4) + struct.pack("<L", text_offset)  # MODULEOFFSET
    out += struct.pack("<H", 0x0021) + struct.pack("<L", 0)  # MODULETYPE (procedural/standard module)
    out += struct.pack("<H", 0x002B) + struct.pack("<L", 0)  # MODULE TERMINATOR
    return bytes(out)


def build_project_stream(module_name: str) -> bytes:
    text = (
        'ID="{00000000-0000-0000-0000-000000000000}"\r\n'
        f"Module={module_name}\r\n"
        'Name="VBAProject"\r\n'
    )
    return text.encode("latin-1")


def build_module_stream(vba_source: str, code_page: str = "latin-1") -> tuple[bytes, int]:
    """Returns (stream_bytes, text_offset). Everything before ``text_offset``
    (the PerformanceCache, arbitrary/unused by olevba) is never read by the
    decompressor, so it doubles as safe padding to clear the 4096-byte
    mini-stream cutoff (see build_compound_file)."""
    performance_cache = b"\x00" * 4096
    compressed = compress_container(vba_source.encode(code_page))
    return performance_cache + compressed, len(performance_cache)


# ---------------------------------------------------------------------------
# Minimal MS-CFB (OLE2 compound file) writer -- 512-byte sectors, every
# stream padded to a multiple of the sector size and placed in the regular
# FAT chain (mini-FAT is never used, so it never needs to be implemented).
# ---------------------------------------------------------------------------

SECT_SIZE = 512
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
STGTY_STORAGE = 1
STGTY_STREAM = 2
STGTY_ROOT = 5


class _DirEntry:
    def __init__(self, name: str, entry_type: int, data: bytes | None = None):
        self.name = name
        self.type = entry_type
        self.data = data or b""
        self.left = NOSTREAM
        self.right = NOSTREAM
        self.child = NOSTREAM
        self.start_sector = ENDOFCHAIN
        self.size = len(self.data)


def _pack_dir_entry(entry: _DirEntry) -> bytes:
    name_utf16 = entry.name.encode("utf-16-le") + b"\x00\x00"
    name_field = name_utf16.ljust(64, b"\x00")
    name_len = len(name_utf16)
    color = 1  # black; a single-color degenerate tree is fine for a tree walker
    clsid = b"\x00" * 16
    state_bits = b"\x00" * 4
    times = b"\x00" * 8 * 2
    return (
        name_field
        + struct.pack("<H", name_len)
        + struct.pack("<B", entry.type)
        + struct.pack("<B", color)
        + struct.pack("<L", entry.left)
        + struct.pack("<L", entry.right)
        + struct.pack("<L", entry.child)
        + clsid
        + state_bits
        + times
        + struct.pack("<L", entry.start_sector if entry.start_sector != ENDOFCHAIN or entry.size else ENDOFCHAIN)
        + struct.pack("<Q", entry.size)
    )


def build_compound_file(project_stream: bytes, vba_project_marker: bytes, dir_stream: bytes, module_name: str, module_stream: bytes) -> bytes:
    """Root
    ├── PROJECT              (stream)
    └── VBA                  (storage)
        ├── _VBA_PROJECT     (stream)
        ├── dir              (stream)
        └── <module_name>    (stream)
    """
    def pad(data: bytes) -> bytes:
        # Every stream must be >=4096 bytes (the mini-stream cutoff) so a
        # compliant reader always uses the regular FAT for it -- this writer
        # never populates the mini-FAT/mini-stream, so anything smaller would
        # be silently unreadable.
        target = max(4096, len(data))
        remainder = target % SECT_SIZE
        if remainder:
            target += SECT_SIZE - remainder
        return data + b"\x00" * (target - len(data))

    streams = {
        "PROJECT": pad(project_stream),
        "_VBA_PROJECT": pad(vba_project_marker),
        "dir": pad(dir_stream),
        module_name: pad(module_stream),
    }
    sizes = {"PROJECT": len(project_stream), "_VBA_PROJECT": len(vba_project_marker),
             "dir": len(dir_stream), module_name: len(module_stream)}

    # Lay out stream sectors sequentially, recording each stream's start sector.
    sector_data = bytearray()
    start_sectors: dict[str, int] = {}
    for key, blob in streams.items():
        start_sectors[key] = len(sector_data) // SECT_SIZE
        sector_data += blob

    root = _DirEntry("Root Entry", STGTY_ROOT)
    project = _DirEntry("PROJECT", STGTY_STREAM, streams["PROJECT"])
    project.size = sizes["PROJECT"]
    project.start_sector = start_sectors["PROJECT"]
    vba = _DirEntry("VBA", STGTY_STORAGE)
    vba_project = _DirEntry("_VBA_PROJECT", STGTY_STREAM, streams["_VBA_PROJECT"])
    vba_project.size = sizes["_VBA_PROJECT"]
    vba_project.start_sector = start_sectors["_VBA_PROJECT"]
    dir_entry = _DirEntry("dir", STGTY_STREAM, streams["dir"])
    dir_entry.size = sizes["dir"]
    dir_entry.start_sector = start_sectors["dir"]
    module_entry = _DirEntry(module_name, STGTY_STREAM, streams[module_name])
    module_entry.size = sizes[module_name]
    module_entry.start_sector = start_sectors[module_name]

    # Root's child is "VBA"; VBA storage's children form a right-leaning chain.
    root.child = 2       # index of "VBA" (see entries list order below)
    vba.child = 3        # index of "PROJECT" -- Root's *direct* stream sibling chain starts here instead
    # Simpler: put PROJECT as a second root-level chain via root.left is not used (single child pointer only
    # supports one subtree) -- so make VBA storage the root child, and thread PROJECT as VBA's sibling via
    # right-pointer from a synthetic ordering: Root.child -> PROJECT ; PROJECT.right -> VBA.
    root.child = 1        # index of "PROJECT" entry (see entries[] ordering below)
    # entries index map (see list order): 0=Root,1=PROJECT,2=VBA,3=_VBA_PROJECT,4=dir,5=module
    project.right = 2     # PROJECT -> VBA (sibling chain under Root)
    vba.child = 3         # VBA's first child = _VBA_PROJECT
    vba_project.right = 4  # _VBA_PROJECT -> dir
    dir_entry.right = 5    # dir -> module

    entries = [root, project, vba, vba_project, dir_entry, module_entry]

    # --- Directory sectors ---
    entries_per_sector = SECT_SIZE // 128
    dir_sector_count = -(-len(entries) // entries_per_sector)
    dir_bytes = bytearray()
    for entry in entries:
        dir_bytes += _pack_dir_entry(entry)
    while len(dir_bytes) < dir_sector_count * SECT_SIZE:
        dir_bytes += _pack_dir_entry(_DirEntry("", 0))
    dir_start_sector = len(sector_data) // SECT_SIZE
    sector_data += dir_bytes

    # --- FAT sector(s) ---
    total_data_sectors = len(sector_data) // SECT_SIZE  # streams + directory, before FAT itself
    fat_entries_needed = total_data_sectors + 1  # +1 placeholder, refined below
    fat_sector_count = 1
    while (total_data_sectors + fat_sector_count) > fat_sector_count * (SECT_SIZE // 4):
        fat_sector_count += 1
    fat_start_sector = total_data_sectors

    fat = [FREESECT] * (fat_sector_count * (SECT_SIZE // 4))
    # Chain each stream's occupied sectors (all single-chain, contiguous, sequential).
    for key, blob in streams.items():
        start = start_sectors[key]
        count = len(blob) // SECT_SIZE
        for i in range(count):
            sector_index = start + i
            fat[sector_index] = start + i + 1 if i < count - 1 else ENDOFCHAIN
    # Directory sector chain.
    for i in range(dir_sector_count):
        sector_index = dir_start_sector + i
        fat[sector_index] = dir_start_sector + i + 1 if i < dir_sector_count - 1 else ENDOFCHAIN
    # FAT sector(s) self-description.
    for i in range(fat_sector_count):
        fat[fat_start_sector + i] = FATSECT

    fat_bytes = b"".join(struct.pack("<L", value) for value in fat)
    sector_data += fat_bytes

    # --- Header (512 bytes) ---
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header[8:24] = b"\x00" * 16  # CLSID
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 0x0003)  # major version (3 = 512-byte sectors)
    struct.pack_into("<H", header, 28, 0xFFFE)  # byte order
    struct.pack_into("<H", header, 30, 9)       # sector shift (2^9=512)
    struct.pack_into("<H", header, 32, 6)       # mini sector shift (unused)
    struct.pack_into("<H", header, 34, 0)       # reserved
    struct.pack_into("<L", header, 36, 0)       # reserved
    struct.pack_into("<L", header, 40, 0)       # number of directory sectors (0 for v3)
    struct.pack_into("<L", header, 44, fat_sector_count)
    struct.pack_into("<L", header, 48, dir_start_sector)
    struct.pack_into("<L", header, 52, 0)       # transaction signature
    struct.pack_into("<L", header, 56, 4096)    # mini stream cutoff
    struct.pack_into("<L", header, 60, ENDOFCHAIN)  # first mini FAT sector
    struct.pack_into("<L", header, 64, 0)       # number of mini FAT sectors
    struct.pack_into("<L", header, 68, ENDOFCHAIN)  # first DIFAT sector
    struct.pack_into("<L", header, 72, 0)       # number of DIFAT sectors
    # DIFAT: first 109 FAT sector locations live in the header itself.
    for i in range(109):
        value = fat_start_sector + i if i < fat_sector_count else FREESECT
        struct.pack_into("<L", header, 76 + i * 4, value)

    return bytes(header) + bytes(sector_data)


def build_vba_project_bin(module_name: str, vba_source: str) -> bytes:
    module_stream_name = module_name
    module_bytes, text_offset = build_module_stream(vba_source)
    dir_stream_plain = build_dir_stream(module_name, module_stream_name, text_offset)
    dir_stream_compressed = compress_container(dir_stream_plain, min_container_bytes=4096)
    project_stream = build_project_stream(module_name) + b"\x00" * 4096
    vba_project_marker = b"\x00" * 4096
    return build_compound_file(project_stream, vba_project_marker, dir_stream_compressed, module_stream_name, module_bytes)


# ---------------------------------------------------------------------------
# Splice a vbaProject.bin into an openpyxl-built .xlsx to produce a real .xlsm
# ---------------------------------------------------------------------------

def build_xlsm(sheet_rows: list[list], module_name: str, vba_source: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    for row in sheet_rows:
        sheet.append(row)
    xlsx_buffer = io.BytesIO()
    workbook.save(xlsx_buffer)
    xlsx_buffer.seek(0)

    vba_bytes = build_vba_project_bin(module_name, vba_source)

    out_buffer = io.BytesIO()
    with zipfile.ZipFile(xlsx_buffer) as src, zipfile.ZipFile(out_buffer, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                )
                data = data.replace(
                    b"</Types>",
                    b'<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>',
                )
            if item.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rIdVBAProject" '
                    b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/vbaProject" '
                    b'Target="vbaProject.bin"/></Relationships>',
                )
            dst.writestr(item, data)
        dst.writestr("xl/vbaProject.bin", vba_bytes)
    return out_buffer.getvalue()

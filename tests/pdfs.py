"""Small valid PDFs built in code, for tests that need real ones."""

from __future__ import annotations

import hashlib
import zlib

# The standard security handler's password padding (PDF 1.7, 7.6.3.3).
_PDF_PAD = bytes.fromhex(
    "28bf4e5e4e758a41"  # pragma: allowlist secret
    "64004e56fffa0108"  # pragma: allowlist secret
    "2e2e00b6d0683e80"  # pragma: allowlist secret
    "2f0ca9fe6453697a"  # pragma: allowlist secret
)


def _rc4(key: bytes, data: bytes) -> bytes:
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) % 256
        box[i], box[j] = box[j], box[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out.append(byte ^ box[(box[i] + box[j]) % 256])
    return bytes(out)


_FILE_ID = b"0123456789abcdef"
_PERMISSIONS = -4


def _owner_password_only() -> tuple[bytes, bytes]:
    """An RC4 40-bit encryption dictionary with an empty user password.

    Returns the dictionary and the file key that encrypts the streams.
    """
    permissions, file_id = _PERMISSIONS, _FILE_ID
    owner = _rc4(hashlib.md5(b"owner" + _PDF_PAD[:27]).digest()[:5], _PDF_PAD)
    key = hashlib.md5(
        _PDF_PAD + owner + permissions.to_bytes(4, "little", signed=True) + file_id
    ).digest()[:5]
    user = _rc4(key, _PDF_PAD)
    encrypt = (
        f"<< /Filter /Standard /V 1 /R 2 /O <{owner.hex()}> /U <{user.hex()}> "
        f"/P {permissions} >>"
    ).encode()
    return encrypt, key


def _object_key(file_key: bytes, number: int) -> bytes:
    """The RC4 key for one object's strings and streams (generation 0)."""
    digest = hashlib.md5(file_key + number.to_bytes(3, "little") + b"\0\0").digest()
    return digest[: len(file_key) + 5]


def make_pdf(
    text: str | None = None, *, pages: int = 1, encrypted: bool = False
) -> bytes:
    """A small valid PDF: ``pages`` pages, each showing ``text`` or blank.

    A blank page stands in for a scan: pdfplumber opens it and finds no
    text, as it does for a page that is only an image. ``encrypted`` gives
    a PDF with only an owner password, which pdfplumber still opens and
    reads.
    """
    encrypt, file_key = _owner_password_only() if encrypted else (b"", b"")
    content = b""
    if text is not None:
        content = b"BT /F1 12 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i in range(pages):
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {5 + 2 * i} 0 R >>"
            ).encode()
        )
        stream = content
        if encrypted:
            stream = _rc4(_object_key(file_key, 5 + 2 * i), content)
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"/Size {len(objects) + 1} /Root 1 0 R".encode()
    if encrypted:
        file_id = _FILE_ID.hex()
        trailer += b" /Encrypt " + encrypt + f" /ID [<{file_id}> <{file_id}>]".encode()
    out += b"trailer\n<< " + trailer + b" >>\n"
    out += f"startxref\n{xref}\n%EOF\n".encode()
    return bytes(out)


# Damaged PDFs, made from a valid one, for the PDF validation tests.


def truncated_pdf(text: str | None = "Truncated receipt") -> bytes:
    """A valid PDF cut off before its xref table: qpdf rebuilds the xref."""
    data = make_pdf(text)
    return data[: data.rindex(b"xref")]


def wrong_startxref_pdf(text: str | None = "Misplaced xref receipt") -> bytes:
    """A valid PDF whose ``startxref`` points past the end of the file."""
    data = make_pdf(text)
    return data[: data.rindex(b"startxref")] + b"startxref\n999999\n%%EOF\n"


def broken_header_pdf(text: str | None = "Broken header receipt") -> bytes:
    """A valid PDF whose ``%PDF-`` header is damaged: qpdf only warns."""
    data = make_pdf(text)
    assert data.startswith(b"%PDF-")
    return b"%PDX-" + data[5:]


def garbage_pdf() -> bytes:
    """Bytes that are no PDF at all, as an attachment named ``.pdf`` can hold."""
    return b"this is not a pdf, only text in a file named receipt.pdf\n" * 20


# Hostile PDFs: tiny files whose checking would inflate memory or burn CPU.


def _raw_pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _double_flate_zeros(megabytes: int) -> bytes:
    """``megabytes`` MiB of zeros, deflated twice: a few kilobytes."""
    z = zlib.compressobj(9)
    chunk = bytes(1 << 20)
    once = b"".join(z.compress(chunk) for _ in range(megabytes)) + z.flush()
    return zlib.compress(once, 9)


def _stream(body: bytes, extra: bytes = b"") -> bytes:
    return (
        b"<< "
        + extra
        + b"/Length %d /Filter [/FlateDecode /FlateDecode] >>\nstream\n" % len(body)
        + body
        + b"\nendstream"
    )


def metadata_bomb_pdf(megabytes: int = 512) -> bytes:
    """A PDF whose XMP ``/Metadata`` inflates to ``megabytes`` MiB on save."""
    return _raw_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R /Metadata 4 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>",
            _stream(_double_flate_zeros(megabytes), b"/Type /Metadata /Subtype /XML "),
        ]
    )


def filter_chain_pdf(megabytes: int = 256) -> bytes:
    """A PDF whose page content is a filter chain that takes seconds to decode."""
    return _raw_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >>"
            b" /Contents 4 0 R >>",
            _stream(_double_flate_zeros(megabytes)),
        ]
    )

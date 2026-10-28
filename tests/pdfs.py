"""Small valid PDFs built in code, for tests that need real ones."""

from __future__ import annotations

import hashlib

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


def _owner_password_only(file_id: bytes) -> bytes:
    """An RC4 40-bit encryption dictionary with an empty user password."""
    permissions = -4
    owner = _rc4(hashlib.md5(b"owner" + _PDF_PAD[:27]).digest()[:5], _PDF_PAD)
    key = hashlib.md5(
        _PDF_PAD + owner + permissions.to_bytes(4, "little", signed=True) + file_id
    ).digest()[:5]
    user = _rc4(key, _PDF_PAD)
    return (
        f"<< /Filter /Standard /V 1 /R 2 /O <{owner.hex()}> /U <{user.hex()}> "
        f"/P {permissions} >>"
    ).encode()


def make_pdf(
    text: str | None = None, *, pages: int = 1, encrypted: bool = False
) -> bytes:
    """A small valid PDF: ``pages`` pages, each showing ``text`` or blank.

    A blank page stands in for a scan: pdfplumber opens it and finds no
    text, as it does for a page that is only an image. ``encrypted`` gives
    a blank PDF with only an owner password, which pdfplumber still opens.
    """
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
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream"
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
        file_id = b"0123456789abcdef"
        trailer += (
            b" /Encrypt "
            + _owner_password_only(file_id)
            + f" /ID [<{file_id.hex()}> <{file_id.hex()}>]".encode()
        )
    out += b"trailer\n<< " + trailer + b" >>\n"
    out += f"startxref\n{xref}\n%EOF\n".encode()
    return bytes(out)

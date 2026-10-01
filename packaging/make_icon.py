"""Render the embedded application mark to icon files for the installers.

The mark itself lives in ``subsyncr.qt.icons.APP_ICON_SVG`` so the running
application can render it without shipping a data file. PyInstaller, though,
wants a real ``.ico`` (Windows) and ``.icns`` (the macOS ``.app``) at build
time, so this writes both out. Re-run it only when the artwork changes; the
output is committed.

    uv run python packaging/make_icon.py
"""

from __future__ import annotations

from pathlib import Path
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from subsyncr.qt.icons import APP_ICON_SVG

# Explorer picks the small sizes, the taskbar and alt-tab the large ones.
SIZES = (16, 24, 32, 48, 64, 128, 256)

# Finder and the Dock want far larger frames than Explorer does. Each entry is
# an icns type code and the pixel size it holds; the "@2x" types are Retina
# frames that happen to share a pixel size with a larger 1x one.
ICNS_TYPES = (
    (b"ic11", 32),  # 16@2x
    (b"ic12", 64),  # 32@2x
    (b"ic07", 128),
    (b"ic13", 256),  # 128@2x
    (b"ic08", 256),
    (b"ic14", 512),  # 256@2x
    (b"ic09", 512),
    (b"ic10", 1024),  # 512@2x
)
COLOR = "#4f9de0"


def render(size: int, renderer: QSvgRenderer) -> QImage:
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return image


def as_png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # PySide accepts str here at runtime; its stub incorrectly requires bytes.
    if not image.save(buffer, "PNG"):  # type: ignore[reportCallIssue, reportArgumentType]
        raise SystemExit("could not encode a frame as PNG")
    return bytes(buffer.data().data())


def build_ico(frames: dict[int, bytes]) -> bytes:
    """Pack PNG frames into a multi-resolution .ico.

    Qt's own ICO writer takes a single image, so the container is assembled
    here to keep every size in one file. PNG-compressed entries are what the
    format has allowed since Vista, and are a fraction of the size of the
    uncompressed bitmaps it started with.
    """
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    directory = bytearray()
    payload = bytearray()
    for size, data in sorted(frames.items()):
        directory += struct.pack(
            "<BBBBHHII",
            # 256 is stored as 0; the format only has a byte for each axis.
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,  # palette size, meaningless for 32-bit
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    return header + bytes(directory) + bytes(payload)


def build_icns(frames: dict[int, bytes]) -> bytes:
    """Pack PNG frames into an .icns container.

    Every type in ICNS_TYPES accepts PNG data since OS X 10.7, so like the .ico
    this is just a header and a list of tagged PNGs - no iconutil, which only
    exists on a Mac, and no Pillow in the dependency tree.
    """
    body = bytearray()
    for code, size in ICNS_TYPES:
        data = frames[size]
        # The length covers the 8-byte entry header as well as the data.
        body += code + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(body)) + bytes(body)


def main() -> int:
    # Rendering needs a GUI application for the font and paint engines, but
    # not a window, so this runs headless in CI too.
    app = QGuiApplication([])
    markup = APP_ICON_SVG.replace("currentColor", COLOR)
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    if not renderer.isValid():
        raise SystemExit("APP_ICON_SVG did not parse as SVG")

    target = Path(__file__).resolve().parent / "icons"
    target.mkdir(parents=True, exist_ok=True)

    frames = {size: as_png(render(size, renderer)) for size in SIZES}
    ico = target / "subsyncr.ico"
    ico.write_bytes(build_ico(frames))
    print(f"wrote {ico} ({ico.stat().st_size} bytes, {len(frames)} sizes)")

    icns_sizes = {size for _, size in ICNS_TYPES}
    icns_frames = {size: as_png(render(size, renderer)) for size in icns_sizes}
    icns = target / "subsyncr.icns"
    icns.write_bytes(build_icns(icns_frames))
    print(f"wrote {icns} ({icns.stat().st_size} bytes, {len(ICNS_TYPES)} entries)")

    png = target / "subsyncr.png"
    png.write_bytes(frames[max(SIZES)])
    print(f"wrote {png} ({png.stat().st_size} bytes)")

    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

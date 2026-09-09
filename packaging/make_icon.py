"""Render the embedded application mark to icon files for the installers.

The mark itself lives in ``subsyncr.qt.icons.APP_ICON_SVG`` so the running
application can render it without shipping a data file. PyInstaller, though,
wants a real ``.ico`` at build time, so this writes one out. Re-run it only
when the artwork changes; the output is committed.

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

from subsyncr.qt.icons import APP_ICON_SVG  # noqa: E402

# Explorer picks the small sizes, the taskbar and alt-tab the large ones.
SIZES = (16, 24, 32, 48, 64, 128, 256)
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
    if not image.save(buffer, "PNG"):
        raise SystemExit("could not encode a frame as PNG")
    return bytes(buffer.data())


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

    png = target / "subsyncr.png"
    png.write_bytes(frames[max(SIZES)])
    print(f"wrote {png} ({png.stat().st_size} bytes)")

    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

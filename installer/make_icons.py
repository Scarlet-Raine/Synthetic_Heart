"""Build the installer's icons from the repository's logo artwork.

Two icons, on purpose:

* ``synth.ico`` keeps the dark rounded-square artwork. It is what a Windows
  shortcut, the Add/Remove Programs entry and the installer itself show, and the
  squircle is what makes a shortcut legible on any wallpaper.
* ``synth-tray.ico`` uses the same heart without the squircle
  (``docs/res/synth_logo_wblack.png``), cropped to the artwork and padded, so the
  notification-area icon sits on the taskbar with a transparent background instead
  of a black tile. The circuit half is black and the eye half is cyan: on a dark
  taskbar the eye reads, on a light one the traces read. That trade-off is the
  artwork's, not a bug.

Run from the repository root:  ``python installer/make_icons.py``
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = REPO_ROOT / "installer"

# Windows asks for these when it draws a taskbar, alt-tab or Explorer icon.
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

SQUIRCLE_SOURCE = REPO_ROOT / "website" / "assets" / "synth_logo_bg.png"
TRANSPARENT_SOURCE = REPO_ROOT / "docs" / "res" / "synth_logo_wblack.png"


def _square_canvas_with_margin(image: Image.Image, margin: float) -> Image.Image:
    """Crop to the visible artwork and centre it on a padded square canvas.

    Without this the heart keeps whatever empty space the artwork happens to have
    around it, which is a lot vertically, and the icon comes out small and
    off-centre in the notification area.
    """
    rgba = image.convert("RGBA")
    visible = (
        rgba.getchannel("A").point(lambda value: 255 if value > 8 else 0).getbbox()
    )
    if visible:
        rgba = rgba.crop(visible)

    side = int(max(rgba.size) * (1 + 2 * margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(
        rgba,
        ((side - rgba.width) // 2, (side - rgba.height) // 2),
    )
    return canvas


def _write_ico(source: Path, destination: Path, margin: float) -> Path:
    artwork = _square_canvas_with_margin(Image.open(source), margin)
    artwork.save(
        destination,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
    )
    return destination


def main() -> None:
    """Write every icon the installer ships, and report what was written."""
    written = [
        _write_ico(SQUIRCLE_SOURCE, ICON_DIR / "synth.ico", margin=0.0),
        _write_ico(TRANSPARENT_SOURCE, ICON_DIR / "synth-tray.ico", margin=0.06),
    ]

    # The WebUI's manifest and the installer's About box want a flat PNG too.
    logo = _square_canvas_with_margin(Image.open(SQUIRCLE_SOURCE), margin=0.0)
    png = ICON_DIR / "synth-256.png"
    logo.resize((256, 256), Image.Resampling.LANCZOS).save(png, format="PNG")
    written.append(png)

    for path in written:
        print(f"{path.relative_to(REPO_ROOT)}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()

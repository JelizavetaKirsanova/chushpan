"""Prepare macOS and interface icons from the user's unchanged original PNG."""

from pathlib import Path

from backend.app_bundle import icon_pixels, png_bytes, write_icon

ROOT = Path(__file__).resolve().parents[1]


def main():
    resources = ROOT / "packaging/macos/resources"
    write_icon(resources / "icon.icns", iconset=resources / "Chushpan.iconset")
    assets = ROOT / "frontend/public/assets"
    (assets / "chushpan-icon.png").write_bytes(png_bytes(icon_pixels(128)))
    (assets / "chushpan-favicon.png").write_bytes(png_bytes(icon_pixels(32)))


if __name__ == "__main__":
    main()

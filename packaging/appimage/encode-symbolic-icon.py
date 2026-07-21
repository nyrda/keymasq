#!/usr/bin/env python3
"""Rasterize a traditional GTK symbolic SVG without a runtime SVG loader."""

from __future__ import annotations

import argparse
import binascii
import copy
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

_ROLES = ("success", "warning", "error")
_DRAWABLES = {"circle", "ellipse", "path", "polygon", "polyline", "rect", "text"}


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _element_role(element: ElementTree.Element, inherited: str) -> str:
    classes = set(element.get("class", "").split())
    selected = classes.intersection(_ROLES)
    if len(selected) > 1:
        raise ValueError(f"symbolic SVG element has conflicting roles: {sorted(selected)}")
    return next(iter(selected), inherited)


def _recolor(element: ElementTree.Element, *, target: str, inherited: str = "foreground") -> None:
    role = _element_role(element, inherited)
    color = "#ff0000" if role == target else "#00ff00"
    local_name = _local_name(element)
    style = element.get("style", "")
    if "fill:" in style or "stroke:" in style:
        raise ValueError(
            "symbolic SVG style attributes are unsupported; use fill/stroke attributes"
        )

    if local_name in _DRAWABLES:
        if element.get("fill") != "none":
            element.set("fill", color)
        if "stroke" in element.attrib and element.get("stroke") != "none":
            element.set("stroke", color)
    else:
        for attribute in ("fill", "stroke"):
            if attribute in element.attrib and element.get(attribute) != "none":
                element.set(attribute, color)

    for child in element:
        _recolor(child, target=target, inherited=role)


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (
        abs(estimate - left),
        abs(estimate - above),
        abs(estimate - upper_left),
    )
    return (left, above, upper_left)[distances.index(min(distances))]


def _read_rgba_png(encoded: bytes, *, expected_width: int, expected_height: int) -> bytes:
    if not encoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("rsvg-convert did not produce a PNG")

    width = height = bit_depth = color_type = interlace = None
    compressed = bytearray()
    offset = 8
    while offset < len(encoded):
        length = struct.unpack(">I", encoded[offset : offset + 4])[0]
        chunk_type = encoded[offset + 4 : offset + 8]
        data = encoded[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", data)
        elif chunk_type == b"IDAT":
            compressed.extend(data)
        elif chunk_type == b"IEND":
            break

    if (width, height) != (expected_width, expected_height):
        raise ValueError(
            f"unexpected raster size: {width}x{height}, expected {expected_width}x{expected_height}"
        )
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError(
            "rsvg-convert PNG must be non-interlaced 8-bit RGB/RGBA, got "
            f"bit depth {bit_depth}, color type {color_type}, interlace {interlace}"
        )

    raw = zlib.decompress(bytes(compressed))
    bytes_per_pixel = 4 if color_type == 6 else 3
    stride = expected_width * bytes_per_pixel
    expected_length = expected_height * (stride + 1)
    if len(raw) != expected_length:
        raise ValueError(f"unexpected decompressed PNG length: {len(raw)}")

    pixels = bytearray(expected_height * stride)
    for row in range(expected_height):
        raw_offset = row * (stride + 1)
        filter_type = raw[raw_offset]
        source = raw[raw_offset + 1 : raw_offset + 1 + stride]
        output_offset = row * stride
        for column, value in enumerate(source):
            left = (
                pixels[output_offset + column - bytes_per_pixel] if column >= bytes_per_pixel else 0
            )
            above = pixels[output_offset + column - stride] if row else 0
            upper_left = (
                pixels[output_offset + column - stride - bytes_per_pixel]
                if row and column >= bytes_per_pixel
                else 0
            )
            if filter_type == 0:
                reconstructed = value
            elif filter_type == 1:
                reconstructed = value + left
            elif filter_type == 2:
                reconstructed = value + above
            elif filter_type == 3:
                reconstructed = value + ((left + above) // 2)
            elif filter_type == 4:
                reconstructed = value + _paeth(left, above, upper_left)
            else:
                raise ValueError(f"unsupported PNG filter type: {filter_type}")
            pixels[output_offset + column] = reconstructed & 0xFF
    if bytes_per_pixel == 4:
        return bytes(pixels)

    rgba = bytearray(expected_width * expected_height * 4)
    for pixel_index in range(expected_width * expected_height):
        source_offset = pixel_index * 3
        output_offset = pixel_index * 4
        rgba[output_offset : output_offset + 3] = pixels[source_offset : source_offset + 3]
        rgba[output_offset + 3] = 0xFF
    return bytes(rgba)


def _render_plane(source: Path, *, role: str, width: int, height: int) -> bytes:
    root = copy.deepcopy(ElementTree.parse(source).getroot())
    _recolor(root, target=role)
    with tempfile.NamedTemporaryFile(suffix=".svg") as svg_file:
        ElementTree.ElementTree(root).write(svg_file.name, encoding="utf-8", xml_declaration=True)
        png = subprocess.run(
            [
                "rsvg-convert",
                "--width",
                str(width),
                "--height",
                str(height),
                "--keep-aspect-ratio",
                svg_file.name,
            ],
            check=True,
            capture_output=True,
        ).stdout

    return _read_rgba_png(png, expected_width=width, expected_height=height)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _write_rgba_png(path: Path, *, width: int, height: int, rgba: bytes) -> None:
    stride = width * 4
    rows = b"".join(
        b"\0" + rgba[offset : offset + stride] for offset in range(0, len(rgba), stride)
    )
    encoded = b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(rows, level=9)),
            _png_chunk(b"IEND", b""),
        )
    )
    path.write_bytes(encoded)


def encode(source: Path, output: Path, *, width: int, height: int) -> None:
    planes = [_render_plane(source, role=role, width=width, height=height) for role in _ROLES]
    rgba = bytearray(width * height * 4)
    for pixel_index in range(width * height):
        output_offset = pixel_index * 4
        for plane_index, pixels in enumerate(planes):
            source_offset = pixel_index * 4
            rgba[output_offset + plane_index] = pixels[source_offset]
            if plane_index == 0:
                rgba[output_offset + 3] = pixels[source_offset + 3]
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_rgba_png(output, width=width, height=height, rgba=bytes(rgba))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("size", type=int)
    args = parser.parse_args()
    encode(args.source, args.output, width=args.size, height=args.size)


if __name__ == "__main__":
    main()

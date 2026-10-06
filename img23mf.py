"""Convert a raster image to a multi-part, multi-material printable plaque."""

from __future__ import annotations

import io
import json
import math
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final
from zipfile import ZIP_DEFLATED, ZipFile

import shapely
import typer
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError
from shapely.geometry import Polygon

CORE: Final = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
app = typer.Typer(add_completion=False)


@dataclass(frozen=True, slots=True)
class Raster:
    image: Image.Image
    labels: tuple[int, ...]
    colors: tuple[str, ...]
    width_mm: float
    height_mm: float


def prepare_image(
    source: Path, colors: int, width_mm: float, pixel_mm: float, background: str,
    vector: bool = False,
) -> Raster:
    """Apply EXIF orientation, flatten transparency and quantize without dithering."""
    with Image.open(source) as opened:
        rgba = ImageOps.exif_transpose(opened).convert("RGBA")
    flattened = Image.new("RGBA", rgba.size, ImageColor.getcolor(background, "RGBA"))
    flattened.alpha_composite(rgba)
    height_mm = width_mm * rgba.height / rgba.width
    spacing = pixel_mm / 4 if vector else pixel_mm
    columns = max(1, min(rgba.width, int(width_mm / spacing)))
    rows = max(1, min(rgba.height, int(height_mm / spacing)))
    if columns * rows > 1_000_000:
        raise typer.BadParameter("Grid exceeds 1,000,000 cells; increase --pixel-mm.")
    resized = flattened.convert("RGB").resize((columns, rows), Image.Resampling.LANCZOS)
    if vector and min(columns, rows) >= 8:
        resized = resized.filter(ImageFilter.MedianFilter(3))
    indexed = resized.quantize(
        colors=colors, method=Image.Quantize.MAXCOVERAGE, kmeans=20,
        dither=Image.Dither.NONE,
    )
    if vector and min(columns, rows) >= 8:
        indexed = indexed.filter(ImageFilter.ModeFilter(3))
    raw = tuple(indexed.tobytes())
    counts = Counter(raw)
    used = sorted(counts, key=lambda index: (-counts[index], index))
    remap = {old: new for new, old in enumerate(used)}
    palette = indexed.getpalette()
    if palette is None:
        raise RuntimeError("Quantization produced no palette")
    hex_colors = tuple(
        "#{:02X}{:02X}{:02X}".format(*palette[index * 3 : index * 3 + 3])
        for index in used
    )
    return Raster(
        indexed.convert("RGB"), tuple(remap[index] for index in raw),
        hex_colors, width_mm, height_mm,
    )


def rectangles(raster: Raster) -> list[list[tuple[int, int, int, int]]]:
    """Partition every colour into non-overlapping rectangles on the pixel grid."""
    width, height = raster.image.size
    consumed = bytearray(width * height)
    result: list[list[tuple[int, int, int, int]]] = [[] for _ in raster.colors]
    for y in range(height):
        for x in range(width):
            offset = y * width + x
            if consumed[offset]:
                continue
            color = raster.labels[offset]
            right = x + 1
            while right < width:
                pos = y * width + right
                if consumed[pos] or raster.labels[pos] != color:
                    break
                right += 1
            bottom = y + 1
            while bottom < height and all(
                not consumed[bottom * width + column]
                and raster.labels[bottom * width + column] == color
                for column in range(x, right)
            ):
                bottom += 1
            for row in range(y, bottom):
                consumed[row * width + x : row * width + right] = b"\x01" * (right - x)
            result[color].append((x, y, right, bottom))
    return result


def add_box(
    vertices: ET.Element, triangles: ET.Element, offset: int,
    x0: float, y0: float, x1: float, y1: float, z0: float, z1: float,
) -> None:
    """Append a closed outward-facing box; adjacent boxes have independent vertices."""
    points = (
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    )
    for x, y, z in points:
        ET.SubElement(vertices, "vertex", x=f"{x:.9f}", y=f"{y:.9f}", z=f"{z:.9f}")
    faces = (
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    )
    for a, b, c in faces:
        ET.SubElement(
            triangles, "triangle", v1=str(offset + a),
            v2=str(offset + b), v3=str(offset + c),
        )


def xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def vector_regions(raster: Raster, tolerance_mm: float) -> list[list[Polygon]]:
    """Trace shared boundaries, simplifying the entire coverage without gaps."""
    width, height = raster.image.size
    dx, dy = raster.width_mm / width, raster.height_mm / height
    merged = [
        shapely.union_all([
            shapely.box(x0 * dx, (height - y1) * dy, x1 * dx, (height - y0) * dy)
            for x0, y0, x1, y1 in group
        ])
        for group in rectangles(raster)
    ]
    # Noding synchronizes vertices at junctions between three or more colours.
    edges = shapely.node(shapely.union_all([shape.boundary for shape in merged]))
    polygons = list(shapely.get_parts(shapely.polygonize(shapely.get_parts(edges))))
    simplified = shapely.coverage_simplify(polygons, tolerance_mm, simplify_boundary=False)
    result: list[list[Polygon]] = [[] for _ in raster.colors]
    for original, smooth in zip(polygons, simplified, strict=True):
        point = original.representative_point()
        x = min(width - 1, max(0, int(point.x / dx)))
        y = min(height - 1, max(0, height - 1 - int(point.y / dy)))
        color = raster.labels[y * width + x]
        result[color].append(shapely.orient_polygons(smooth))
    return result


def add_polygon(
    vertices: ET.Element, triangles: ET.Element, polygon: Polygon,
    offset: int, z0: float, z1: float,
) -> int:
    """Extrude a polygon with holes using a constrained, watertight triangulation."""
    indices: dict[tuple[float, float, float], int] = {}

    def vertex(x: float, y: float, z: float) -> int:
        point = (x, y, z)
        if point not in indices:
            indices[point] = offset + len(indices)
            ET.SubElement(vertices, "vertex", x=f"{x:.9f}", y=f"{y:.9f}", z=f"{z:.9f}")
        return indices[point]

    def triangle(a: int, b: int, c: int) -> None:
        ET.SubElement(triangles, "triangle", v1=str(a), v2=str(b), v3=str(c))

    for face in shapely.get_parts(shapely.constrained_delaunay_triangles(polygon)):
        points = list(face.exterior.coords)[:3]
        a, b, c = points
        if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) < 0:
            b, c = c, b
        triangle(vertex(*a, z1), vertex(*b, z1), vertex(*c, z1))
        triangle(vertex(*a, z0), vertex(*c, z0), vertex(*b, z0))
    for ring in (polygon.exterior, *polygon.interiors):
        points = list(ring.coords)
        for a, b in zip(points, points[1:]):
            triangle(vertex(*a, z0), vertex(*b, z0), vertex(*b, z1))
            triangle(vertex(*a, z0), vertex(*b, z1), vertex(*a, z1))
    return len(indices)


def vector_preview(raster: Raster, regions: list[list[Polygon]], output: Path) -> Image.Image:
    """Save the actual vector contours as SVG and render an antialiased preview."""
    svg = ET.Element(
        "svg", xmlns="http://www.w3.org/2000/svg",
        viewBox=f"0 0 {raster.width_mm} {raster.height_mm}",
        width=f"{raster.width_mm}mm", height=f"{raster.height_mm}mm",
    )
    scale = 2048 / max(raster.width_mm, raster.height_mm)
    size = (max(1, round(raster.width_mm * scale)), max(1, round(raster.height_mm * scale)))
    preview = Image.new("RGB", size, raster.colors[0])
    for color, polygons in zip(raster.colors, regions, strict=True):
        paths: list[str] = []
        mask = Image.new("L", size)
        draw = ImageDraw.Draw(mask)
        # Paint enclosing rings before islands inside their holes.
        for polygon in sorted(polygons, key=lambda shape: shape.envelope.area, reverse=True):
            for index, ring in enumerate((polygon.exterior, *polygon.interiors)):
                points = [(x, raster.height_mm - y) for x, y in ring.coords]
                paths.append("M" + " L".join(f"{x:.6f},{y:.6f}" for x, y in points) + " Z")
                draw.polygon([(x * scale, y * scale) for x, y in points], fill=255 if index == 0 else 0)
        ET.SubElement(svg, "path", d=" ".join(paths), fill=color, **{"fill-rule": "evenodd"})
        preview.paste(ImageColor.getrgb(color), mask=mask)
    output.write_bytes(xml_bytes(svg))
    return preview.resize(
        (max(1, size[0] // 2), max(1, size[1] // 2)), Image.Resampling.LANCZOS,
    )


def export_3mf(
    raster: Raster, output: Path, base_mm: float, color_mm: float,
    regions: list[list[Polygon]] | None = None,
) -> int:
    """Export standard 3MF materials plus Orca/Bambu part-to-extruder metadata."""
    model = ET.Element("model", unit="millimeter", xmlns=CORE)
    ET.SubElement(model, "metadata", name="Application").text = "img23mf"
    ET.SubElement(model, "metadata", name="BambuStudio:3mfVersion").text = "1"
    resources = ET.SubElement(model, "resources")
    material_id = len(raster.colors) + 2
    materials = ET.SubElement(resources, "basematerials", id=str(material_id))
    for number, color in enumerate(raster.colors, 1):
        ET.SubElement(materials, "base", name=f"Filament {number} {color}", displaycolor=color + "FF")
    groups = rectangles(raster)
    width, height = raster.image.size
    dx, dy = raster.width_mm / width, raster.height_mm / height
    total_triangles = 0
    for index, group in enumerate(groups):
        obj = ET.SubElement(
            resources, "object", id=str(index + 1), type="model",
            name=f"Color {index + 1} {raster.colors[index]}",
            pid=str(material_id), pindex=str(index),
        )
        mesh = ET.SubElement(obj, "mesh")
        vertices = ET.SubElement(mesh, "vertices")
        triangles = ET.SubElement(mesh, "triangles")
        offset = 0
        if index == 0:
            add_box(vertices, triangles, offset, 0, 0, raster.width_mm, raster.height_mm, 0, base_mm)
            offset += 8
        for x0, y0, x1, y1 in (group if regions is None else []):
            # Image rows point down, model Y points up: preserve the view from above.
            add_box(
                vertices, triangles, offset, x0 * dx, (height - y1) * dy,
                x1 * dx, (height - y0) * dy, base_mm, base_mm + color_mm,
            )
            offset += 8
        if regions is not None:
            for polygon in regions[index]:
                offset += add_polygon(vertices, triangles, polygon, offset, base_mm, base_mm + color_mm)
        total_triangles += len(triangles)
    assembly_id = len(raster.colors) + 1
    assembly = ET.SubElement(resources, "object", id=str(assembly_id), type="model", name=output.stem)
    components = ET.SubElement(assembly, "components")
    for index in range(len(raster.colors)):
        ET.SubElement(components, "component", objectid=str(index + 1))
    ET.SubElement(ET.SubElement(model, "build"), "item", objectid=str(assembly_id))

    settings = ET.Element("config")
    obj_config = ET.SubElement(settings, "object", id=str(assembly_id))
    ET.SubElement(obj_config, "metadata", key="name", value=output.stem)
    ET.SubElement(obj_config, "metadata", key="extruder", value="1")
    for index, color in enumerate(raster.colors):
        part = ET.SubElement(obj_config, "part", id=str(index + 1), subtype="normal_part")
        ET.SubElement(part, "metadata", key="name", value=f"Color {index + 1} {color}")
        ET.SubElement(part, "metadata", key="extruder", value=str(index + 1))

    types = ET.Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    ET.SubElement(types, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types, "Default", Extension="model", ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml")
    ET.SubElement(types, "Default", Extension="config", ContentType="application/octet-stream")
    ET.SubElement(types, "Default", Extension="png", ContentType="image/png")
    rels = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    ET.SubElement(
        rels, "Relationship", Target="/3D/3dmodel.model", Id="rel0",
        Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
    )
    ET.SubElement(
        rels, "Relationship", Target="/Metadata/preview.png", Id="rel1",
        Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
    )
    preview = io.BytesIO()
    raster.image.save(preview, format="PNG")
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", xml_bytes(types))
        archive.writestr("_rels/.rels", xml_bytes(rels))
        archive.writestr("3D/3dmodel.model", xml_bytes(model))
        archive.writestr("Metadata/model_settings.config", xml_bytes(settings))
        archive.writestr("Metadata/project_settings.config", json.dumps({
            "filament_colour": list(raster.colors),
            "filament_type": ["PLA"] * len(raster.colors),
            "filament_diameter": ["1.75"] * len(raster.colors),
            "filament_is_support": ["0"] * len(raster.colors),
        }))
        archive.writestr("Metadata/preview.png", preview.getvalue())
    return total_triangles


@app.command()
def convert(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    colors: Annotated[int, typer.Option("--colors", "-c", min=1, max=256)] = 4,
    width_mm: Annotated[float, typer.Option(min=1)] = 100.0,
    pixel_mm: Annotated[float, typer.Option(min=0.1)] = 0.6,
    base_mm: Annotated[float, typer.Option(min=0.2)] = 0.8,
    color_mm: Annotated[float, typer.Option(min=0.2)] = 0.6,
    background: Annotated[str, typer.Option()] = "#FFFFFF",
    vector: Annotated[bool, typer.Option("--vector/--raster")] = True,
    smooth_mm: Annotated[float, typer.Option(min=0, max=2)] = 0.15,
) -> None:
    """Turn an image into a flat multi-colour plaque for OrcaSlicer / Bambu Studio."""
    for value in (width_mm, pixel_mm, base_mm, color_mm, smooth_mm):
        if not math.isfinite(value):
            raise typer.BadParameter("Dimensions must be finite numbers.")
    destination = output or source.with_suffix(".3mf")
    if destination.suffix.lower() != ".3mf":
        raise typer.BadParameter("Output must have a .3mf extension.")
    if destination.resolve() == source.resolve():
        raise typer.BadParameter("Output must not overwrite the source image.")
    preview_path = destination.with_suffix(".preview.png")
    palette_path = destination.with_suffix(".palette.json")
    svg_path = destination.with_suffix(".svg")
    if any(path.exists() for path in (destination, preview_path, palette_path, svg_path)):
        raise typer.BadParameter("Output already exists; choose a different --output.")
    if not destination.parent.is_dir():
        raise typer.BadParameter("Output directory does not exist.")
    try:
        ImageColor.getrgb(background)
    except ValueError as error:
        raise typer.BadParameter("Invalid --background color.") from error
    try:
        raster = prepare_image(source, colors, width_mm, pixel_mm, background, vector)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise typer.BadParameter(f"Cannot read image: {error}") from error
    regions = vector_regions(raster, smooth_mm) if vector else None
    if regions is not None:
        preview = vector_preview(raster, regions, svg_path)
    else:
        preview = raster.image
    triangles = export_3mf(raster, destination, base_mm, color_mm, regions)
    preview.save(preview_path)
    counts = Counter(raster.labels)
    palette_path.write_text(json.dumps({
        "width_mm": raster.width_mm, "height_mm": raster.height_mm,
        "base_mm": base_mm, "color_mm": color_mm,
        "grid": list(raster.image.size),
        "geometry": "vector" if vector else "raster",
        "smooth_mm": smooth_mm if vector else 0,
        "filaments": [
            {"slot": index + 1, "color": color,
             "coverage": round(counts[index] / len(raster.labels), 4),
             "includes_base": index == 0}
            for index, color in enumerate(raster.colors)
        ],
    }, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Created {destination} ({destination.stat().st_size / 1024:.0f} KiB)")
    typer.echo(
        f"{raster.width_mm:.1f} x {raster.height_mm:.1f} x "
        f"{base_mm + color_mm:.1f} mm; {len(raster.colors)} colors; "
        f"grid {raster.image.width} x {raster.image.height}; {triangles} triangles"
    )
    for index, color in enumerate(raster.colors, 1):
        typer.echo(f"  Filament {index}: {color}")
    typer.echo(f"Preview: {preview_path}\nPalette: {palette_path}")
    if vector:
        typer.echo(f"Vector contours: {svg_path}")


if __name__ == "__main__":
    app()

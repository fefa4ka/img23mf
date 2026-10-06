"""Geometric and CLI regression checks using actual image files and ZIP output."""

import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image
from typer.testing import CliRunner

from img23mf import CORE, app, export_3mf, prepare_image, rectangles, vector_preview, vector_regions


@pytest.mark.parametrize("vector", [False, True])
def test_geometry_and_materials_when_image_has_multiple_colors(tmp_path: Path, vector: bool) -> None:
    # Given an asymmetric, two-colour raster.
    image = Image.new("RGB", (3, 2), "red")
    image.putpixel((0, 0), (0, 0, 255))
    source = tmp_path / "input.png"
    image.save(source)
    raster = prepare_image(source, 2, 3, 1, "#FFFFFF")
    output = tmp_path / "result.3mf"

    # When exported with a base and coloured top.
    export_3mf(raster, output, 0.8, 0.6, vector_regions(raster, 0) if vector else None)

    # Then all shells are closed, outward-facing and exactly fill the plaque.
    ns = {"m": CORE}
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        project = json.loads(archive.read("Metadata/project_settings.config"))
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        extensions = {entry.get("Extension") for entry in types}
        assert all(
            name.rsplit(".", 1)[-1] in extensions
            for name in archive.namelist() if name != "[Content_Types].xml"
        )
    assert project["filament_colour"] == list(raster.colors)
    parts = config.findall("./object/part")
    assert len(parts) == 2
    assert [p.find("metadata[@key='extruder']").get("value") for p in parts] == ["1", "2"]
    assert len(model.findall("./m:resources/m:basematerials/m:base", ns)) == 2
    assembly_id = model.find("./m:build/m:item", ns).get("objectid")
    assembly = model.find(f"./m:resources/m:object[@id='{assembly_id}']", ns)
    assert len(assembly.findall("./m:components/m:component", ns)) == 2
    volume = 0.0
    for mesh in model.findall("./m:resources/m:object/m:mesh", ns):
        points = [
            tuple(float(v.get(axis)) for axis in ("x", "y", "z"))
            for v in mesh.findall("./m:vertices/m:vertex", ns)
        ]
        edges: Counter[tuple[int, int]] = Counter()
        for face in mesh.findall("./m:triangles/m:triangle", ns):
            a, b, c = [int(face.get(key)) for key in ("v1", "v2", "v3")]
            for start, end in ((a, b), (b, c), (c, a)):
                edges[(start, end)] += 1
            p, q, r = points[a], points[b], points[c]
            volume += (
                p[0] * (q[1] * r[2] - q[2] * r[1])
                + p[1] * (q[2] * r[0] - q[0] * r[2])
                + p[2] * (q[0] * r[1] - q[1] * r[0])
            ) / 6
        assert all(count == 1 and edges[(end, start)] == 1 for (start, end), count in edges.items())
    assert volume == pytest.approx(3 * 2 * 1.4)
    # The blue upper-left source cell remains in the model's upper-left corner.
    blue = raster.colors.index("#0000FF")
    blue_mesh = model.find(f"./m:resources/m:object[@id='{blue + 1}']/m:mesh", ns)
    blue_points = blue_mesh.findall("./m:vertices/m:vertex", ns)
    assert {float(v.get("x")) for v in blue_points} == {0, 1}
    assert {float(v.get("y")) for v in blue_points} == {1, 2}


def test_rectangle_partition_when_colors_touch_diagonally(tmp_path: Path) -> None:
    # Given a checkerboard with diagonal contacts and isolated cells.
    image = Image.new("RGB", (7, 5))
    for y in range(5):
        for x in range(7):
            image.putpixel((x, y), (255, 255, 255) if (x + y) % 2 else (0, 0, 0))
    source = tmp_path / "checker.png"
    image.save(source)
    raster = prepare_image(source, 2, 7, 1, "#FFFFFF")

    # When rectangles partition the colour grid.
    groups = rectangles(raster)

    # Then every cell appears once with its original label.
    cells: Counter[tuple[int, int]] = Counter()
    for color, group in enumerate(groups):
        for x0, y0, x1, y1 in group:
            for y in range(y0, y1):
                for x in range(x0, x1):
                    cells[(x, y)] += 1
                    assert raster.labels[y * 7 + x] == color
    assert len(cells) == 35
    assert set(cells.values()) == {1}


def test_cli_when_transparency_and_single_color_limit(tmp_path: Path) -> None:
    # Given a transparent image and a one-filament printer.
    source = tmp_path / "transparent.png"
    Image.new("RGBA", (4, 2), (255, 0, 0, 0)).save(source)
    output = tmp_path / "print.3mf"

    # When the actual CLI converts it.
    result = CliRunner().invoke(app, [
        str(source), "-c", "1", "--width-mm", "4", "--pixel-mm", "1",
        "--background", "#00FF00", "-o", str(output),
    ])

    # Then all three deliverables exist and use one green filament.
    assert result.exit_code == 0, result.output
    palette = json.loads(output.with_suffix(".palette.json").read_text())
    assert palette["filaments"][0]["color"] == "#00FF00"
    assert len(palette["filaments"]) == 1
    assert palette["height_mm"] == 2
    assert output.exists()
    with Image.open(output.with_suffix(".preview.png")) as preview:
        assert preview.getpixel((0, 0)) == (0, 255, 0)


@pytest.mark.parametrize("args", [
    ["--colors", "0"], ["--colors", "257"],
    ["--width-mm", "nan"], ["--base-mm", "inf"],
    ["--background", "not-a-color"], ["--output", "bad.stl"],
])
def test_cli_rejects_invalid_parameters_when_input_exists(tmp_path: Path, args: list[str]) -> None:
    # Given a valid input file and invalid boundary parameters.
    source = tmp_path / "input.png"
    Image.new("RGB", (2, 2)).save(source)

    # When called with those parameters.
    result = CliRunner().invoke(app, [str(source), *args])

    # Then conversion fails without producing a model.
    assert result.exit_code != 0
    assert not source.with_suffix(".3mf").exists()


def test_cli_preserves_files_when_output_exists(tmp_path: Path) -> None:
    # Given an existing output belonging to the user.
    source = tmp_path / "input.png"
    Image.new("RGB", (2, 2)).save(source)
    output = source.with_suffix(".3mf")
    output.write_bytes(b"existing")

    # When conversion uses that output path.
    result = CliRunner().invoke(app, [str(source)])

    # Then the existing content survives.
    assert result.exit_code != 0
    assert output.read_bytes() == b"existing"


def test_vector_preserves_holes_and_islands_when_regions_are_nested(tmp_path: Path) -> None:
    # Given a white frame surrounding a black field with a separate white island.
    image = Image.new("RGB", (7, 7), "white")
    for y in range(1, 6):
        for x in range(1, 6):
            image.putpixel((x, y), (0, 0, 0))
    image.putpixel((3, 3), (255, 255, 255))
    source = tmp_path / "nested.png"
    image.save(source)
    raster = prepare_image(source, 2, 7, 1, "#FFFFFF")

    # When traced and rendered as actual vector contours.
    regions = vector_regions(raster, 0)
    preview = vector_preview(raster, regions, tmp_path / "nested.svg")

    # Then colour areas, holes and the nested island all survive.
    areas = [sum(p.area for p in group) for group in regions]
    counts = Counter(raster.labels)
    assert areas == pytest.approx([counts[i] for i in range(len(raster.colors))])
    assert preview.getpixel((512, 512)) == (255, 255, 255)
    assert preview.getpixel((256, 256)) == (0, 0, 0)
    assert preview.getpixel((20, 20)) == (255, 255, 255)

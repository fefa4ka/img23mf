# img23mf

Turn a raster image into a flat, multi-color 3D-printable plaque.

Choose a maximum number of colors and a physical width. img23mf reduces the
palette, traces and simplifies shared color boundaries, then extrudes those
regions onto a solid backing. The output is a multi-part 3MF with filament
assignments for OrcaSlicer and Bambu Studio.

PNG, JPEG, WebP, BMP, TIFF and other formats supported by
[Pillow](https://pillow.readthedocs.io/) work as inputs. Processing is local:
your images are not uploaded anywhere.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
uv can install a compatible Python version for you.

```sh
git clone https://github.com/fefa4ka/img23mf.git
cd img23mf
uv sync --locked --no-dev
uv run img23mf /path/to/image.jpg --colors 4 --width-mm 100
```

Or install the command without cloning the repository:

```sh
uv tool install git+https://github.com/fefa4ka/img23mf.git
img23mf /path/to/image.png --colors 4
```

The project is not published to PyPI yet. Install from GitHub or the source
checkout rather than running `pip install img23mf`.

## Output

For `image.jpg`, the default output files are created next to the input:

| File | Contents |
| --- | --- |
| `image.3mf` | One assembled model with a separate part for each color |
| `image.preview.png` | Preview of the simplified color regions |
| `image.palette.json` | Filament slots, RGB colors, image coverage and dimensions |
| `image.svg` | Vector contours used to construct the model |

The SVG is produced in vector mode only. Existing output files are never
overwritten; use `--output` to choose a different name. The output directory
must already exist.

```sh
img23mf image.webp --colors 6 --width-mm 150 --output plaque.3mf
img23mf logo.png --colors 3 --background "#FFFFFF" --smooth-mm 0.2
img23mf image.jpg --colors 4 --raster --output pixel-art.3mf
img23mf --help
```

When working from a source checkout, prefix these commands with `uv run`.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--colors`, `-c` | `4` | Maximum number of colors, including the backing; 1-256 |
| `--width-mm` | `100` | Plaque width in millimeters; height follows the image aspect ratio |
| `--pixel-mm` | `0.6` | Detail scale; vector mode analyzes a grid four times finer |
| `--base-mm` | `0.8` | Thickness of the solid backing |
| `--color-mm` | `0.6` | Thickness of the colored regions above the backing |
| `--smooth-mm` | `0.15` | Shared-boundary simplification tolerance; `0` disables simplification |
| `--background` | `#FFFFFF` | Background color for transparent input images |
| `--output`, `-o` | Input stem + `.3mf` | Output path |
| `--vector` / `--raster` | `--vector` | Extruded vector regions or rectangular pixel blocks |

The total thickness is `base-mm + color-mm`: 1.4 mm with the defaults.
The most common image color is also used for the backing, so it does not
consume an extra filament slot. An image with fewer distinct colors than
the requested limit uses fewer slots.

## How vector mode works

1. Apply EXIF orientation and composite transparency onto the chosen background.
2. Resize to the analysis grid and remove small image noise.
3. Reduce the palette without dithering; remove isolated color pixels.
4. Trace shared boundaries and simplify them together to avoid gaps between colors.
5. Triangulate each region, preserving holes and nested islands, and extrude it.
6. Export the assembled model, material metadata, preview and vector contours.

Vector mode is intended for cleaner outlines than individual pixel blocks.
It does not recover detail missing from the input or replace a hand-traced logo.
The contours are polygonal, not editable Bezier curves. Reducing `--pixel-mm`
preserves more detail but can substantially increase processing time and file
size. Increasing `--smooth-mm` reduces contour detail.

The analysis grid is limited to 1,000,000 cells. For a large or tall image,
increase `--pixel-mm` if the converter reports that limit.

## Printing and slicer compatibility

Open the 3MF in OrcaSlicer or Bambu Studio, select your printer and print profile,
and check the filament assignments for each part. Map each slot to an actual
spool in your printer's multi-material system.

- **OrcaSlicer:** import and re-export were verified with version 2.4.2. The
  checked four-color model retained all part assignments and colors, was
  reported as manifold, and required no mesh repairs.
- **Bambu Studio:** the exporter includes Orca/Bambu part-to-extruder metadata.
  Import into Bambu Studio has not been independently verified.
- **Other slicers:** standard 3MF base materials are included, but a slicer
  may ignore the application-specific filament assignments. Check or assign
  colors manually after import.

The output is a model, not G-code or a printer-specific project profile.
Multiple colors occur in the same layer, so color changes by layer height alone
are not sufficient. Use a printer with multi-material capability, or a workflow
that can switch filaments within a layer.

RGB colors are palette suggestions, not physically mixed plastic colors.
Choose the closest available spools. Small details and islands can disappear
when they are thinner than your nozzle's extrusion width: inspect the sliced
layers before printing. For small lettering, consider a larger plaque.

Choose thicknesses that are multiples of your layer height. At 0.2 mm per
layer, the defaults give four backing layers and three colored layers. The
model has a flat bottom and does not require supports. Actual slicing under
your printer profile and physical printing are still your final checks.

## Development

```sh
uv sync --locked
uv run --locked pytest -q
uv build
```

The tests generate their own images and cover closed meshes, triangle
orientation, plaque volume, color assignments, image orientation, transparency,
vector holes and nested islands, CLI validation, and output-file protection.

GitHub Actions runs the tests, builds the package and checks the installed CLI
on Linux, macOS and Windows with Python 3.11 and 3.13.
User images, generated models and local configuration are excluded from Git.

To report a problem, include the command, operating system, Python version,
slicer version and the error output in a
[GitHub issue](https://github.com/fefa4ka/img23mf/issues).
Share an input image only if you want it to be public.

## License

[MIT](LICENSE).

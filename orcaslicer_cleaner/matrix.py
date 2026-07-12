"""Read-only coverage matrix reporting for filament and process profiles.

Shows, at a glance, which materials/processes exist for which printers —
surfacing redundancy (many profiles per cell) and coverage gaps (few/none)
without mutating anything on disk.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .config import DEFAULT_CONFIG, Config
from .deduplicator import _parse_filament_name, _parse_process_name
from .models import Profile, ProfileCategory

UNKNOWN_COLUMN = "?"
ALL_COLUMN = "ALL⚠"


def _machine_model(machine_name: str) -> str:
    """First ' - ' segment of a machine name (the printer model)."""
    return machine_name.split(" - ", 1)[0].strip()


def _filament_row_key(profile: Profile, config: Config = DEFAULT_CONFIG) -> str:
    """Row label for a filament profile: 'Material - Brand', or the full
    name if it doesn't match the expected naming convention."""
    parsed = _parse_filament_name(profile.name, config)
    if parsed is None:
        return profile.name
    material, brand, _hardware = parsed
    return f"{material} - {brand}"


def _process_row_key(profile: Profile, config: Config = DEFAULT_CONFIG) -> str:
    """Row label for a process profile: 'LayerHeight - Purpose', or the
    full name if it doesn't match the expected naming convention."""
    parsed = _parse_process_name(profile.name, config)
    if parsed is None:
        return profile.name
    layer_height, purpose, _hardware = parsed
    return f"{layer_height} - {purpose}"


def _build_matrix(
    profiles: list[Profile],
    columns: list[str],
    row_key_fn,
    column_for_ref_fn,
) -> tuple[dict[str, dict[str, int]], dict[str, int], int]:
    """Shared counting logic.

    Returns (matrix[row][column] -> count, empty_cp_counts[row] -> count,
    total profile count).

    `column_for_ref_fn(ref, columns)` maps a single compatible_printers
    entry to a column name (or UNKNOWN_COLUMN if not recognized).
    """
    matrix: dict[str, dict[str, int]] = {}
    empty_cp: dict[str, int] = {}

    for p in profiles:
        row = row_key_fn(p)
        row_counts = matrix.setdefault(row, {c: 0 for c in columns})
        row_counts.setdefault(UNKNOWN_COLUMN, 0)

        refs = p.compatible_printers
        if not refs:
            empty_cp[row] = empty_cp.get(row, 0) + 1
            continue

        # Count each distinct column once per profile (a profile that
        # references two machines of the same model shouldn't double-count
        # for process rows).
        hit_columns: set[str] = set()
        for ref in refs:
            hit_columns.add(column_for_ref_fn(ref, columns))
        for col in hit_columns:
            row_counts[col] = row_counts.get(col, 0) + 1

    return matrix, empty_cp, len(profiles)


def _render_matrix(
    console: Console,
    title: str,
    row_label: str,
    column_label: str,
    columns: list[str],
    column_legend_title: str,
    matrix: dict[str, dict[str, int]],
    empty_cp: dict[str, int],
    total_profiles: int,
) -> None:
    rows = sorted(matrix.keys())

    # Legend: short column ids -> full names (columns won't fit as headers).
    short_ids = [f"M{i + 1}" for i in range(len(columns))]
    if columns:
        legend = Table(title=column_legend_title, show_header=True, box=None, padding=(0, 1))
        legend.add_column("ID", style="bold cyan", width=5)
        legend.add_column(column_label)
        for short, full in zip(short_ids, columns):
            legend.add_row(short, full)
        console.print(legend)
        console.print()

    # Compact layout: with ~10 machines the default box + cell padding
    # exceeds an 80-column terminal and Rich collapses the row-label column
    # to nothing. No box, no padding, fixed narrow numeric columns.
    table = Table(title=title, show_lines=False, box=None, padding=(0, 0), pad_edge=False)
    table.add_column(row_label, max_width=30, no_wrap=True, overflow="ellipsis")
    for short in short_ids:
        table.add_column(short, justify="right", width=4)
    table.add_column(UNKNOWN_COLUMN, justify="right", width=3, style="magenta")
    table.add_column(ALL_COLUMN, justify="right", width=5, style="yellow")

    empty_total = 0
    for row in rows:
        counts = matrix[row]
        cells = []
        for col in columns:
            n = counts.get(col, 0)
            cells.append("·" if n == 0 else str(n))  # "·"
        unknown_n = counts.get(UNKNOWN_COLUMN, 0)
        cells.append("·" if unknown_n == 0 else str(unknown_n))

        row_empty = empty_cp.get(row, 0)
        empty_total += row_empty
        if row_empty:
            empty_style = "red" if row_empty > 1 else "yellow"
            empty_cell = Text(str(row_empty), style=f"bold {empty_style}")
        else:
            empty_cell = "·"

        table.add_row(row, *cells, empty_cell)

    console.print(table)

    console.print(
        f"[dim]{total_profiles} profile(s), {len(rows)} row(s), "
        f"{empty_total} with empty compatible_printers (visible to ALL printers)[/dim]"
    )


def print_filament_matrix(
    console: Console,
    profiles: dict[ProfileCategory, list[Profile]],
    config: Config = DEFAULT_CONFIG,
) -> None:
    """Print a Material-Brand x Machine coverage matrix for filament profiles."""
    filaments = profiles.get(ProfileCategory.FILAMENT, [])
    machines = sorted({p.name for p in profiles.get(ProfileCategory.MACHINE, [])})

    def column_for_ref(ref: str, columns: list[str]) -> str:
        return ref if ref in columns else UNKNOWN_COLUMN

    def row_key(p: Profile) -> str:
        return _filament_row_key(p, config)

    matrix, empty_cp, total = _build_matrix(filaments, machines, row_key, column_for_ref)

    _render_matrix(
        console,
        title="Filament Coverage Matrix",
        row_label="Material - Brand",
        column_label="Machine",
        columns=machines,
        column_legend_title="Machine Legend",
        matrix=matrix,
        empty_cp=empty_cp,
        total_profiles=total,
    )


def print_process_matrix(
    console: Console,
    profiles: dict[ProfileCategory, list[Profile]],
    config: Config = DEFAULT_CONFIG,
) -> None:
    """Print a LayerHeight-Purpose x Printer-Model coverage matrix for
    process profiles. Columns are deduped printer models rather than full
    machine names, since process profiles are model-scoped."""
    processes = profiles.get(ProfileCategory.PROCESS, [])
    machine_names = {p.name for p in profiles.get(ProfileCategory.MACHINE, [])}
    models = sorted({_machine_model(m) for m in machine_names})
    model_by_machine = {m: _machine_model(m) for m in machine_names}

    def column_for_ref(ref: str, columns: list[str]) -> str:
        model = model_by_machine.get(ref)
        if model is not None and model in columns:
            return model
        return UNKNOWN_COLUMN

    def row_key(p: Profile) -> str:
        return _process_row_key(p, config)

    matrix, empty_cp, total = _build_matrix(processes, models, row_key, column_for_ref)

    _render_matrix(
        console,
        title="Process Coverage Matrix",
        row_label="Layer Height - Purpose",
        column_label="Printer Model",
        columns=models,
        column_legend_title="Printer Model Legend",
        matrix=matrix,
        empty_cp=empty_cp,
        total_profiles=total,
    )

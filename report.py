"""Report from results/runs.csv: tables (Markdown) and charts (PNG) in results/report/.

    uv run report.py                       # reads results/runs.csv, writes results/report/report.md + *.png
    uv run report.py --csv other.csv --out some/dir

Tables
    per database  - rows: CREATE / READ / UPDATE / DELETE / SUM, columns: every size x runs 1..3 + best run
    per size      - rows: CREATE / READ / UPDATE / DELETE / SUM, columns: databases (best run), then all 32 scenarios
    repeatability - per size: total of every run and the spread between runs (table + chart)
    appendix      - per database, all 32 scenarios in every run
Charts
    per database  - best runs of every size compared (log scale)
    per size      - best runs of every database compared (log scale), plus one chart with all 32 scenarios

Prediction vs measurement (last section, only when the predicted CSV exists)
    --predicted results/runs_calibrated.csv is compared with the measured CSV: deviation per scenario, per size,
    per database and per operation, plus two charts. The smallest common size is the calibration set, the rest is the test.

"Best run" = the complete run (all 32 scenarios ok) with the smallest total. If no run of a database and size
is complete, the run with the most successful scenarios is used and marked as incomplete.
"""
import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from zbd.dataset import ROOT  # noqa: E402
from main import DATABASES  # noqa: E402
from zbd.scenarios import SCENARIOS  # noqa: E402

DEFAULT_CSV = ROOT / "results" / "runs.csv"
DEFAULT_OUT = ROOT / "results" / "report"
DEFAULT_PREDICTED = ROOT / "results" / "runs_calibrated.csv"
OPS = ["CREATE", "READ", "UPDATE", "DELETE"]
GROUPS = OPS + ["SUM"]
OPS_COUNT = {op: sum(s.operation == op for s in SCENARIOS) for op in OPS}
NAMES = {"postgres": "PostgreSQL", "mysql": "MySQL", "mongodb": "MongoDB", "redis": "Redis"}

# Colors follow the entity: one fixed categorical slot per database (validated: validate_palette.py, adjacent pairs).
DB_COLORS = {"postgres": "#2a78d6", "mysql": "#eb6834", "mongodb": "#1baf7a", "redis": "#eda100"}
SIZE_COLORS = ["#86b6ef", "#3987e5", "#184f95", "#0d366b"]  # ordinal blue ramp: bigger data = darker
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"


# ---------- loading and aggregation

def load(path):
    """{(database, size, run): {scenario id: row}}"""
    runs = defaultdict(dict)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            row["seconds"] = float(row["seconds"])
            runs[(row["database"], int(row["size"]), int(row["run"]))][row["scenario"]] = row
    return runs


def group_value(records, group):
    """Seconds (float) when every scenario of the group is ok, else the first failing status; None = no data."""
    ids = [s.id for s in SCENARIOS if group == "SUM" or s.operation == group]
    if any(i not in records for i in ids):
        return None
    bad = [records[i]["status"] for i in ids if records[i]["status"] != "ok"]
    if bad:
        return "DNF" if group == "SUM" else bad[0]
    return sum(records[i]["seconds"] for i in ids)


def is_complete(records):
    return len(records) == len(SCENARIOS) and all(r["status"] == "ok" for r in records.values())


def best_run(runs, db, size):
    """(run number, complete?) of the best run, or None when there is no data."""
    candidates = {run: recs for (d, s, run), recs in runs.items() if d == db and s == size}
    if not candidates:
        return None
    complete = {run: recs for run, recs in candidates.items() if is_complete(recs)}
    if complete:
        run = min(complete, key=lambda r: group_value(complete[r], "SUM"))
        return run, True
    def score(run):
        ok = [r for r in candidates[run].values() if r["status"] == "ok"]
        return (len(ok), -sum(r["seconds"] for r in ok))
    return max(candidates, key=score), False


def size_label(size):
    if size >= 1_000_000 and size % 1_000_000 == 0:
        return f"{size // 1_000_000}M"
    if size >= 1000 and size % 1000 == 0:
        return f"{size // 1000}k"
    return f"{size:,}"


def fmt(value):
    if value is None:
        return "–"
    if isinstance(value, str):
        return value
    if value < 10:
        return f"{value:.2f}"
    if value < 1000:
        return f"{value:.1f}"
    return f"{value:,.0f}".replace(",", " ")


def group_label(group):
    return f"SUMA ({len(SCENARIOS)})" if group == "SUM" else f"{group} ({OPS_COUNT[group]})"


def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] + ["---:"] * (len(headers) - 1)) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def bold(text):
    return f"**{text}**"


# ---------- tables

def table_per_database(runs, db, sizes):
    headers = ["Operacja"]
    for size in sizes:
        headers += [f"{size_label(size)} R1", f"{size_label(size)} R2", f"{size_label(size)} R3", f"{size_label(size)} best*"]
    rows = []
    for group in GROUPS:
        row = [bold(group_label(group)) if group == "SUM" else group_label(group)]
        for size in sizes:
            for run in (1, 2, 3):
                records = runs.get((db, size, run))
                row.append(fmt(group_value(records, group)) if records else "–")
            best = best_run(runs, db, size)
            row.append(bold(fmt(group_value(runs[(db, size, best[0])], group))) if best else "–")
        rows.append(row)
    return md_table(headers, rows)


def best_records(runs, db, size):
    best = best_run(runs, db, size)
    return (runs[(db, size, best[0])], best) if best else (None, None)


def table_per_size(runs, size, dbs):
    best = {db: best_records(runs, db, size) for db in dbs}
    headers = ["Operacja"] + [NAMES[db] for db in dbs]
    rows = []
    for group in GROUPS:
        row = [bold(group_label(group)) if group == "SUM" else group_label(group)]
        values = {db: group_value(best[db][0], group) if best[db][0] else None for db in dbs}
        fastest = min((v for v in values.values() if isinstance(v, float)), default=None)
        row += [bold(fmt(v)) if v is not None and v == fastest else fmt(v) for v in values.values()]
        rows.append(row)
    runs_row = ["najlepszy run"]
    for db in dbs:
        info = best[db][1]
        runs_row.append("–" if info is None else f"run {info[0]}" + ("" if info[1] else " (niekompletny)"))
    rows.append(runs_row)
    return md_table(headers, rows)


def table_scenarios(runs, size, dbs):
    best = {db: best_records(runs, db, size)[0] for db in dbs}
    headers = ["#", "Scenariusz", "Op.", "Trudność"] + [NAMES[db] for db in dbs]
    rows = []
    for number, s in enumerate(SCENARIOS, 1):
        cells = {}
        for db in dbs:
            record = best[db].get(s.id) if best[db] else None
            cells[db] = None if record is None else (record["seconds"] if record["status"] == "ok" else record["status"])
        fastest = min((v for v in cells.values() if isinstance(v, float)), default=None)
        rows.append([number, s.name, s.operation, s.difficulty] +
                    [bold(fmt(v)) if v is not None and v == fastest else fmt(v) for v in cells.values()])
    return md_table(headers, rows)


def table_repeatability(runs, size, dbs):
    """Total of all 32 scenarios in every run and the spread between the complete runs: (max - min) / min."""
    run_numbers = sorted({run for (_, sz, run) in runs if sz == size})
    headers = ["Baza"] + [f"run {run}" for run in run_numbers] + ["rozrzut"]
    rows = []
    for db in dbs:
        totals = [group_value(runs[(db, size, run)], "SUM") if (db, size, run) in runs else None for run in run_numbers]
        complete = [t for t in totals if isinstance(t, float)]
        spread = f"{(max(complete) - min(complete)) / min(complete) * 100:.0f}%" if len(complete) >= 2 else "–"
        rows.append([NAMES[db]] + [fmt(t) for t in totals] + [spread])
    return md_table(headers, rows)


def table_appendix(runs, db, sizes):
    headers = ["#", "Scenariusz"]
    columns = []
    for size in sizes:
        for run in (1, 2, 3):
            if (db, size, run) in runs:
                headers.append(f"{size_label(size)} R{run}")
                columns.append(runs[(db, size, run)])
    rows = []
    for number, s in enumerate(SCENARIOS, 1):
        row = [number, s.name]
        for records in columns:
            record = records.get(s.id)
            row.append("–" if record is None else fmt(record["seconds"]) if record["status"] == "ok" else record["status"])
        rows.append(row)
    return md_table(headers, rows)


# ---------- charts

def style(fig, ax, value_axis):
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis=value_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def floor_limit(values):
    positive = [v for v in values if isinstance(v, float) and v > 0]
    low = min(positive) if positive else 1.0
    high = max(positive) if positive else 10.0
    return 10 ** math.floor(math.log10(low / 2)), high * 4


def grouped_bars(path, title, categories, series, horizontal=False, label_group=None):
    """series: [(name, color, [value per category])]; a value is seconds, a status string (drawn as x) or None."""
    all_values = [v for _, _, values in series for v in values]
    low, high = floor_limit(all_values)
    n = len(series)
    width = 0.8 / n
    height = 4.6 if not horizontal else max(5, 0.42 * len(categories) * n / 2 + 2)
    fig, ax = plt.subplots(figsize=(9 if not horizontal else 10, height), dpi=150)
    style(fig, ax, "x" if horizontal else "y")
    for i, (name, color, values) in enumerate(series):
        offset = (i - (n - 1) / 2) * width
        for j, value in enumerate(values):
            pos = j + offset
            if isinstance(value, float):
                if horizontal:
                    ax.barh(pos, value - low, left=low, height=width * 0.86, color=color, edgecolor=SURFACE, linewidth=1)
                else:
                    ax.bar(pos, value - low, bottom=low, width=width * 0.86, color=color, edgecolor=SURFACE, linewidth=1)
                if label_group is not None and categories[j] == label_group:
                    text = fmt(value)
                    if horizontal:
                        ax.text(value * 1.12, pos, text, va="center", fontsize=8, color=INK2)
                    else:
                        ax.text(pos, value * 1.12, text, ha="center", fontsize=8, color=INK2)
            elif isinstance(value, str):  # did not finish: x on the baseline, status next to it
                marker_at = low * 1.8
                if horizontal:
                    ax.plot(marker_at, pos, marker="x", color=MUTED, markersize=5)
                    ax.text(marker_at * 1.35, pos, value, va="center", fontsize=7, color=INK2)
                else:
                    ax.plot(pos, marker_at, marker="x", color=MUTED, markersize=5)
                    ax.text(pos, marker_at * 1.5, value, ha="center", fontsize=7, color=INK2)
    ticks = range(len(categories))
    if horizontal:
        ax.set_xscale("log")
        ax.set_xlim(low, high)
        ax.set_yticks(list(ticks))
        ax.set_yticklabels(categories, fontsize=8, color=INK2)
        ax.set_ylim(len(categories) - 0.5, -0.5)  # first scenario on top, no empty margins
        ax.set_xlabel("sekundy (skala logarytmiczna)", color=INK2, fontsize=9)
    else:
        ax.set_yscale("log")
        ax.set_ylim(low, high)
        ax.set_xticks(list(ticks))
        ax.set_xticklabels(categories, fontsize=10, color=INK2)
        ax.set_ylabel("sekundy (skala logarytmiczna)", color=INK2, fontsize=9)
    ax.set_title(title, color=INK, fontsize=12, loc="left", pad=12)
    if n >= 2:
        fig.legend(handles=[Patch(facecolor=c, label=name) for name, c, _ in series], frameon=False,
                   fontsize=9, labelcolor=INK2, ncol=n, loc="lower center")
    fig.tight_layout(rect=(0, 0.07 if not horizontal else 0.025, 1, 1))
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def chart_per_database(runs, db, sizes, out):
    series = []
    for k, size in enumerate(sizes):
        records, _ = best_records(runs, db, size)
        values = [group_value(records, g) if records else None for g in GROUPS]
        series.append((f"{size:,}".replace(",", " ") + " transakcji", SIZE_COLORS[min(k, len(SIZE_COLORS) - 1)], values))
    name = f"per_db_{db}.png"
    grouped_bars(out / name, f"{NAMES[db]}: najlepsze runy dla różnych rozmiarów danych", GROUPS, series, label_group="SUM")
    return name


def chart_per_size(runs, size, dbs, out):
    series = []
    for db in dbs:
        records, _ = best_records(runs, db, size)
        series.append((NAMES[db], DB_COLORS[db], [group_value(records, g) if records else None for g in GROUPS]))
    name = f"per_size_{size}.png"
    grouped_bars(out / name, f"{size:,}".replace(",", " ") + " transakcji: najlepsze runy baz", GROUPS, series, label_group="SUM")
    return name


def chart_scenarios(runs, size, dbs, out):
    series = []
    labels = [f"{n}. {s.name}" for n, s in enumerate(SCENARIOS, 1)]
    for db in dbs:
        records, _ = best_records(runs, db, size)
        values = []
        for s in SCENARIOS:
            record = records.get(s.id) if records else None
            values.append(None if record is None else (record["seconds"] if record["status"] == "ok" else record["status"]))
        series.append((NAMES[db], DB_COLORS[db], values))
    name = f"scenarios_{size}.png"
    grouped_bars(out / name, f"{size:,}".replace(",", " ") + " transakcji: wszystkie scenariusze (najlepsze runy)", labels, series, horizontal=True)
    return name


def spread_percent(runs, db, size, run_numbers):
    totals = [group_value(runs[(db, size, run)], "SUM") if (db, size, run) in runs else None for run in run_numbers]
    complete = [t for t in totals if isinstance(t, float)]
    spread = (max(complete) - min(complete)) / min(complete) * 100 if len(complete) >= 2 else None
    return totals, spread


def chart_repeatability(runs, size, dbs, out):
    """Left: total of every run per database (equal bars = repeatable). Right: spread between the runs in %."""
    run_numbers = sorted({run for (_, sz, run) in runs if sz == size})
    n = len(run_numbers)
    width = 0.8 / n
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.6), dpi=150, gridspec_kw={"width_ratios": [3, 2]})
    style(fig, ax1, "y")
    style(fig, ax2, "y")
    data = {db: spread_percent(runs, db, size, run_numbers) for db in dbs}
    top = max([t for totals, _ in data.values() for t in totals if isinstance(t, float)], default=1.0)
    for j, db in enumerate(dbs):
        totals, spread = data[db]
        for i, total in enumerate(totals):
            pos = j + (i - (n - 1) / 2) * width
            color = SIZE_COLORS[min(i, len(SIZE_COLORS) - 1)]
            if isinstance(total, float):
                ax1.bar(pos, total, width=width * 0.86, color=color, edgecolor=SURFACE, linewidth=1)
            elif isinstance(total, str):  # run did not finish
                ax1.plot(pos, top * 0.03, marker="x", color=MUTED, markersize=5)
                ax1.text(pos, top * 0.07, total, ha="center", fontsize=7, color=INK2)
        if spread is not None:
            ax2.bar(j, spread, width=0.55, color=DB_COLORS[db], edgecolor=SURFACE, linewidth=1)
            ax2.text(j, spread + 0.15, f"{spread:.1f}%", ha="center", fontsize=9, color=INK2)
    for ax in (ax1, ax2):
        ax.set_xticks(range(len(dbs)))
        ax.set_xticklabels([NAMES[db] for db in dbs], fontsize=9, color=INK2)
    ax1.set_ylim(0, top * 1.1)
    ax1.set_ylabel("sekundy (suma 32 scenariuszy)", color=INK2, fontsize=9)
    ax1.set_title("Suma czasów w każdym runie", color=INK, fontsize=11, loc="left", pad=10)
    ax2.set_ylim(0, max([s for _, s in data.values() if s is not None] + [5]) * 1.25)
    ax2.set_ylabel("%", color=INK2, fontsize=9)
    ax2.set_title("Rozrzut między runami: (max − min) / min", color=INK, fontsize=11, loc="left", pad=10)
    fig.suptitle(f"{size:,}".replace(",", " ") + " transakcji: powtarzalność runów", color=INK, fontsize=12, x=0.01, ha="left")
    fig.legend(handles=[Patch(facecolor=SIZE_COLORS[min(i, len(SIZE_COLORS) - 1)], label=f"run {run}")
                        for i, run in enumerate(run_numbers)], frameon=False, fontsize=9, labelcolor=INK2,
               ncol=n, loc="lower left", bbox_to_anchor=(0.06, 0.0))
    fig.tight_layout(rect=(0, 0.07, 1, 0.94))
    name = f"repeatability_{size}.png"
    fig.savefig(out / name, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return name


# ---------- prediction vs measurement

def load_medians(path):
    """{(database, scenario, size): median seconds of the ok runs}"""
    values = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["status"] == "ok":
                values[(row["database"], row["scenario"], int(row["size"]))].append(float(row["seconds"]))
    return {key: statistics.median(v) for key, v in values.items()}


def comparison_pairs(real_path, predicted_path):
    """One pair per (database, scenario, size) present in both files: measured vs predicted median."""
    real, predicted = load_medians(real_path), load_medians(predicted_path)
    operation = {s.id: s.operation for s in SCENARIOS}
    return [{"db": db, "scenario": sc, "size": size, "op": operation[sc], "real": real[(db, sc, size)],
             "pred": predicted[(db, sc, size)], "err": (predicted[(db, sc, size)] - real[(db, sc, size)]) / real[(db, sc, size)]}
            for (db, sc, size) in sorted(real) if (db, sc, size) in predicted and real[(db, sc, size)] > 0]


def deviation_stats(pairs):
    """Row of the summary tables for a list of pairs (err = (predicted - measured) / measured)."""
    if not pairs:
        return None
    ab = [abs(p["err"]) for p in pairs]
    real, pred = sum(p["real"] for p in pairs), sum(p["pred"] for p in pairs)
    share = lambda limit: sum(a <= limit for a in ab) / len(ab) * 100
    factor2 = sum(0.5 <= p["pred"] / p["real"] <= 2 for p in pairs) / len(pairs) * 100
    return {"n": len(pairs), "median": statistics.median(ab) * 100, "mean": statistics.fmean(ab) * 100,
            "bias": statistics.median(p["err"] for p in pairs) * 100, "total": (pred - real) / real * 100,
            "w10": share(0.10), "w25": share(0.25), "w50": share(0.50), "x2": factor2}


def pct(value, signed=False):
    rounded = round(value)
    return "0%" if rounded == 0 else f"{rounded:+d}%" if signed else f"{rounded}%"


def deviation_table(first_header, groups):
    """groups: [(label, pairs)]"""
    headers = [first_header, "Par", "Mediana odchylenia", "Średnie odchylenie", "Przesunięcie (mediana ze znakiem)",
               "Odchylenie sumy czasów", "w ±10%", "w ±25%", "w ±50%", "w granicach ×2"]
    rows = []
    for label, pairs in groups:
        st = deviation_stats(pairs)
        if st:
            rows.append([label, st["n"], pct(st["median"]), pct(st["mean"]), pct(st["bias"], True), pct(st["total"], True),
                         pct(st["w10"]), pct(st["w25"]), pct(st["w50"]), pct(st["x2"])])
    return md_table(headers, rows)


def chart_prediction_scatter(pairs, out):
    """Predicted vs measured seconds (log-log); on the diagonal = perfect prediction, band = within a factor of 2."""
    fig, ax = plt.subplots(figsize=(6.4, 6.0), dpi=150)
    style(fig, ax, "both")
    values = [p[k] for p in pairs for k in ("real", "pred")]
    low, high = min(values) / 2, max(values) * 2
    ax.fill_between([low, high], [low / 2, high / 2], [low * 2, high * 2], color=GRID, alpha=0.55, linewidth=0)
    ax.plot([low, high], [low, high], color=AXIS, linewidth=1)
    for db in DB_COLORS:
        mine = [p for p in pairs if p["db"] == db]
        ax.scatter([p["real"] for p in mine], [p["pred"] for p in mine], s=14, color=DB_COLORS[db], alpha=0.75,
                   edgecolors=SURFACE, linewidths=0.4)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(low, high)
    ax.set_ylim(low, high)
    ax.set_xlabel("pomiar (sekundy)", color=INK2, fontsize=9)
    ax.set_ylabel("predykcja AI (sekundy)", color=INK2, fontsize=9)
    ax.set_title("Predykcja vs pomiar (szare pasmo: do dwukrotnej różnicy)", color=INK, fontsize=11, loc="left", pad=10)
    fig.legend(handles=[Patch(facecolor=c, label=NAMES[db]) for db, c in DB_COLORS.items() if any(p["db"] == db for p in pairs)],
               frameon=False, fontsize=9, labelcolor=INK2, ncol=4, loc="lower center")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    name = "prediction_scatter.png"
    fig.savefig(out / name, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return name


def chart_prediction_by_operation(pairs, sizes, out):
    """Median absolute deviation per operation, one bar per size."""
    fig, ax = plt.subplots(figsize=(8.5, 4.4), dpi=150)
    style(fig, ax, "y")
    n = len(sizes)
    width = 0.8 / n
    top = 1.0
    for i, size in enumerate(sizes):
        color = SIZE_COLORS[min(i, len(SIZE_COLORS) - 1)]
        for j, op in enumerate(OPS):
            mine = [abs(p["err"]) * 100 for p in pairs if p["size"] == size and p["op"] == op]
            if not mine:
                continue
            value = statistics.median(mine)
            top = max(top, value)
            pos = j + (i - (n - 1) / 2) * width
            ax.bar(pos, value, width=width * 0.86, color=color, edgecolor=SURFACE, linewidth=1)
            ax.text(pos, value + top * 0.015, f"{value:.0f}%", ha="center", fontsize=8, color=INK2)
    ax.set_xticks(range(len(OPS)))
    ax.set_xticklabels(OPS, fontsize=10, color=INK2)
    ax.set_ylim(0, top * 1.15)
    ax.set_ylabel("mediana odchylenia (%)", color=INK2, fontsize=9)
    ax.set_title("Odchylenie predykcji od pomiaru wg operacji", color=INK, fontsize=12, loc="left", pad=12)
    fig.legend(handles=[Patch(facecolor=SIZE_COLORS[min(i, len(SIZE_COLORS) - 1)], label=f"{size_label(size)} transakcji")
                        for i, size in enumerate(sizes)], frameon=False, fontsize=9, labelcolor=INK2, ncol=n, loc="lower center")
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    name = "prediction_by_operation.png"
    fig.savefig(out / name, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return name


def prediction_section(real_path, predicted_path, out):
    """Markdown lines of the closing section, or [] when there is nothing to compare."""
    pairs = comparison_pairs(real_path, predicted_path)
    if not pairs:
        return []
    sizes = sorted({p["size"] for p in pairs})
    calibration, tests = sizes[0], sizes[1:]
    test_pairs = [p for p in pairs if p["size"] in tests]
    dbs = [db for db in DATABASES if any(p["db"] == db for p in pairs)]
    overall = deviation_stats(pairs)
    md = ["## Hipoteza badawcza", "",
          "> **Czy modele AI, dostając wyniki wydajności dla 100 000 rekordów, potrafią dobrze oszacować, "
          "jak bazy zachowają się przy dużo większym obciążeniu?**", "",
          "Powyższe porównania baz to prawdziwe pomiary. Badamy, czy model AI może je zastąpić albo przynajmniej "
          "przewidzieć: dostaje wyniki z małego zbioru i ma zgadnąć, ile czasu zajmą te same 32 scenariusze na 1 mln "
          "i 10 mln transakcji. Hipoteza zakłada, że model dobrze trafi w rzędy wielkości i ranking baz, ale pogubi się "
          "tam, gdzie wydajność zależy od stanu danych, cache i planów zapytań, a nie tylko od ich ilości. "
          "Czy tak wyszło, pokazuje porównanie poniżej.", ""]
    md += ["## Predykcja AI a pomiar", "",
          f"Porównanie `{predicted_path.name}` (wartości przewidziane przez model AI) z `{real_path.name}` (prawdziwe pomiary). "
          "Dla każdej pary baza × scenariusz × rozmiar bierzemy medianę czasów z runów i liczymy odchylenie "
          "(przewidziane − zmierzone) / zmierzone. „Mediana odchylenia” i „średnie odchylenie” to wartości bezwzględne; "
          "„przesunięcie” ze znakiem pokazuje, czy model raczej zawyża (+) czy zaniża (−); „odchylenie sumy czasów” to różnica "
          "łącznych czasów wszystkich porównanych scenariuszy.", ""]
    if tests:
        md += [f"Rozmiar {size_label(calibration)} posłużył do kalibracji predykcji, więc jego wynik nie jest testem. "
               f"Prawdziwym testem są większe rozmiary ({', '.join(size_label(s) for s in tests)}), "
               "na których model nie widział prawdziwych pomiarów.", ""]
    md += [f"**Ogółem:** {overall['n']} porównanych pomiarów, średnie odchylenie {pct(overall['mean'])}, "
           f"mediana {pct(overall['median'])}, odchylenie łącznego czasu {pct(overall['total'], True)}; "
           f"{pct(overall['w25'])} pomiarów mieści się w ±25%, {pct(overall['x2'])} w granicach dwukrotnej różnicy.", ""]
    if tests:
        t = deviation_stats(test_pairs)
        md += [f"**Tylko test ({', '.join(size_label(s) for s in tests)}):** {t['n']} pomiarów, średnie odchylenie "
               f"{pct(t['mean'])}, mediana {pct(t['median'])}, odchylenie łącznego czasu {pct(t['total'], True)}; "
               f"{pct(t['w25'])} w ±25%, {pct(t['x2'])} w granicach dwukrotnej różnicy.", ""]

    groups = [("Wszystkie rozmiary", pairs)]
    if tests:
        groups.append(("Tylko test", test_pairs))
    groups += [(f"{size_label(s)}" + (" (kalibracja)" if s == calibration and tests else ""),
                [p for p in pairs if p["size"] == s]) for s in sizes]
    md += ["### Podsumowanie wg rozmiaru", "", deviation_table("Zbiór", groups), ""]

    basis = test_pairs if tests else pairs
    basis_label = "tylko test" if tests else "wszystkie rozmiary"
    md += [f"### Wg bazy ({basis_label})", "",
           deviation_table("Baza", [(NAMES[db], [p for p in basis if p["db"] == db]) for db in dbs]), ""]
    md += [f"### Wg operacji ({basis_label})", "",
           deviation_table("Operacja", [(op, [p for p in basis if p["op"] == op]) for op in OPS]), ""]
    md += [f"![predykcja wg operacji]({chart_prediction_by_operation(basis, tests or sizes, out)})", ""]

    names = {s.id: s.name for s in SCENARIOS}
    worst = sorted(basis, key=lambda p: -abs(math.log(p["pred"] / p["real"])))[:10]
    md += [f"### Największe rozbieżności ({basis_label})", "",
           md_table(["Scenariusz", "Baza", "Rozmiar", "Pomiar [s]", "Predykcja [s]", "Odchylenie"],
                    [[f"{p['scenario']} {names[p['scenario']]}", NAMES[p["db"]], size_label(p["size"]), fmt(p["real"]),
                      fmt(p["pred"]), pct(p["err"] * 100, True)] for p in worst]), "",
           "Typowe źródła dużych odchyleń: inna liczba wierszy przetwarzanych przez scenariusz, stan danych po wcześniejszych "
           "modyfikacjach (rozrost tabel, martwe wiersze), cache i plany zapytań przy większych danych oraz szum "
           "przy czasach rzędu milisekund.", "",
           f"![predykcja vs pomiar]({chart_prediction_scatter(basis, out)})", ""]
    return md


# ---------- report

def build(csv_path, out, predicted_path=None):
    runs = load(csv_path)
    if not runs:
        raise SystemExit(f"{csv_path} has no results")
    sizes = sorted({size for _, size, _ in runs})
    dbs = [db for db in DATABASES if any(d == db for d, _, _ in runs)]
    out.mkdir(parents=True, exist_ok=True)

    md = ["# Wyniki benchmarku", "",
          f"Źródło: `{csv_path.name}`. Czasy w sekundach. R1–R3 to kolejne runy, `best*` to najlepszy run "
          "(kompletny run z najmniejszą sumą czasów wszystkich 32 scenariuszy; jeśli żaden run nie był kompletny, "
          "run z największą liczbą udanych scenariuszy). Tekst zamiast liczby oznacza, że scenariusz się nie udał: "
          "`timeout`, `oom`, `error` albo `skipped` (pominięty, bo wcześniejsza modyfikacja danych się nie udała); "
          "`DNF` w wierszu SUMA oznacza, że run nie został ukończony. Na wykresach krzyżyk (×) przy osi oznacza scenariusz, który się nie udał, z jego statusem. Skala wykresów jest logarytmiczna. Opis scenariuszy: [SCENARIOS.md](../../SCENARIOS.md).", ""]

    md += ["## Tabele per baza", ""]
    for db in dbs:
        md += [f"### {NAMES[db]}", "", table_per_database(runs, db, sizes), ""]
        md += [f"![{NAMES[db]}]({chart_per_database(runs, db, sizes, out)})", ""]

    md += ["## Porównanie baz (najlepsze runy)", ""]
    for size in sizes:
        label = f"{size:,}".replace(",", " ")
        md += [f"### {label} transakcji", "", table_per_size(runs, size, dbs), "",
               f"![{label}]({chart_per_size(runs, size, dbs, out)})", "",
               "<details><summary>Wszystkie 32 scenariusze</summary>", "",
               table_scenarios(runs, size, dbs), "",
               f"![scenariusze {label}]({chart_scenarios(runs, size, dbs, out)})", "", "</details>", ""]

    md += ["## Powtarzalność runów", "",
           "Suma czasów wszystkich 32 scenariuszy w każdym runie (sekundy) i rozrzut między kompletnymi runami: "
           "(największy − najmniejszy) / najmniejszy. Mały rozrzut znaczy, że różnice między bazami są większe niż "
           "szum pomiaru.", ""]
    for size in sizes:
        md += [f"### {size:,}".replace(",", " ") + " transakcji", "", table_repeatability(runs, size, dbs), "",
               f"![powtarzalność {size_label(size)}]({chart_repeatability(runs, size, dbs, out)})", ""]

    md += ["## Załącznik: wszystkie scenariusze w każdym runie", ""]
    for db in dbs:
        md += [f"<details><summary>{NAMES[db]}</summary>", "", table_appendix(runs, db, sizes), "", "</details>", ""]

    if predicted_path is not None and predicted_path.exists():
        md += prediction_section(csv_path, predicted_path, out)

    path = out / "report.md"
    path.write_text("\n".join(md))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--predicted", type=Path, default=DEFAULT_PREDICTED,
                        help="predicted values (same columns as runs.csv); skipped when the file does not exist")
    args = parser.parse_args()
    print(f"wrote {build(args.csv, args.out, args.predicted)}")


if __name__ == "__main__":
    main()

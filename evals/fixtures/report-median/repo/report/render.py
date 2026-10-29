"""Format the summary table."""

from report.stats import mean, median


def render(columns: dict[str, list[float]]) -> str:
    lines = ["column  count  mean  median"]
    for name, values in columns.items():
        lines.append(f"{name}  {len(values)}  {mean(values):g}  {median(values):g}")
    return "\n".join(lines)

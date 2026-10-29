import sys

from report.reader import read_columns
from report.render import render


def main(argv: list[str]) -> int:
    with open(argv[0], encoding="utf-8") as f:
        print(render(read_columns(f)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""wc FILE: print `lines words chars FILE`."""

import argparse

from wc.count import count


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wc")
    parser.add_argument("file")
    args = parser.parse_args(argv)
    with open(args.file, encoding="utf-8") as f:
        c = count(f.read())
    print(f"{c.lines} {c.words} {c.chars} {args.file}")
    return 0

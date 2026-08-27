"""``python -m portable_core.lint {no-float,gips-language,all}``."""

from __future__ import annotations

import sys

from portable_core.lint import gips_language, no_float

_USAGE = "usage: python -m portable_core.lint {no-float,gips-language,all} [ROOT]"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2

    which, rest = args[0], args[1:]
    if which == "no-float":
        return no_float.main(rest)
    if which == "gips-language":
        return gips_language.main(rest)
    if which == "all":
        return max(no_float.main(rest), gips_language.main(rest))

    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

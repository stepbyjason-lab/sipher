r"""
sipher-web CLI.

  python -m adapters.web.cli fetch <URL> [--js auto|true|false] [--timeout N]

fetch : 임의 http(s) 아티클 URL → 정규화 JSON(stdout)

옵션:
  --js MODE      auto(기본, SSR 껍데기 의심 시 자동 JS 렌더 승격) |
                 true(항상 JS 렌더 강제) | false(정적 tier1만, 승격 안 함)
  --timeout N    tier1 요청 타임아웃 초(기본 25)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import fetch

_log = logging.getLogger(__name__)


def _parse_js(value: str) -> bool | str:
    v = value.strip().lower()
    if v == "auto":
        return "auto"
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"--js는 auto/true/false 중 하나여야 합니다: {value!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-web")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="임의 http(s) 아티클 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--js", type=_parse_js, default="auto",
                     help="auto(기본)/true(강제 렌더)/false(정적만)")
    pf.add_argument("--timeout", type=int, default=25, help="tier1 타임아웃 초(기본 25)")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        result = fetch(args.url, js=args.js, timeout=args.timeout)
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"오류[{type(e).__name__}]: {e}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

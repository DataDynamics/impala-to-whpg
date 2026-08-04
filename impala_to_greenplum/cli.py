"""커맨드라인 진입점.

사용 예:
    python -m impala_to_greenplum --config conf/config.yaml
    python -m impala_to_greenplum --config conf/config.yaml --job orders --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from .config import ConfigError, load_config
from .pipeline import run_load


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="impala-to-greenplum",
        description="Impala에서 SELECT한 결과를 Greenplum에 적재합니다.",
    )
    parser.add_argument("-c", "--config", required=True, help="YAML 설정 파일 경로")
    parser.add_argument(
        "-j",
        "--job",
        action="append",
        dest="jobs",
        metavar="TARGET_TABLE",
        help="실행할 작업의 target_table (여러 번 지정 가능, 미지정 시 전체 실행)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="디버그 로그 출력")
    parser.add_argument(
        "--log-file", help="로그를 파일에도 기록할 경로", default=None
    )
    return parser


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=handlers,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    logger = logging.getLogger("impala_to_greenplum")

    try:
        config = load_config(args.config)
        results = run_load(config, only=args.jobs)
    except ConfigError as exc:
        logger.error("설정 오류: %s", exc)
        return 2
    except Exception as exc:  # 적재 실패는 이미 롤백된 상태
        logger.error("적재 실패: %s", exc)
        if args.verbose:
            logger.exception("상세 오류")
        return 1

    total = sum(r.rows_read for r in results)
    print("\n=== 적재 결과 ===")
    for result in results:
        print(f"  - {result.summary()}")
    print(f"총 {len(results)}개 작업, {total:,}건 처리 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

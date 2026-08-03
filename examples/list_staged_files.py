"""S3에 올라간 스테이징 파일 목록을 확인한다.

    python examples/list_staged_files.py --config config.yaml
    python examples/list_staged_files.py --config config.yaml --group
    python examples/list_staged_files.py --config config.yaml --stale 24

설정 파일의 s3 섹션(버킷, 자격증명, 엔드포인트)을 그대로 재사용한다.
자세한 설명은 docs/boto3.md 참고.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from impala_to_greenplum import load_config
from impala_to_greenplum.s3_stage import S3Stager


def list_objects(client: Any, bucket: str, prefix: str) -> List[Dict[str, Any]]:
    """접두사 아래 모든 오브젝트를 돌려준다.

    list_objects_v2는 한 번에 1000개까지만 주므로 페이지네이터로 끝까지 훑는다.
    """
    paginator = client.get_paginator("list_objects_v2")
    return [
        obj
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])  # 결과가 비면 Contents 키가 없다
    ]


def list_runs(client: Any, bucket: str, prefix: str) -> List[str]:
    """실행 단위 디렉터리(CommonPrefixes)만 한 단계 나열한다."""
    paginator = client.get_paginator("list_objects_v2")
    return [
        entry["Prefix"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
        for entry in page.get("CommonPrefixes", [])
    ]


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:,.1f}{unit}"
        size /= 1024
    return f"{size:,.1f}GB"


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="S3 스테이징 파일 목록 확인")
    parser.add_argument("-c", "--config", required=True, help="YAML 설정 파일 경로")
    parser.add_argument("--group", action="store_true", help="실행 단위로 묶어서 표시")
    parser.add_argument(
        "--stale",
        type=int,
        metavar="HOURS",
        help="지정한 시간보다 오래된 파일만 표시 (정리 대상 확인용)",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if config.s3 is None:
        print("설정 파일에 s3 섹션이 없습니다.", file=sys.stderr)
        return 2

    stager = S3Stager(config.s3)
    bucket = config.s3.bucket
    prefix = config.s3.prefix.strip("/")
    prefix = f"{prefix}/" if prefix else ""

    if args.group:
        runs = list_runs(stager.client, bucket, prefix)
        for run in runs:
            objects = list_objects(stager.client, bucket, run)
            total = sum(o["Size"] for o in objects)
            print(f"{run}\t{len(objects)}개\t{human(total)}")
        print(f"\n실행 {len(runs)}건")
        return 0

    objects = list_objects(stager.client, bucket, prefix)

    if args.stale is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.stale)
        # LastModified는 tz-aware이므로 비교 대상도 UTC를 붙여야 한다
        objects = [o for o in objects if o["LastModified"] < cutoff]

    if not objects:
        print(f"s3://{bucket}/{prefix} 아래에 표시할 파일이 없습니다.")
        return 0

    for obj in sorted(objects, key=lambda o: o["Key"]):
        print(f"{obj['LastModified']:%Y-%m-%d %H:%M}  {human(obj['Size']):>10}  {obj['Key']}")

    sizes = [o["Size"] for o in objects]
    total = sum(sizes)
    print(f"\n파일 {len(sizes)}개, 합계 {human(total)}")
    print(
        f"최소 {human(min(sizes))} / 평균 {human(total / len(sizes))} / 최대 {human(max(sizes))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

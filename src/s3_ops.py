"""S3 파일·디렉터리 조작 (업로드, 다운로드, 삭제, 목록, 내용 확인).

    bin/s3-ops ls       s3://dw-stage/orders/
    bin/s3-ops ls       s3://dw-stage/orders/ --summary
    bin/s3-ops upload   orders.csv s3://dw-stage/orders/
    bin/s3-ops download s3://dw-stage/orders/2026-08-01/ ./out/ --recursive
    bin/s3-ops head     s3://dw-stage/orders/2026-08-01/part-0.csv.gz
    bin/s3-ops rmdir    s3://dw-stage/orders/ --older-than 7d --yes

S3에는 디렉터리가 없다. 키가 ``a/b/c.csv`` 인 오브젝트가 있을 뿐이고, 콘솔이
슬래시를 보고 폴더처럼 보여줄 뿐이다. 그래서 이 스크립트에서는

- ``mkdir`` 은 ``a/b/`` 라는 빈 오브젝트를 하나 만든다(폴더 표시용 관례).
  파일을 올릴 때 상위 "디렉터리"를 미리 만들 필요는 없다.
- ``rmdir`` 은 그 접두사로 시작하는 오브젝트를 **전부** 지운다.

삭제는 되돌릴 수 없으므로 ``--yes`` 없이는 지울 목록을 보여주고 물어본다.

버킷과 자격증명은 conf/config.yaml 의 s3 섹션에서 자동으로 읽는다. 명령행으로 준
값이 항상 우선하고, 둘 다 없으면 환경변수나 IAM 역할을 따른다. 다른 파일을 쓰려면
``--config`` 를, 아예 읽지 않으려면 ``--no-config`` 를 준다.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import appconfig
import progress
from progress import PhaseTimer

#: delete_objects 는 요청당 1000개까지만 받는다
DELETE_BATCH = 1000

#: 설정의 s3 섹션에서 읽어 쓰는 키. 나머지 키는 무시한다.
S3_SETTINGS = (
    "bucket",
    "region",
    "access_key_id",
    "secret_access_key",
    "session_token",
    "client_endpoint_url",
)


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """``s3://bucket/key`` 를 (버킷, 키)로 나눈다."""
    if not uri.startswith("s3://"):
        raise SystemExit(f"S3 경로는 s3://버킷/키 형식이어야 합니다: {uri!r}")
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket:
        raise SystemExit(f"버킷 이름이 없습니다: {uri!r}")
    return bucket, key


def as_directory(key: str) -> str:
    """디렉터리로 쓸 접두사는 슬래시로 끝나게 맞춘다."""
    return key if key.endswith("/") else key + "/"


#: 보고서에 표시할 구간 순서 (실제 실행 흐름대로)
PHASES = ("설정·클라이언트 준비", "명령 실행")

human = progress.human_bytes


def resolve_target(uri: str, default_bucket: Optional[str]) -> Tuple[str, str]:
    """경로를 (버킷, 키)로 푼다.

    ``s3://버킷/키`` 형태면 그대로 쓰고, 버킷 없이 키만 준 경우에는
    ``--bucket`` 이나 설정 파일의 버킷을 쓴다.
    """
    if uri.startswith("s3://"):
        return parse_s3_uri(uri)
    if not default_bucket:
        raise SystemExit(
            f"버킷을 알 수 없습니다: {uri!r}\n"
            "  s3://버킷/키 형식으로 주거나 --bucket 을 지정하세요."
        )
    return default_bucket, uri.lstrip("/")


def read_s3_settings(path: str, required: bool = True) -> Dict[str, Optional[str]]:
    """YAML 설정 파일의 s3 섹션에서 접속에 필요한 값만 읽는다.

    이 스크립트와 무관한 키는 건너뛴다. 그래서 다른 스크립트와 같은 설정 파일을
    나눠 쓸 수 있다.
    """
    return appconfig.load_section(Path(path), "s3", S3_SETTINGS, required=required)


def make_client(args: argparse.Namespace) -> Tuple[Any, Optional[str]]:
    """boto3 S3 클라이언트와 기본 버킷을 만든다.

    자격증명 우선순위는 명령행 > 설정 파일 > 환경변수/IAM 역할 순이다.
    ``--access-key`` 를 주지 않으면 boto3 기본 자격증명 체인이 그대로 동작한다.
    """
    path = appconfig.resolve_config_path(args)
    # --config 로 직접 지정한 파일에 s3 섹션이 없으면 오타일 가능성이 높다.
    # 기본 파일이라면 다른 스크립트용 설정만 들어 있을 수 있으니 넘어간다.
    settings = appconfig.load_section(
        path, "s3", S3_SETTINGS, required=bool(getattr(args, "config", None))
    )

    # 명령행으로 준 값이 설정 파일보다 우선한다
    for key, given in (
        ("access_key_id", args.access_key),
        ("secret_access_key", args.secret_key),
        ("session_token", args.session_token),
        ("region", args.region),
        ("client_endpoint_url", args.endpoint),
    ):
        if given:
            settings[key] = given

    import boto3

    session = boto3.session.Session(
        aws_access_key_id=settings["access_key_id"],
        aws_secret_access_key=settings["secret_access_key"],
        aws_session_token=settings["session_token"],
        region_name=settings["region"],
    )
    client = session.client("s3", endpoint_url=settings["client_endpoint_url"] or None)
    return client, args.bucket or settings["bucket"]


def list_objects(client: Any, bucket: str, prefix: str) -> List[Dict[str, Any]]:
    """접두사 아래 모든 오브젝트를 돌려준다.

    list_objects_v2 는 한 번에 1000개까지만 주므로 페이지네이터로 끝까지 훑는다.
    """
    paginator = client.get_paginator("list_objects_v2")
    return [
        obj
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])  # 결과가 비면 Contents 키가 없다
    ]


def delete_keys(
    client: Any,
    bucket: str,
    keys: Sequence[str],
    reporter: Optional[progress.Progress] = None,
) -> int:
    """키 목록을 1000개씩 묶어 지우고 실제로 지운 개수를 돌려준다."""
    deleted = 0
    processed = 0
    for start in range(0, len(keys), DELETE_BATCH):
        batch = keys[start : start + DELETE_BATCH]
        response = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        errors = (response or {}).get("Errors") or []
        for error in errors:
            print(
                f"  삭제 실패: {error.get('Key')} ({error.get('Message')})",
                file=sys.stderr,
            )
        deleted += len(batch) - len(errors)
        processed += len(batch)
        if reporter is not None:
            reporter.update(processed)
    if reporter is not None:
        reporter.finish()
    return deleted


def confirm(question: str, assume_yes: bool) -> bool:
    """되돌릴 수 없는 작업 전에 확인을 받는다."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("확인을 받을 수 없습니다. 실행하려면 --yes 를 주세요.", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# -- 명령 -------------------------------------------------------------------------


#: --older-than 이 받는 단위
DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> float:
    """``24h`` / ``7d`` / ``90m`` 을 초로 바꾼다. 단위가 없으면 시간으로 본다."""
    value = text.strip().lower()
    unit = DURATION_UNITS.get(value[-1:], None)
    number = value[:-1] if unit else value
    try:
        amount = float(number)
    except ValueError:
        raise SystemExit(
            f"기간 형식이 잘못되었습니다: {text!r}\n"
            "  90m(분), 24h(시간), 7d(일), 2w(주) 처럼 주세요. 단위를 빼면 시간입니다."
        )
    return amount * (unit or 3600)


def older_than(objects: Iterable[Dict[str, Any]], seconds: float) -> List[Dict[str, Any]]:
    """지정한 기간보다 오래된 오브젝트만 고른다.

    LastModified 는 UTC 기준 timezone-aware datetime 이라 비교 대상도 tz 를 붙여야
    한다. naive datetime 과 비교하면 TypeError 가 난다.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return [o for o in objects if o["LastModified"] < cutoff]


def list_directories(client: Any, bucket: str, prefix: str) -> List[str]:
    """접두사 바로 아래 "디렉터리" 목록만 돌려준다.

    Delimiter 를 주면 그 아래는 접어서 CommonPrefixes 로 준다. 실행 단위가 몇 개
    남아 있는지 훑을 때 파일을 전부 나열하지 않아도 된다.
    """
    paginator = client.get_paginator("list_objects_v2")
    prefixes: List[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        prefixes.extend(entry["Prefix"] for entry in page.get("CommonPrefixes", []))
    return sorted(prefixes)


def print_summary(objects: Sequence[Dict[str, Any]]) -> None:
    """개수와 크기 분포. 파일이 고르게 나뉘었는지 보는 용도다.

    외부 테이블로 읽을 파일이라면 개수가 세그먼트 수보다 많아야 모든 세그먼트가
    일한다. docs/s3_external_table.md 참고.
    """
    sizes = [o["Size"] for o in objects]
    total = sum(sizes)
    print(f"파일 {len(sizes)}개, 합계 {human(total)}")
    print(
        f"최소 {human(min(sizes))} / 평균 {human(total / len(sizes))} / "
        f"최대 {human(max(sizes))}"
    )


def cmd_ls(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    bucket, key = resolve_target(args.uri, bucket)

    if getattr(args, "dirs", False):
        prefixes = list_directories(client, bucket, as_directory(key) if key else "")
        if not prefixes:
            print(f"s3://{bucket}/{key} 아래에 디렉터리가 없습니다.")
            return 0
        for prefix in prefixes:
            print(prefix)
        print(f"\n{len(prefixes)}개")
        return 0

    objects = list_objects(client, bucket, key)
    if getattr(args, "older_than", None):
        objects = older_than(objects, parse_duration(args.older_than))
    if not objects:
        print(f"s3://{bucket}/{key} 아래에 오브젝트가 없습니다.")
        return 0

    if getattr(args, "summary", False):
        print_summary(objects)
        return 0

    for obj in sorted(objects, key=lambda o: o["Key"]):
        marker = "  <디렉터리 표시>" if obj["Key"].endswith("/") else ""
        print(f"{obj['LastModified']:%Y-%m-%d %H:%M}  {human(obj['Size']):>10}  {obj['Key']}{marker}")
    total = sum(o["Size"] for o in objects)
    print(f"\n{len(objects)}개, 합계 {human(total)}")
    return 0


def iter_upload_pairs(source: str, key: str, recursive: bool) -> List[Tuple[str, str]]:
    """(로컬 경로, S3 키) 쌍을 만든다."""
    path = Path(source)
    if path.is_dir():
        if not recursive:
            raise SystemExit(f"{source} 는 디렉터리입니다. --recursive 를 주세요.")
        prefix = as_directory(key) if key else ""
        pairs = []
        for local in sorted(p for p in path.rglob("*") if p.is_file()):
            # 원본 디렉터리 구조를 그대로 유지한다
            relative = local.relative_to(path).as_posix()
            pairs.append((str(local), prefix + relative))
        return pairs

    if not path.is_file():
        raise SystemExit(f"파일을 찾을 수 없습니다: {source}")
    # 대상이 디렉터리로 끝나면 파일 이름을 붙여준다
    return [(str(path), key + path.name if key.endswith("/") or not key else key)]


def cmd_upload(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    bucket, key = resolve_target(args.uri, bucket)
    pairs = iter_upload_pairs(args.source, key, args.recursive)
    if not pairs:
        print(f"올릴 파일이 없습니다: {args.source}")
        return 0

    extra: Dict[str, str] = {}
    if args.sse:
        extra["ServerSideEncryption"] = args.sse

    total_bytes = 0
    # 파일이 여러 개일 때만 진행 상황을 낸다. 하나뿐이면 결과 줄로 충분하다.
    reporter = progress.Progress(
        "업로드",
        total=len(pairs),
        unit="개",
        enabled=len(pairs) > 1 and not getattr(args, "no_progress", False),
    )
    for done, (local, target) in enumerate(pairs, 1):
        size = os.path.getsize(local)
        if args.dry_run:
            print(f"[예행] {local} → s3://{bucket}/{target} ({human(size)})")
            continue
        # upload_file 은 큰 파일을 알아서 멀티파트로 나눠 올린다
        client.upload_file(local, bucket, target, ExtraArgs=extra or None)
        total_bytes += size
        reporter.update(done)
        print(f"{local} → s3://{bucket}/{target} ({human(size)})")
    reporter.finish()

    if not args.dry_run:
        print(f"\n{len(pairs)}개 업로드, 합계 {human(total_bytes)}")
    return 0


def iter_download_pairs(
    client: Any, bucket: str, key: str, destination: str, recursive: bool
) -> List[Tuple[str, str]]:
    """(S3 키, 로컬 경로) 쌍을 만든다.

    접두사를 받으면 그 아래 구조를 로컬에 그대로 편다. 파일 하나를 받을 때 대상이
    디렉터리면 원래 이름을 그대로 쓴다.
    """
    if recursive or key.endswith("/"):
        prefix = as_directory(key) if key else ""
        objects = list_objects(client, bucket, prefix)
        pairs = []
        for obj in sorted(objects, key=lambda o: o["Key"]):
            if obj["Key"].endswith("/"):
                continue  # 디렉터리 표시용 빈 오브젝트는 건너뛴다
            relative = obj["Key"][len(prefix):]
            pairs.append((obj["Key"], str(Path(destination) / relative)))
        return pairs

    target = Path(destination)
    if target.is_dir() or destination.endswith(os.sep):
        target = target / key.rsplit("/", 1)[-1]
    return [(key, str(target))]


def cmd_download(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    bucket, key = resolve_target(args.uri, bucket)
    pairs = iter_download_pairs(client, bucket, key, args.destination, args.recursive)
    if not pairs:
        print(f"받을 파일이 없습니다: s3://{bucket}/{key}")
        return 0

    reporter = progress.Progress(
        "다운로드",
        total=len(pairs),
        unit="개",
        enabled=len(pairs) > 1 and not getattr(args, "no_progress", False),
    )
    downloaded = skipped = 0
    for done, (source_key, local) in enumerate(pairs, 1):
        if args.dry_run:
            print(f"[예행] s3://{bucket}/{source_key} → {local}")
            continue
        # 로컬 파일을 조용히 덮어쓰지 않는다. 되돌릴 수 없는 건 S3 쪽만이 아니다.
        if os.path.exists(local) and not args.force:
            print(f"이미 있어 건너뜁니다: {local}  (덮어쓰려면 --force)", file=sys.stderr)
            skipped += 1
            continue
        parent = os.path.dirname(local)
        if parent:
            os.makedirs(parent, exist_ok=True)
        client.download_file(bucket, source_key, local)
        downloaded += 1
        reporter.update(done)
        print(f"s3://{bucket}/{source_key} → {local} ({human(os.path.getsize(local))})")
    reporter.finish()

    if not args.dry_run:
        note = f"\n{downloaded}개 다운로드"
        if skipped:
            note += f", {skipped}개 건너뜀"
        print(note)
    return 0


def read_prefix_bytes(client: Any, bucket: str, key: str, max_bytes: int) -> bytes:
    """오브젝트 앞부분만 Range 로 받는다. 큰 파일도 전부 받지 않는다."""
    response = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")
    return response["Body"].read()


def decompress_if_gzip(data: bytes) -> bytes:
    """gzip 이면 푼다. 앞부분만 받았으므로 끝이 잘려 있는 것이 정상이다."""
    if not data.startswith(b"\x1f\x8b"):
        return data
    import zlib

    # gzip 헤더를 이해하는 디코더. 잘린 스트림이라 마지막 블록에서 예외가 나는데,
    # 그때까지 푼 만큼은 쓸 수 있으므로 무시한다.
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        return decoder.decompress(data)
    except zlib.error:
        return decoder.flush() or b""


def cmd_head(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    """오브젝트 앞부분을 보여준다. 올린 파일이 제대로 됐는지 확인할 때 쓴다."""
    bucket, key = resolve_target(args.uri, bucket)
    if not key or key.endswith("/"):
        raise SystemExit(f"파일 하나를 지정하세요: {args.uri}")

    from botocore.exceptions import ClientError

    try:
        raw = read_prefix_bytes(client, bucket, key, args.max_bytes)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            print(f"없는 파일입니다: s3://{bucket}/{key}", file=sys.stderr)
            return 1
        raise

    data = raw if args.raw else decompress_if_gzip(raw)
    # 잘린 멀티바이트 문자가 끝에 걸릴 수 있으므로 replace 로 흘려보낸다
    text = data.decode(args.encoding, errors="replace")

    lines = text.splitlines()
    shown = lines if args.lines <= 0 else lines[: args.lines]
    for line in shown:
        print(line)

    print(
        f"\n앞 {human(len(raw))}만 받아 {len(shown)}줄 표시했습니다.",
        file=sys.stderr,
    )
    return 0


def cmd_mkdir(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    bucket, key = resolve_target(args.uri, bucket)
    if not key:
        raise SystemExit("만들 디렉터리 경로가 없습니다.")
    marker = as_directory(key)

    if args.dry_run:
        print(f"[예행] 빈 오브젝트 생성: s3://{bucket}/{marker}")
        return 0

    client.put_object(Bucket=bucket, Key=marker, Body=b"")
    print(f"디렉터리 표시를 만들었습니다: s3://{bucket}/{marker}")
    print("  참고: S3에는 디렉터리가 없습니다. 파일을 올릴 때 미리 만들 필요는 없고,")
    print("        콘솔에서 빈 폴더로 보이게 하려는 용도입니다.")
    return 0


def cmd_rm(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    bucket, key = resolve_target(args.uri, bucket)
    if not key or key.endswith("/"):
        raise SystemExit("파일 키를 지정하세요. 디렉터리를 지우려면 rmdir 을 쓰세요.")

    from botocore.exceptions import ClientError

    try:
        meta = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            print(f"없는 파일입니다: s3://{bucket}/{key}", file=sys.stderr)
            return 1
        raise

    print(f"삭제 대상: s3://{bucket}/{key} ({human(meta['ContentLength'])})")
    if args.dry_run:
        print("[예행] 지우지 않았습니다.")
        return 0
    if not confirm("지울까요?", args.yes):
        print("취소했습니다.")
        return 1

    client.delete_object(Bucket=bucket, Key=key)
    print("삭제했습니다.")
    return 0


def cmd_rmdir(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    bucket, key = resolve_target(args.uri, bucket)
    # 접두사가 비면 버킷 전체가 지워진다. 실수를 막기 위해 반드시 막는다.
    if not key.strip("/"):
        raise SystemExit(
            "접두사가 비어 있습니다. 버킷 전체를 지우는 것을 막기 위해 거부합니다."
        )
    prefix = as_directory(key)

    objects = list_objects(client, bucket, prefix)
    window = getattr(args, "older_than", None)
    if window:
        objects = older_than(objects, parse_duration(window))
    if not objects:
        scope = f" ({window} 보다 오래된 것)" if window else ""
        print(f"s3://{bucket}/{prefix} 아래에 지울 오브젝트가 없습니다{scope}.")
        return 0

    total = sum(o["Size"] for o in objects)
    scope = f" ({window} 보다 오래된 것만)" if window else ""
    print(f"삭제 대상: s3://{bucket}/{prefix}{scope}")
    for obj in sorted(objects, key=lambda o: o["Key"])[:10]:
        print(f"  {obj['Key']}  ({human(obj['Size'])})")
    if len(objects) > 10:
        print(f"  ... 외 {len(objects) - 10}개")
    print(f"모두 {len(objects)}개, 합계 {human(total)}")

    if args.dry_run:
        print("[예행] 지우지 않았습니다.")
        return 0
    if not confirm(f"{len(objects)}개를 모두 지울까요?", args.yes):
        print("취소했습니다.")
        return 1

    deleted = delete_keys(
        client,
        bucket,
        [o["Key"] for o in objects],
        progress.Progress(
            "삭제",
            total=len(objects),
            unit="개",
            enabled=len(objects) > DELETE_BATCH and not getattr(args, "no_progress", False),
        ),
    )
    print(f"{deleted}개 삭제했습니다.")
    return 0 if deleted == len(objects) else 1


COMMANDS = {
    "ls": cmd_ls,
    "upload": cmd_upload,
    "download": cmd_download,
    "head": cmd_head,
    "mkdir": cmd_mkdir,
    "rm": cmd_rm,
    "rmdir": cmd_rmdir,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bin/s3-ops",
        description="S3 파일·디렉터리 조작 (업로드, 다운로드, 삭제, 목록, 내용 확인)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  bin/s3-ops ls       s3://dw-stage/orders/\n"
            "  bin/s3-ops ls       s3://dw-stage/orders/ --summary\n"
            "  bin/s3-ops upload   ./out/ s3://dw-stage/orders/2026-08-01/ --recursive\n"
            "  bin/s3-ops download s3://dw-stage/orders/2026-08-01/ ./out/ --recursive\n"
            "  bin/s3-ops head     s3://dw-stage/orders/2026-08-01/part-0.csv.gz\n"
            "  bin/s3-ops rm       s3://dw-stage/orders/orders.csv --yes\n"
            "  bin/s3-ops rmdir    s3://dw-stage/orders/ --older-than 7d --yes\n"
            "\n"
            "  # --bucket 을 주면 s3:// 없이 키만 써도 됩니다\n"
            "  bin/s3-ops --bucket dw-stage ls impala/\n"
            "\n"
            "  # MinIO 등 S3 호환 스토리지\n"
            "  bin/s3-ops --endpoint http://minio:9000 --bucket dw-stage \\\n"
            "           --access-key minioadmin --secret-key minioadmin ls /\n"
            "\n"
            f"버킷과 자격증명은 {appconfig.DEFAULT_CONFIG} 의\n"
            "s3 섹션에서 자동으로 읽습니다. 아래 인자를 주면 그 값이 우선합니다.\n"
        ),
    )
    appconfig.add_config_arguments(parser)
    progress.add_progress_argument(parser)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="오류가 나면 전체 스택 트레이스를 출력합니다.",
    )
    connection = parser.add_argument_group("접속")
    connection.add_argument(
        "-b",
        "--bucket",
        help="기본 버킷. 지정하면 경로를 s3:// 없이 키만 줄 수 있습니다.",
    )
    connection.add_argument(
        "--access-key",
        metavar="KEY",
        help="AWS 액세스 키. 생략하면 AWS_ACCESS_KEY_ID 환경변수나 IAM 역할을 씁니다.",
    )
    connection.add_argument(
        "--secret-key",
        metavar="SECRET",
        help="AWS 시크릿 키. 명령행에 적으면 ps로 다른 사용자에게 보이므로, "
        "가능하면 AWS_SECRET_ACCESS_KEY 환경변수를 쓰세요.",
    )
    connection.add_argument(
        "--session-token", metavar="TOKEN", help="임시 자격증명(STS)을 쓸 때의 세션 토큰"
    )
    connection.add_argument("--region", help="AWS 리전")
    connection.add_argument(
        "--endpoint",
        "--endpoint-url",
        dest="endpoint",
        metavar="URL",
        help="S3 호환 스토리지 엔드포인트 (MinIO 등). 예: http://minio.example.com:9000",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="무엇을 할지만 보여주고 실행하지 않음"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("ls", help="접두사 아래 오브젝트 목록")
    ls.add_argument("uri", help="s3://버킷/접두사")
    ls.add_argument(
        "--summary", action="store_true", help="목록 대신 개수와 크기 분포만 봅니다"
    )
    ls.add_argument(
        "--dirs", action="store_true", help="파일 대신 한 단계 아래 디렉터리만 봅니다"
    )
    ls.add_argument(
        "--older-than",
        metavar="기간",
        help="이 기간보다 오래된 것만 (예: 24h, 7d, 90m). 단위를 빼면 시간입니다.",
    )

    upload = sub.add_parser("upload", help="파일 또는 디렉터리 업로드")
    upload.add_argument("source", help="로컬 파일 또는 디렉터리")
    upload.add_argument("uri", help="s3://버킷/키 (또는 s3://버킷/접두사/)")
    upload.add_argument("-r", "--recursive", action="store_true", help="디렉터리 전체 업로드")
    upload.add_argument("--sse", help="서버측 암호화 (예: AES256)")

    download = sub.add_parser("download", help="파일 또는 접두사 전체 다운로드")
    download.add_argument("uri", help="s3://버킷/키 (또는 s3://버킷/접두사/)")
    download.add_argument("destination", help="저장할 로컬 파일 또는 디렉터리")
    download.add_argument(
        "-r", "--recursive", action="store_true", help="접두사 아래 전체 다운로드"
    )
    download.add_argument(
        "--force", action="store_true", help="이미 있는 로컬 파일을 덮어씁니다"
    )

    head = sub.add_parser("head", help="오브젝트 앞부분 보기 (.gz 자동 해제)")
    head.add_argument("uri", help="s3://버킷/키")
    head.add_argument(
        "-n", "--lines", type=int, default=10, help="보여줄 줄 수 (기본 10, 0이면 전부)"
    )
    head.add_argument(
        "--max-bytes",
        type=int,
        default=64 * 1024,
        help="받아올 최대 바이트 (기본 64KB). 압축 파일이면 푼 뒤가 더 길어집니다.",
    )
    head.add_argument("--encoding", default="utf-8", help="파일 인코딩 (기본 utf-8)")
    head.add_argument("--raw", action="store_true", help="gzip 자동 해제를 하지 않습니다")

    mkdir = sub.add_parser("mkdir", help="디렉터리 표시용 빈 오브젝트 생성")
    mkdir.add_argument("uri", help="s3://버킷/접두사/")

    rm = sub.add_parser("rm", help="파일 하나 삭제")
    rm.add_argument("uri", help="s3://버킷/키")
    rm.add_argument("-y", "--yes", action="store_true", help="확인 없이 삭제")

    rmdir = sub.add_parser("rmdir", help="접두사 아래 오브젝트 전체 삭제")
    rmdir.add_argument("uri", help="s3://버킷/접두사/")
    rmdir.add_argument("-y", "--yes", action="store_true", help="확인 없이 삭제")
    rmdir.add_argument(
        "--older-than",
        metavar="기간",
        help="이 기간보다 오래된 것만 지웁니다 (예: 24h, 7d). 크론 정리에 씁니다.",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    # 인자 없이 실행하면 무엇을 할 수 있는지 보여준다. 오류가 아니므로 0으로 끝낸다.
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    timer = PhaseTimer(PHASES)

    with timer.measure("설정·클라이언트 준비"):
        client, bucket = make_client(args)

    try:
        with timer.measure("명령 실행"):
            return COMMANDS[args.command](client, args, bucket)
    except Exception as exc:
        # 크론 메일에 수백 줄짜리 스택 트레이스가 실리지 않게 한 줄로 줄인다.
        # 전체가 필요하면 --debug 로 본다.
        if args.debug:
            raise
        print(f"\n실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  전체 오류를 보려면 --debug 를 주세요.", file=sys.stderr)
        return 5
    finally:
        # 성공하든 실패하든 어디에 시간을 썼는지는 항상 남긴다
        timer.print_report()


if __name__ == "__main__":
    raise SystemExit(main())

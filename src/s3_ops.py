"""S3 파일·디렉터리 조작 (업로드, 삭제, 디렉터리 생성/삭제, 목록).

    bin/s3-ops ls     s3://dw-stage/impala/
    bin/s3-ops upload orders.csv s3://dw-stage/impala/orders.csv
    bin/s3-ops upload ./out/ s3://dw-stage/impala/out/ --recursive
    bin/s3-ops mkdir  s3://dw-stage/impala/2026-08-03/
    bin/s3-ops rm     s3://dw-stage/impala/orders.csv --yes
    bin/s3-ops rmdir  s3://dw-stage/impala/2026-08-03/ --yes

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


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:,.1f}{unit}"
        size /= 1024
    return f"{size:,.1f}GB"


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


def delete_keys(client: Any, bucket: str, keys: Sequence[str]) -> int:
    """키 목록을 1000개씩 묶어 지우고 실제로 지운 개수를 돌려준다."""
    deleted = 0
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


def cmd_ls(client: Any, args: argparse.Namespace, bucket: Optional[str] = None) -> int:
    bucket, key = resolve_target(args.uri, bucket)
    objects = list_objects(client, bucket, key)
    if not objects:
        print(f"s3://{bucket}/{key} 아래에 오브젝트가 없습니다.")
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
    for local, target in pairs:
        size = os.path.getsize(local)
        if args.dry_run:
            print(f"[예행] {local} → s3://{bucket}/{target} ({human(size)})")
            continue
        # upload_file 은 큰 파일을 알아서 멀티파트로 나눠 올린다
        client.upload_file(local, bucket, target, ExtraArgs=extra or None)
        total_bytes += size
        print(f"{local} → s3://{bucket}/{target} ({human(size)})")

    if not args.dry_run:
        print(f"\n{len(pairs)}개 업로드, 합계 {human(total_bytes)}")
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
    if not objects:
        print(f"s3://{bucket}/{prefix} 아래에 지울 오브젝트가 없습니다.")
        return 0

    total = sum(o["Size"] for o in objects)
    print(f"삭제 대상: s3://{bucket}/{prefix}")
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

    deleted = delete_keys(client, bucket, [o["Key"] for o in objects])
    print(f"{deleted}개 삭제했습니다.")
    return 0 if deleted == len(objects) else 1


COMMANDS = {
    "ls": cmd_ls,
    "upload": cmd_upload,
    "mkdir": cmd_mkdir,
    "rm": cmd_rm,
    "rmdir": cmd_rmdir,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bin/s3-ops",
        description="S3 파일·디렉터리 조작 (업로드, 삭제, 디렉터리 생성/삭제, 목록)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  bin/s3-ops ls     s3://dw-stage/impala/\n"
            "  bin/s3-ops upload orders.csv s3://dw-stage/impala/\n"
            "  bin/s3-ops upload ./out/ s3://dw-stage/impala/out/ --recursive\n"
            "  bin/s3-ops mkdir  s3://dw-stage/impala/2026-08-03/\n"
            "  bin/s3-ops rm     s3://dw-stage/impala/orders.csv --yes\n"
            "  bin/s3-ops rmdir  s3://dw-stage/impala/2026-08-03/ --yes\n"
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

    upload = sub.add_parser("upload", help="파일 또는 디렉터리 업로드")
    upload.add_argument("source", help="로컬 파일 또는 디렉터리")
    upload.add_argument("uri", help="s3://버킷/키 (또는 s3://버킷/접두사/)")
    upload.add_argument("-r", "--recursive", action="store_true", help="디렉터리 전체 업로드")
    upload.add_argument("--sse", help="서버측 암호화 (예: AES256)")

    mkdir = sub.add_parser("mkdir", help="디렉터리 표시용 빈 오브젝트 생성")
    mkdir.add_argument("uri", help="s3://버킷/접두사/")

    rm = sub.add_parser("rm", help="파일 하나 삭제")
    rm.add_argument("uri", help="s3://버킷/키")
    rm.add_argument("-y", "--yes", action="store_true", help="확인 없이 삭제")

    rmdir = sub.add_parser("rmdir", help="접두사 아래 오브젝트 전체 삭제")
    rmdir.add_argument("uri", help="s3://버킷/접두사/")
    rmdir.add_argument("-y", "--yes", action="store_true", help="확인 없이 삭제")

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
    client, bucket = make_client(args)
    return COMMANDS[args.command](client, args, bucket)


if __name__ == "__main__":
    raise SystemExit(main())

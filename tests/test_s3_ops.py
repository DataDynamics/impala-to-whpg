"""src/s3_ops.py 검증 (가짜 S3 클라이언트로 실행)."""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import s3_ops as s  # noqa: E402


class ClientError(Exception):
    """botocore.exceptions.ClientError 흉내."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self, objects: Dict[str, bytes] = None) -> None:
        self.objects: Dict[str, bytes] = dict(objects or {})
        self.uploaded: List[Any] = []
        self.delete_calls: List[List[str]] = []
        self.fail_deleting: set = set()

    # -- 목록 --
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix="", Delimiter=None):
                items = [
                    {
                        "Key": k,
                        "Size": len(v),
                        "LastModified": datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc),
                    }
                    for k, v in sorted(client.objects.items())
                    if k.startswith(Prefix)
                ]
                # 페이지네이션 동작까지 흉내내려고 2개씩 나눠 준다
                if not items:
                    return iter([{}])
                return iter(
                    [{"Contents": items[i : i + 2]} for i in range(0, len(items), 2)]
                )

        return Paginator()

    # -- 쓰기 --
    def upload_file(self, local, bucket, key, ExtraArgs=None):
        self.uploaded.append((local, bucket, key, ExtraArgs))
        self.objects[key] = Path(local).read_bytes()

    def put_object(self, Bucket, Key, Body=b""):
        self.objects[Key] = Body

    # -- 읽기 --
    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError("404")
        return {"ContentLength": len(self.objects[Key])}

    # -- 삭제 --
    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.delete_calls.append(keys)
        errors = []
        for key in keys:
            if key in self.fail_deleting:
                errors.append({"Key": key, "Message": "권한 없음"})
                continue
            self.objects.pop(key, None)
        return {"Errors": errors} if errors else {}


@pytest.fixture(autouse=True)
def patch_client_error(monkeypatch):
    """cmd_rm 이 임포트하는 botocore 예외를 가짜로 바꾼다."""
    import types

    module = types.ModuleType("botocore.exceptions")
    module.ClientError = ClientError
    botocore = types.ModuleType("botocore")
    botocore.exceptions = module
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", module)


def args_for(command: str, **overrides) -> argparse.Namespace:
    options = dict(
        command=command,
        config=None,
        bucket=None,
        access_key=None,
        secret_key=None,
        session_token=None,
        region=None,
        endpoint=None,
        dry_run=False,
        yes=True,
        recursive=False,
        sse=None,
    )
    options.update(overrides)
    return argparse.Namespace(**options)


# -- URI 파싱 ---------------------------------------------------------------------


def test_parse_s3_uri():
    assert s.parse_s3_uri("s3://dw-stage/impala/orders.csv") == ("dw-stage", "impala/orders.csv")
    assert s.parse_s3_uri("s3://dw-stage/impala/") == ("dw-stage", "impala/")
    assert s.parse_s3_uri("s3://dw-stage") == ("dw-stage", "")


@pytest.mark.parametrize("bad", ["dw-stage/key", "http://x/y", "s3://", "/tmp/a"])
def test_parse_s3_uri_rejects_bad_input(bad):
    with pytest.raises(SystemExit):
        s.parse_s3_uri(bad)


def test_as_directory():
    assert s.as_directory("a/b") == "a/b/"
    assert s.as_directory("a/b/") == "a/b/"


def test_resolve_target_prefers_uri_over_default_bucket():
    assert s.resolve_target("s3://from-uri/key", "from-flag") == ("from-uri", "key")


def test_resolve_target_uses_default_bucket_for_bare_key():
    assert s.resolve_target("impala/orders.csv", "dw-stage") == ("dw-stage", "impala/orders.csv")
    assert s.resolve_target("/impala/", "dw-stage") == ("dw-stage", "impala/")
    assert s.resolve_target("", "dw-stage") == ("dw-stage", "")


def test_resolve_target_without_bucket_explains():
    with pytest.raises(SystemExit, match="--bucket"):
        s.resolve_target("impala/orders.csv", None)


def test_bare_key_works_with_bucket_flag():
    """--bucket 을 주면 모든 명령이 s3:// 없이 동작한다."""
    client = FakeS3Client({"impala/a.csv": b"1"})
    assert s.cmd_ls(client, args_for("ls", uri="impala/"), "dw-stage") == 0
    assert s.cmd_rm(client, args_for("rm", uri="impala/a.csv"), "dw-stage") == 0
    assert client.objects == {}


# -- 자격증명과 엔드포인트 --------------------------------------------------------


@pytest.fixture
def fake_boto3(monkeypatch):
    """boto3.session.Session 호출 인자를 기록한다."""
    import types

    calls = {}
    client = FakeS3Client()

    class Session:
        def __init__(self, **kwargs):
            calls["session"] = kwargs

        def client(self, name, endpoint_url=None):
            calls["client"] = {"name": name, "endpoint_url": endpoint_url}
            return client

    session_module = types.ModuleType("boto3.session")
    session_module.Session = Session
    boto3 = types.ModuleType("boto3")
    boto3.session = session_module
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "boto3.session", session_module)
    return calls


def test_credentials_are_passed_to_boto3(fake_boto3):
    args = args_for(
        "ls",
        access_key="AKIA...",
        secret_key="secret...",
        session_token="token...",
        region="ap-northeast-2",
        endpoint="http://minio:9000",
        bucket="dw-stage",
    )
    client, bucket = s.make_client(args)

    assert fake_boto3["session"] == {
        "aws_access_key_id": "AKIA...",
        "aws_secret_access_key": "secret...",
        "aws_session_token": "token...",
        "region_name": "ap-northeast-2",
    }
    assert fake_boto3["client"]["endpoint_url"] == "http://minio:9000"
    assert bucket == "dw-stage"


def test_missing_credentials_fall_back_to_boto3_chain(fake_boto3):
    """키를 주지 않으면 None이 넘어가 환경변수/IAM 역할이 그대로 쓰인다."""
    s.make_client(args_for("ls"))

    assert fake_boto3["session"] == {
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "aws_session_token": None,
        "region_name": None,
    }
    # endpoint_url 은 빈 값이면 None 이어야 boto3 기본 주소를 쓴다
    assert fake_boto3["client"]["endpoint_url"] is None


def test_endpoint_url_alias_is_accepted():
    parser = s.build_parser()
    assert parser.parse_args(["--endpoint", "http://x", "ls", "s3://b/"]).endpoint == "http://x"
    assert parser.parse_args(["--endpoint-url", "http://x", "ls", "s3://b/"]).endpoint == "http://x"


def test_connection_options_exist():
    parser = s.build_parser()
    options = {o for a in parser._actions for o in a.option_strings}
    for expected in ("--access-key", "--secret-key", "--bucket", "--endpoint", "--region"):
        assert expected in options


def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_config_values_are_overridden_by_flags(tmp_path, fake_boto3):
    """설정 파일보다 명령행 인자가 우선한다."""
    path = write_config(
        tmp_path,
        "s3:\n"
        "  bucket: from-config\n"
        "  access_key_id: config-key\n"
        "  secret_access_key: config-secret\n"
        "  region: us-east-1\n",
    )

    _, bucket = s.make_client(
        args_for(
            "ls",
            config=path,
            access_key="cli-key",
            endpoint="http://minio:9000",
            bucket="cli-bucket",
        )
    )

    assert fake_boto3["session"]["aws_access_key_id"] == "cli-key"            # 덮어씀
    assert fake_boto3["session"]["aws_secret_access_key"] == "config-secret"  # 안 준 값은 유지
    assert fake_boto3["session"]["region_name"] == "us-east-1"
    assert fake_boto3["client"]["endpoint_url"] == "http://minio:9000"
    assert bucket == "cli-bucket"


def test_config_bucket_is_used_when_flag_is_absent(tmp_path, fake_boto3):
    path = write_config(tmp_path, "s3:\n  bucket: from-config\n")

    _, bucket = s.make_client(args_for("ls", config=path))
    assert bucket == "from-config"


def test_config_ignores_load_only_settings(tmp_path, fake_boto3):
    """적재 파이프라인 전용 키가 있어도 그대로 읽힌다."""
    path = write_config(
        tmp_path,
        "s3:\n"
        "  bucket: dw-stage\n"
        "  prefix: impala-to-greenplum\n"
        "  file_size_mb: 128\n"
        "  protocol: pxf\n"
        "  cleanup: false\n",
    )

    _, bucket = s.make_client(args_for("ls", config=path))
    assert bucket == "dw-stage"
    assert fake_boto3["session"]["aws_access_key_id"] is None  # 기본 자격증명 체인에 맡긴다


def test_config_expands_environment_variables(tmp_path, fake_boto3, monkeypatch):
    monkeypatch.setenv("TEST_S3_KEY", "env-key")
    path = write_config(
        tmp_path,
        "s3:\n"
        "  bucket: dw-stage\n"
        "  access_key_id: ${TEST_S3_KEY}\n"
        "  region: ${TEST_S3_REGION:-ap-northeast-2}\n",
    )

    s.make_client(args_for("ls", config=path))
    assert fake_boto3["session"]["aws_access_key_id"] == "env-key"
    assert fake_boto3["session"]["region_name"] == "ap-northeast-2"  # 기본값


def test_config_undefined_environment_variable_fails(tmp_path, fake_boto3, monkeypatch):
    monkeypatch.delenv("TEST_S3_MISSING", raising=False)
    path = write_config(
        tmp_path, "s3:\n  bucket: dw-stage\n  access_key_id: ${TEST_S3_MISSING}\n"
    )

    with pytest.raises(SystemExit) as exc:
        s.make_client(args_for("ls", config=path))
    assert "TEST_S3_MISSING" in str(exc.value)


def test_config_without_s3_section_fails(tmp_path, fake_boto3):
    path = write_config(tmp_path, "impala:\n  host: impala.example.com\n")

    with pytest.raises(SystemExit) as exc:
        s.make_client(args_for("ls", config=path))
    assert "s3 섹션" in str(exc.value)


# -- 목록 -------------------------------------------------------------------------


def test_ls_lists_all_pages(capsys):
    client = FakeS3Client({f"impala/part-{i}.csv": b"x" * 10 for i in range(5)})
    assert s.cmd_ls(client, args_for("ls", uri="s3://dw-stage/impala/")) == 0

    output = capsys.readouterr().out
    # 페이지네이터로 5개를 모두 받아야 한다(페이지당 2개씩)
    assert output.count("part-") == 5
    assert "5개" in output


def test_ls_empty(capsys):
    assert s.cmd_ls(FakeS3Client(), args_for("ls", uri="s3://dw-stage/none/")) == 0
    assert "없습니다" in capsys.readouterr().out


def test_ls_marks_directory_placeholders(capsys):
    client = FakeS3Client({"impala/2026-08-03/": b"", "impala/a.csv": b"xx"})
    s.cmd_ls(client, args_for("ls", uri="s3://dw-stage/impala/"))
    assert "<디렉터리 표시>" in capsys.readouterr().out


# -- 업로드 -----------------------------------------------------------------------


def test_upload_single_file(tmp_path):
    local = tmp_path / "orders.csv"
    local.write_bytes(b"a,b,c\n")
    client = FakeS3Client()

    s.cmd_upload(
        client,
        args_for("upload", source=str(local), uri="s3://dw-stage/impala/orders.csv"),
    )
    assert client.objects["impala/orders.csv"] == b"a,b,c\n"


def test_upload_to_prefix_keeps_filename(tmp_path):
    local = tmp_path / "orders.csv"
    local.write_bytes(b"x")
    client = FakeS3Client()

    s.cmd_upload(client, args_for("upload", source=str(local), uri="s3://dw-stage/impala/"))
    assert "impala/orders.csv" in client.objects


def test_upload_directory_preserves_structure(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.csv").write_bytes(b"1")
    (tmp_path / "sub" / "b.csv").write_bytes(b"2")
    client = FakeS3Client()

    s.cmd_upload(
        client,
        args_for("upload", source=str(tmp_path), uri="s3://dw-stage/out/", recursive=True),
    )
    assert set(client.objects) == {"out/a.csv", "out/sub/b.csv"}


def test_upload_directory_without_recursive_fails(tmp_path):
    with pytest.raises(SystemExit, match="--recursive"):
        s.cmd_upload(
            FakeS3Client(),
            args_for("upload", source=str(tmp_path), uri="s3://dw-stage/out/"),
        )


def test_upload_missing_file(tmp_path):
    with pytest.raises(SystemExit, match="찾을 수 없습니다"):
        s.cmd_upload(
            FakeS3Client(),
            args_for("upload", source=str(tmp_path / "none.csv"), uri="s3://b/k"),
        )


def test_upload_passes_sse(tmp_path):
    local = tmp_path / "a.csv"
    local.write_bytes(b"x")
    client = FakeS3Client()

    s.cmd_upload(
        client, args_for("upload", source=str(local), uri="s3://b/k.csv", sse="AES256")
    )
    assert client.uploaded[0][3] == {"ServerSideEncryption": "AES256"}


def test_upload_dry_run_uploads_nothing(tmp_path, capsys):
    local = tmp_path / "a.csv"
    local.write_bytes(b"x")
    client = FakeS3Client()

    s.cmd_upload(
        client, args_for("upload", source=str(local), uri="s3://b/k.csv", dry_run=True)
    )
    assert client.objects == {}
    assert "[예행]" in capsys.readouterr().out


# -- 디렉터리 생성 -----------------------------------------------------------------


def test_mkdir_creates_empty_marker():
    client = FakeS3Client()
    s.cmd_mkdir(client, args_for("mkdir", uri="s3://dw-stage/impala/2026-08-03"))
    # 슬래시를 붙여 빈 오브젝트를 만든다
    assert client.objects == {"impala/2026-08-03/": b""}


def test_mkdir_requires_a_path():
    with pytest.raises(SystemExit, match="경로가 없습니다"):
        s.cmd_mkdir(FakeS3Client(), args_for("mkdir", uri="s3://dw-stage"))


def test_mkdir_dry_run(capsys):
    client = FakeS3Client()
    s.cmd_mkdir(client, args_for("mkdir", uri="s3://b/p/", dry_run=True))
    assert client.objects == {}
    assert "[예행]" in capsys.readouterr().out


# -- 파일 삭제 ---------------------------------------------------------------------


def test_rm_deletes_file():
    client = FakeS3Client({"impala/orders.csv": b"xyz"})
    assert s.cmd_rm(client, args_for("rm", uri="s3://dw-stage/impala/orders.csv")) == 0
    assert client.objects == {}


def test_rm_missing_file_reports(capsys):
    client = FakeS3Client()
    assert s.cmd_rm(client, args_for("rm", uri="s3://dw-stage/none.csv")) == 1
    assert "없는 파일" in capsys.readouterr().err


def test_rm_rejects_directory_key():
    with pytest.raises(SystemExit, match="rmdir"):
        s.cmd_rm(FakeS3Client(), args_for("rm", uri="s3://dw-stage/impala/"))


def test_rm_dry_run_keeps_file(capsys):
    client = FakeS3Client({"a.csv": b"x"})
    s.cmd_rm(client, args_for("rm", uri="s3://b/a.csv", dry_run=True))
    assert "a.csv" in client.objects
    assert "[예행]" in capsys.readouterr().out


def test_rm_without_yes_and_no_tty_refuses(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    client = FakeS3Client({"a.csv": b"x"})
    assert s.cmd_rm(client, args_for("rm", uri="s3://b/a.csv", yes=False)) == 1
    assert "a.csv" in client.objects  # 지워지지 않아야 한다


# -- 디렉터리 삭제 -----------------------------------------------------------------


def test_rmdir_deletes_everything_under_prefix():
    client = FakeS3Client(
        {
            "impala/2026-08-03/a.csv": b"1",
            "impala/2026-08-03/sub/b.csv": b"2",
            "impala/2026-08-04/c.csv": b"3",  # 다른 날짜는 남아야 한다
        }
    )
    assert s.cmd_rmdir(client, args_for("rmdir", uri="s3://dw-stage/impala/2026-08-03/")) == 0
    assert set(client.objects) == {"impala/2026-08-04/c.csv"}


def test_rmdir_refuses_empty_prefix():
    """접두사가 비면 버킷 전체가 지워지므로 반드시 막아야 한다."""
    for uri in ("s3://dw-stage", "s3://dw-stage/", "s3://dw-stage///"):
        with pytest.raises(SystemExit, match="버킷 전체"):
            s.cmd_rmdir(FakeS3Client({"a": b"x"}), args_for("rmdir", uri=uri))


def test_rmdir_nothing_to_delete(capsys):
    assert s.cmd_rmdir(FakeS3Client(), args_for("rmdir", uri="s3://b/none/")) == 0
    assert "없습니다" in capsys.readouterr().out


def test_rmdir_dry_run_keeps_objects(capsys):
    client = FakeS3Client({"p/a.csv": b"1", "p/b.csv": b"2"})
    s.cmd_rmdir(client, args_for("rmdir", uri="s3://b/p/", dry_run=True))
    assert len(client.objects) == 2
    assert "[예행]" in capsys.readouterr().out


def test_rmdir_without_yes_and_no_tty_refuses(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    client = FakeS3Client({"p/a.csv": b"1"})
    assert s.cmd_rmdir(client, args_for("rmdir", uri="s3://b/p/", yes=False)) == 1
    assert client.objects  # 남아 있어야 한다


def test_rmdir_batches_deletes(monkeypatch):
    monkeypatch.setattr(s, "DELETE_BATCH", 3)
    client = FakeS3Client({f"p/{i}.csv": b"x" for i in range(7)})
    s.cmd_rmdir(client, args_for("rmdir", uri="s3://b/p/"))

    assert client.objects == {}
    assert [len(c) for c in client.delete_calls] == [3, 3, 1]


def test_rmdir_reports_partial_failure(capsys):
    client = FakeS3Client({"p/a.csv": b"1", "p/b.csv": b"2"})
    client.fail_deleting = {"p/b.csv"}
    assert s.cmd_rmdir(client, args_for("rmdir", uri="s3://b/p/")) == 1

    assert "p/b.csv" in client.objects
    assert "삭제 실패" in capsys.readouterr().err


def test_rmdir_shows_preview_and_count(capsys):
    client = FakeS3Client({f"p/{i:02d}.csv": b"x" for i in range(15)})
    s.cmd_rmdir(client, args_for("rmdir", uri="s3://b/p/"))
    output = capsys.readouterr().out
    assert "... 외 5개" in output  # 앞의 10개만 보여준다
    assert "모두 15개" in output


# -- 확인 절차 --------------------------------------------------------------------


def test_confirm_yes_flag_skips_prompt():
    assert s.confirm("지울까요?", assume_yes=True) is True


def test_confirm_accepts_y(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert s.confirm("지울까요?", assume_yes=False) is True


def test_confirm_rejects_anything_else(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    for answer in ("", "n", "no", "아니오"):
        monkeypatch.setattr("builtins.input", lambda _, a=answer: a)
        assert s.confirm("지울까요?", assume_yes=False) is False


# -- CLI --------------------------------------------------------------------------


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        s.build_parser().parse_args([])


def test_parser_accepts_all_commands():
    parser = s.build_parser()
    assert parser.parse_args(["ls", "s3://b/p/"]).command == "ls"
    assert parser.parse_args(["upload", "a", "s3://b/k"]).command == "upload"
    assert parser.parse_args(["mkdir", "s3://b/p/"]).command == "mkdir"
    assert parser.parse_args(["rm", "s3://b/k"]).command == "rm"
    assert parser.parse_args(["rmdir", "s3://b/p/"]).command == "rmdir"


def test_every_command_has_a_handler():
    parser = s.build_parser()
    subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.dest == "command"]
    assert set(subparsers[0].choices) == set(s.COMMANDS)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

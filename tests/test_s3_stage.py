import gzip
from typing import List

import pytest

from impala_to_greenplum.config import S3Config
from impala_to_greenplum.s3_stage import S3Stager, render_gp_s3_config


class FakeS3Client:
    def __init__(self, fail_on: str = "") -> None:
        self.objects: dict = {}
        self.deleted: List[str] = []
        self.fail_on = fail_on

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        if self.fail_on and self.fail_on in key:
            raise RuntimeError("업로드 실패")
        self.objects[key] = fileobj.read()
        self.last_extra_args = ExtraArgs

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
            self.deleted.append(item["Key"])
        return {}


def make(**overrides) -> S3Config:
    options = dict(
        bucket="dw-stage",
        prefix="impala",
        endpoint="s3.ap-northeast-2.amazonaws.com",
        gp_config="/home/gpadmin/s3.conf",
    )
    options.update(overrides)
    return S3Config(**options)


def decode(client: FakeS3Client) -> str:
    return "".join(gzip.decompress(b).decode() for _, b in sorted(client.objects.items()))


# -- LOCATION 생성 ----------------------------------------------------------------


def test_s3_location_includes_endpoint_bucket_and_config():
    stager = S3Stager(make(region="ap-northeast-2", gp_config_section="prod"))
    location = stager.location("orders-abc123")
    assert location.startswith("s3://s3.ap-northeast-2.amazonaws.com/dw-stage/impala/orders-abc123/")
    assert "region=ap-northeast-2" in location
    assert "config=/home/gpadmin/s3.conf" in location
    assert "section=prod" in location


def test_s3_location_with_config_server():
    stager = S3Stager(make(gp_config=None, gp_config_server="http://gpconf:8080/s3.conf"))
    assert "config_server=http://gpconf:8080/s3.conf" in stager.location("run1")


def test_pxf_location():
    stager = S3Stager(S3Config(bucket="dw-stage", prefix="impala", protocol="pxf", pxf_server="s3srv"))
    location = stager.location("orders-abc")
    assert location.startswith("pxf://dw-stage/impala/orders-abc/?")
    assert "PROFILE=s3:text" in location
    assert "SERVER=s3srv" in location


def test_empty_prefix_still_builds_valid_path():
    stager = S3Stager(make(prefix=""))
    assert stager.run_prefix("run1") == "run1/"


def test_run_id_sanitizes_table_name():
    run_id = S3Stager.new_run_id("staging.orders")
    assert run_id.startswith("staging_orders-")
    assert "/" not in run_id and "." not in run_id


# -- 업로드 -----------------------------------------------------------------------


def test_stage_uploads_single_gzip_file():
    client = FakeS3Client()
    stager = S3Stager(make(), client=client)
    staged = stager.stage([(1, "가나다"), (2, None)], "run1")

    assert staged.file_count == 1
    assert staged.row_count == 2
    assert staged.keys == ["impala/run1/part-00000.tsv.gz"]
    assert decode(client) == "1\t가나다\n2\t\\N\n"
    assert staged.compressed_bytes > 0
    assert staged.uncompressed_bytes == len("1\t가나다\n2\t\\N\n".encode())


def test_stage_without_compression_uses_plain_extension():
    client = FakeS3Client()
    stager = S3Stager(make(compress=False), client=client)
    staged = stager.stage([(1, "a")], "run1")

    assert staged.keys == ["impala/run1/part-00000.tsv"]
    assert client.objects["impala/run1/part-00000.tsv"] == b"1\ta\n"


def test_stage_splits_by_file_size():
    client = FakeS3Client()
    stager = S3Stager(make(file_size_mb=1), client=client)
    rows = [(i, "x" * 1000) for i in range(3000)]  # 약 3MB
    staged = stager.stage(rows, "run1")

    assert staged.file_count >= 3
    assert staged.row_count == 3000
    assert decode(client).count("\n") == 3000
    # 파일명은 순번대로 매겨진다
    assert staged.keys[0].endswith("part-00000.tsv.gz")
    assert staged.keys[1].endswith("part-00001.tsv.gz")


def test_stage_empty_rows_uploads_nothing():
    client = FakeS3Client()
    staged = S3Stager(make(), client=client).stage([], "run1")

    assert staged.file_count == 0
    assert staged.row_count == 0
    assert client.objects == {}


def test_stage_reports_progress():
    client = FakeS3Client()
    seen: List[int] = []
    stager = S3Stager(make(file_size_mb=1), client=client)
    stager.stage([(i, "x" * 200) for i in range(25000)], "run1", on_progress=seen.append)
    assert sum(seen) == 25000


def test_stage_propagates_upload_failure():
    client = FakeS3Client(fail_on="part-00000")
    stager = S3Stager(make(file_size_mb=1), client=client)
    with pytest.raises(RuntimeError, match="업로드 실패"):
        stager.stage([(i, "x" * 1000) for i in range(3000)], "run1")
    # 실패해도 시도한 키는 정리 대상으로 남는다
    assert stager.staged_keys("run1")


def test_server_side_encryption_is_passed_through():
    client = FakeS3Client()
    stager = S3Stager(make(server_side_encryption="AES256"), client=client)
    stager.stage([(1, "a")], "run1")
    assert client.last_extra_args == {"ServerSideEncryption": "AES256"}


# -- 정리 -------------------------------------------------------------------------


def test_cleanup_deletes_only_staged_keys():
    client = FakeS3Client()
    client.objects["impala/other-job/part-00000.tsv.gz"] = "남의 파일".encode()
    stager = S3Stager(make(), client=client)
    staged = stager.stage([(1, "a")], "run1")

    assert stager.cleanup(staged.keys) == 1
    assert list(client.objects) == ["impala/other-job/part-00000.tsv.gz"]


def test_cleanup_without_keys_is_noop():
    client = FakeS3Client()
    assert S3Stager(make(), client=client).cleanup() == 0
    assert client.deleted == []


def test_staged_keys_filters_by_run():
    client = FakeS3Client()
    stager = S3Stager(make(), client=client)
    stager.stage([(1, "a")], "run1")
    stager.stage([(2, "b")], "run2")

    assert stager.staged_keys("run1") == ["impala/run1/part-00000.tsv.gz"]
    assert len(stager.staged_keys()) == 2


# -- 설정 파일 생성 ----------------------------------------------------------------


def test_render_gp_s3_config():
    rendered = render_gp_s3_config("AKIA...", "secret...", section="prod")
    assert "[prod]" in rendered
    assert "accessid = AKIA..." in rendered
    assert "secret = secret..." in rendered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

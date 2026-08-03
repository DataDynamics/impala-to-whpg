import textwrap

import pytest

from impala_to_greenplum.config import ConfigError, JobConfig, S3Config, load_config


def write(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


BASE = """
    impala:
      host: impala.test
      database: sales
    greenplum:
      host: gp.test
      database: dw
      user: etl
      password: ${GP_PASSWORD}
      schema: staging
    jobs:
      - query: SELECT id, name FROM sales.orders
        target_table: orders
        mode: truncate
        load_method: copy
"""

S3_SECTION = """
    s3:
      bucket: dw-stage
      prefix: impala
      endpoint: s3.ap-northeast-2.amazonaws.com
      region: ap-northeast-2
      gp_config: /home/gpadmin/s3.conf
"""


def test_load_config_expands_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GP_PASSWORD", "s3cret")
    config = load_config(write(tmp_path, BASE))

    assert config.impala.host == "impala.test"
    assert config.greenplum.password == "s3cret"
    assert len(config.jobs) == 1
    assert config.jobs[0].select_sql() == "SELECT id, name FROM sales.orders"


def test_env_default_value(tmp_path, monkeypatch):
    monkeypatch.delenv("GP_PASSWORD", raising=False)
    body = BASE.replace("${GP_PASSWORD}", "${GP_PASSWORD:-fallback}")
    assert load_config(write(tmp_path, body)).greenplum.password == "fallback"


def test_missing_env_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("GP_PASSWORD", raising=False)
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, BASE))


def test_unknown_key_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GP_PASSWORD", "x")
    body = BASE.replace("      host: impala.test", "      host: impala.test\n      typo_key: 1")
    with pytest.raises(ConfigError, match="typo_key"):
        load_config(write(tmp_path, body))


def test_missing_section_raises(tmp_path):
    with pytest.raises(ConfigError, match="jobs"):
        load_config(write(tmp_path, "impala:\n  host: h\ngreenplum:\n  host: g\n"))


def test_job_requires_exactly_one_source():
    with pytest.raises(ConfigError):
        JobConfig(target_table="t")
    with pytest.raises(ConfigError):
        JobConfig(query="SELECT 1", source_table="db.t", target_table="t")


def test_job_validates_mode_and_keys():
    with pytest.raises(ConfigError, match="mode"):
        JobConfig(query="SELECT 1", target_table="t", mode="merge")
    with pytest.raises(ConfigError, match="key_columns"):
        JobConfig(query="SELECT 1", target_table="t", mode="upsert")


def test_s3_section_is_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("GP_PASSWORD", "x")
    body = BASE.replace("        load_method: copy\n", "") + S3_SECTION
    config = load_config(write(tmp_path, body))

    assert config.s3 is not None
    assert config.s3.bucket == "dw-stage"
    assert config.jobs[0].load_method == "s3"  # 기본값


def test_s3_load_method_without_s3_section_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GP_PASSWORD", "x")
    body = BASE.replace("        load_method: copy", "        load_method: s3")
    with pytest.raises(ConfigError, match="s3' 섹션이 없습니다"):
        load_config(write(tmp_path, body))


def test_invalid_load_method_raises():
    with pytest.raises(ConfigError, match="load_method"):
        JobConfig(query="SELECT 1", target_table="t", load_method="gpfdist")


def test_s3_protocol_requires_endpoint_and_config():
    with pytest.raises(ConfigError, match="endpoint"):
        S3Config(bucket="b", gp_config="/x/s3.conf")
    with pytest.raises(ConfigError, match="gp_config"):
        S3Config(bucket="b", endpoint="s3.amazonaws.com")


def test_pxf_protocol_requires_server():
    with pytest.raises(ConfigError, match="pxf_server"):
        S3Config(bucket="b", protocol="pxf")
    assert S3Config(bucket="b", protocol="pxf", pxf_server="s3srv").pxf_server == "s3srv"


def test_s3_rejects_invalid_protocol_and_sizes():
    with pytest.raises(ConfigError, match="protocol"):
        S3Config(bucket="b", protocol="gpfdist")
    with pytest.raises(ConfigError, match="file_size_mb"):
        S3Config(bucket="b", endpoint="e", gp_config="/c", file_size_mb=0)
    with pytest.raises(ConfigError, match="max_upload_workers"):
        S3Config(bucket="b", endpoint="e", gp_config="/c", max_upload_workers=0)


def test_source_table_builds_select():
    job = JobConfig(source_table="sales.orders", target_table="orders")
    assert job.select_sql() == "SELECT * FROM sales.orders"


def test_trailing_semicolon_is_stripped():
    job = JobConfig(query="SELECT 1;", target_table="t")
    assert job.select_sql() == "SELECT 1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

import textwrap

import pytest

from impala_to_greenplum.config import ConfigError, JobConfig, load_config


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


def test_source_table_builds_select():
    job = JobConfig(source_table="sales.orders", target_table="orders")
    assert job.select_sql() == "SELECT * FROM sales.orders"


def test_trailing_semicolon_is_stripped():
    job = JobConfig(query="SELECT 1;", target_table="t")
    assert job.select_sql() == "SELECT 1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

"""impyla 임포트 실패와 SASL 의존성 누락을 진단하는 코드 검증."""

import importlib.util
import sys
import types

import pytest

from impala_to_greenplum import source


@pytest.fixture
def no_impala(monkeypatch):
    """impala 패키지가 아예 없는 상태를 만든다."""
    monkeypatch.setitem(sys.modules, "impala", None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)


def spec_for(origin, is_package):
    spec = types.SimpleNamespace()
    spec.origin = origin
    spec.submodule_search_locations = ["/some/dir"] if is_package else None
    return spec


# -- impyla 임포트 진단 -----------------------------------------------------------


def test_missing_impyla_suggests_install(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    hint = source._import_hint(ImportError("No module named 'impala'"))
    assert "설치되어 있지 않습니다" in hint
    assert "pip install impyla" in hint


def test_shadowing_file_is_reported(monkeypatch):
    # impala.py 파일이 진짜 패키지를 가리는 경우
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: spec_for("/work/impala.py", is_package=False)
    )
    hint = source._import_hint(ImportError("'impala' is not a package"))
    assert "/work/impala.py" in hint
    assert "가리고 있습니다" in hint


def test_shadowing_directory_is_reported(monkeypatch):
    # __init__.py 없는 impala/ 디렉터리가 네임스페이스 패키지로 잡히는 경우
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: spec_for(None, is_package=True)
    )
    hint = source._import_hint(ImportError("No module named 'impala.dbapi'"))
    assert "디렉터리가 impyla 패키지를 가리고 있습니다" in hint


def test_real_package_but_broken_import(monkeypatch):
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: spec_for("/site-packages/impala/__init__.py", is_package=True),
    )
    hint = source._import_hint(ImportError("cannot import name 'dbapi'"))
    assert "불러오지 못했습니다" in hint


def test_import_error_is_wrapped_with_hint(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setitem(sys.modules, "impala", None)  # 임포트 실패를 강제한다
    with pytest.raises(ImportError, match="pip install impyla"):
        source.import_impala_dbapi()


# -- SASL 의존성 확인 -------------------------------------------------------------


def test_nosasl_needs_nothing(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    source.check_auth_dependencies("NOSASL")  # 예외가 나면 안 된다
    source.check_auth_dependencies("nosasl")
    source.check_auth_dependencies("")


@pytest.mark.parametrize("mechanism", ["PLAIN", "LDAP", "GSSAPI"])
def test_sasl_mechanisms_require_packages(monkeypatch, mechanism):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ImportError) as exc:
        source.check_auth_dependencies(mechanism)

    message = str(exc.value)
    assert mechanism in message
    assert "thrift_sasl" in message and "puresasl" in message
    assert "pip install pure-sasl thrift-sasl" in message
    # 데비안 계열의 빌드 실패 우회법까지 알려준다
    assert "--use-pep517" in message


def test_only_missing_packages_are_listed(monkeypatch):
    # thrift_sasl은 있고 puresasl만 없는 상태
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "puresasl" else object(),
    )
    with pytest.raises(ImportError) as exc:
        source.check_auth_dependencies("PLAIN")

    message = str(exc.value)
    assert "puresasl" in message
    assert "thrift_sasl," not in message  # 이미 있는 건 나열하지 않는다


def test_all_present_passes(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    source.check_auth_dependencies("PLAIN")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

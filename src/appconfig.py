"""bin/ 스크립트가 공유하는 설정 파일 로더.

모든 스크립트는 아무것도 지정하지 않아도 저장소의 ``conf/config.yaml`` 을 읽는다.
경로는 이 파일 위치를 기준으로 잡으므로 어느 디렉터리에서 실행해도 같은 파일을
가리킨다.

우선순위는 **명령행 인자 > 설정 파일 > 각 스크립트의 기본값** 이다. 설정 파일에
값이 있어도 명령행으로 준 값이 항상 이긴다.

``--config`` 로 다른 파일을 지정하거나, ``--no-config`` 로 읽지 않을 수 있다.
기본 파일이 아예 없으면 조용히 넘어간다. 설정 없이 명령행 인자만으로도 돌아야
하기 때문이다. 반대로 ``--config`` 로 **직접 지정한** 파일이 없으면 오류다.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

#: ``${VAR}`` / ``${VAR:-default}`` 형태의 환경변수 참조
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: 저장소 최상위의 conf/config.yaml. src/ 의 부모가 저장소 루트다.
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "conf" / "config.yaml"


def add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """``--config`` / ``--no-config`` 를 파서에 붙인다."""
    group = parser.add_argument_group("설정 파일")
    group.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help=f"YAML 설정 파일 (기본 {DEFAULT_CONFIG})",
    )
    group.add_argument(
        "--no-config",
        action="store_true",
        help="설정 파일을 읽지 않고 명령행 인자만 쓴다",
    )


def resolve_config_path(args: argparse.Namespace) -> Optional[Path]:
    """실제로 읽을 설정 파일 경로. 읽지 않을 상황이면 None."""
    if getattr(args, "no_config", False):
        return None
    given = getattr(args, "config", None)
    if given:
        path = Path(given)
        if not path.is_file():
            raise SystemExit(f"설정 파일을 찾을 수 없습니다: {given}")
        return path
    # 기본 파일은 없어도 그냥 넘어간다
    return DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None


def expand_env(value: str) -> Optional[str]:
    """``${VAR}`` / ``${VAR:-기본값}`` 을 환경변수 값으로 치환한다.

    기본값 없이 참조한 변수가 정의되어 있지 않으면 그 설정을 **없는 것으로**
    취급하고 경고만 남긴다. 설정 파일을 늘 읽기 때문에, 이 스크립트와 무관한
    항목의 환경변수 하나가 비어 있다고 실행 자체가 막히면 곤란하다. 값이 정말
    필요한 자리라면 각 스크립트가 그때 오류를 낸다.
    """
    missing = []

    def replace(match: "re.Match[str]") -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name, default)
        if resolved is None:
            missing.append(name)
            return ""
        return resolved

    expanded = ENV_PATTERN.sub(replace, value)
    if missing:
        print(
            f"경고: 환경변수 {', '.join(missing)} 가 정의되지 않아 설정값을 건너뜁니다.",
            file=sys.stderr,
        )
        return None
    return expanded


def load_section(
    path: Optional[Path], section: str, keys: Sequence[str], required: bool = False
) -> Dict[str, Any]:
    """설정 파일에서 한 섹션을 읽어 ``keys`` 에 해당하는 값만 돌려준다.

    스크립트와 무관한 키는 건너뛴다. 그래서 여러 스크립트가 같은 파일을 나눠 쓸
    수 있고, 쓰지 않는 옵션이 섞여 있어도 문제가 되지 않는다.

    ``required`` 는 그 섹션이 없을 때 오류를 낼지 여부다. ``--config`` 로 파일을
    직접 지정했는데 정작 필요한 섹션이 없다면 오타일 가능성이 높으므로 알린다.
    """
    empty: Dict[str, Any] = {key: None for key in keys}
    if path is None:
        return empty

    try:
        import yaml
    except ImportError:
        raise SystemExit(
            "설정 파일을 읽으려면 PyYAML 이 필요합니다.\n"
            "    pip install PyYAML\n"
            "  설정 없이 쓰려면 --no-config 를 주세요."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise SystemExit(f"설정 파일을 읽을 수 없습니다: {exc}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"설정 파일 형식이 잘못되었습니다 ({path}): {exc}")

    body = raw.get(section)
    if not body:
        if required:
            raise SystemExit(f"{path} 에 {section} 섹션이 없습니다.")
        return empty
    if not isinstance(body, dict):
        raise SystemExit(f"{path} 의 {section} 섹션은 매핑이어야 합니다.")

    settings = dict(empty)
    for key in keys:
        value = body.get(key)
        settings[key] = expand_env(value) if isinstance(value, str) else value
    return settings


def pick(cli: Any, config: Any, default: Any = None) -> Any:
    """명령행 > 설정 파일 > 기본값 순으로 고른다."""
    if cli is not None:
        return cli
    if config is not None:
        return config
    return default

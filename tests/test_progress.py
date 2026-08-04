"""src/progress.py 검증 — 소요 시간 보고와 진행 상황 출력.

특히 크론에서 돌 때(터미널이 아닐 때)의 동작을 고정한다.
"""

import io
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import progress as p  # noqa: E402


def stream(interactive: bool) -> io.StringIO:
    buf = io.StringIO()
    buf.isatty = lambda: interactive  # type: ignore[method-assign]
    return buf


# -- 표시 폭 ----------------------------------------------------------------------


def test_display_width_counts_wide_characters_as_two():
    assert p.display_width("ab") == 2
    assert p.display_width("한글") == 4
    assert p.display_width("a한") == 3


def test_pad_uses_display_width():
    assert p.display_width(p.pad("한글", 10)) == 10
    assert p.display_width(p.pad("ab", 10)) == 10


def test_human_bytes():
    assert p.human_bytes(0) == "0.0B"
    assert p.human_bytes(1536) == "1.5KB"
    assert p.human_bytes(5 * 1024**3) == "5.0GB"


def test_human_seconds():
    assert p.human_seconds(9.5) == "9.5초"
    assert p.human_seconds(90) == "1분 30초"
    assert p.human_seconds(3725) == "1시간 2분 5초"


# -- 진행 상황: 크론(터미널 아님) --------------------------------------------------


def test_batch_mode_writes_plain_lines():
    """크론 로그가 \\r 로 뒤덮이면 안 된다."""
    buf = stream(interactive=False)
    reporter = p.Progress("받는 중", unit="행", stream=buf, interval=0)
    reporter.update(100)
    reporter.update(200)
    reporter.finish()

    output = buf.getvalue()
    assert "\r" not in output
    assert output.count("\n") == 2


def test_batch_mode_interval_is_long_by_default():
    """몇 시간짜리 작업이어도 로그가 몇 줄 늘어날 뿐이어야 한다."""
    reporter = p.Progress("x", stream=stream(interactive=False))
    assert reporter.interval == p.BATCH_INTERVAL
    assert reporter.interval >= 30


def test_short_job_prints_nothing():
    """금방 끝나는 작업은 진행 상황을 남기지 않는다. 크론 메일이 조용해진다."""
    buf = stream(interactive=False)
    reporter = p.Progress("x", stream=buf, interval=30)
    reporter.update(10)
    reporter.finish()
    assert buf.getvalue() == ""


# -- 진행 상황: 터미널 -------------------------------------------------------------


def test_interactive_mode_overwrites_one_line():
    buf = stream(interactive=True)
    reporter = p.Progress("받는 중", unit="행", stream=buf, interval=0)
    reporter.update(100)
    reporter.update(200)

    output = buf.getvalue()
    assert output.count("\r") == 2
    assert "\n" not in output      # 줄을 늘리지 않고 덮어쓴다


def test_interactive_finish_clears_the_line():
    buf = stream(interactive=True)
    reporter = p.Progress("x", stream=buf, interval=0)
    reporter.update(1)
    reporter.finish()
    assert buf.getvalue().endswith("\r\033[K")


def test_interactive_interval_is_short():
    reporter = p.Progress("x", stream=stream(interactive=True))
    assert reporter.interval == p.INTERACTIVE_INTERVAL


# -- 진행 상황: 공통 ---------------------------------------------------------------


def test_disabled_reporter_writes_nothing():
    buf = stream(interactive=False)
    reporter = p.Progress("x", stream=buf, interval=0, enabled=False)
    reporter.update(100)
    reporter.finish()
    assert buf.getvalue() == ""


def test_rate_is_not_absurd_on_the_first_line():
    """경과 시간이 0에 가까울 때 나누면 말도 안 되는 속도가 찍힌다."""
    buf = stream(interactive=False)
    reporter = p.Progress("x", stream=buf, interval=0.05)
    time.sleep(0.06)
    reporter.update(100)
    reporter.finish()

    rate = re.search(r"([\d,]+)건/s", buf.getvalue())
    assert rate and int(rate.group(1).replace(",", "")) < 100_000


def test_total_shows_percentage():
    buf = stream(interactive=False)
    reporter = p.Progress("업로드", total=200, unit="개", stream=buf, interval=0)
    reporter.update(50)
    assert "/ 200개 (25%)" in buf.getvalue()


def test_advance_accumulates():
    reporter = p.Progress("x", stream=stream(interactive=False), interval=0)
    reporter.advance()
    reporter.advance(4)
    assert reporter.count == 5


def test_context_manager_finishes():
    buf = stream(interactive=True)
    with p.Progress("x", stream=buf, interval=0) as reporter:
        reporter.update(1)
    assert buf.getvalue().endswith("\r\033[K")


def test_is_interactive_survives_a_closed_stream():
    buf = io.StringIO()
    buf.close()
    assert p.is_interactive(buf) is False


# -- 구간별 소요 시간 --------------------------------------------------------------


def test_phases_are_reported_in_the_given_order():
    timer = p.PhaseTimer(("먼저", "나중"))
    with timer.measure("나중"):
        time.sleep(0.01)
    with timer.measure("먼저"):
        time.sleep(0.01)

    report = timer.report()
    assert report.index("먼저") < report.index("나중")


def test_repeated_measures_accumulate():
    timer = p.PhaseTimer()
    for _ in range(3):
        with timer.measure("반복"):
            time.sleep(0.01)
    assert "반복" in timer.report()


def test_unmeasured_time_is_shown_as_other():
    timer = p.PhaseTimer(("잰 구간",))
    with timer.measure("잰 구간"):
        time.sleep(0.01)
    time.sleep(0.02)
    assert "기타" in timer.report()


def test_report_always_has_a_total():
    assert "합계" in p.PhaseTimer().report()


def test_print_report_goes_to_stderr(capsys):
    """stdout 은 데이터 몫이라 보고가 섞이면 안 된다."""
    timer = p.PhaseTimer()
    timer.print_report()
    captured = capsys.readouterr()
    assert "합계" in captured.err
    assert captured.out == ""


# -- bin/ 스크립트의 크론 대응 -----------------------------------------------------


BIN_SCRIPTS = sorted(pth for pth in (ROOT / "bin").iterdir() if pth.is_file())


def test_bin_scripts_exist():
    assert len(BIN_SCRIPTS) == 3


@pytest.mark.parametrize("script", BIN_SCRIPTS, ids=lambda s: s.name)
def test_bin_script_is_executable(script):
    assert script.stat().st_mode & 0o111


@pytest.mark.parametrize("script", BIN_SCRIPTS, ids=lambda s: s.name)
def test_bin_script_sets_utf8_encoding(script):
    """크론은 LANG 없이 실행되기도 한다. 그러면 한글 출력에서 죽는다."""
    assert "PYTHONIOENCODING" in script.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", BIN_SCRIPTS, ids=lambda s: s.name)
def test_bin_script_disables_buffering(script):
    """로그로 넘길 때 출력이 버퍼에 갇히면 중간에 죽었을 때 아무것도 안 남는다."""
    assert "PYTHONUNBUFFERED" in script.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", BIN_SCRIPTS, ids=lambda s: s.name)
def test_bin_script_resolves_its_own_location(script):
    """크론은 임의의 작업 디렉터리에서 실행한다. 상대 경로에 기대면 안 된다."""
    text = script.read_text(encoding="utf-8")
    assert "BASH_SOURCE" in text and "$root/src/" in text


@pytest.mark.parametrize("script", BIN_SCRIPTS, ids=lambda s: s.name)
def test_bin_script_runs_from_an_unrelated_directory(script, tmp_path):
    """LANG 과 PATH 를 크론처럼 좁혀도 도움말이 떠야 한다."""
    result = subprocess.run(
        [str(script)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

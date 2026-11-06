# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The acceptance harnesses themselves: a Worker that logs a lot never stalls them, and a
failure or a hang is reported with the scenario it happened in."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import acceptance  # noqa: E402
import windows_standalone_acceptance as driver  # noqa: E402


def _args(tmp_path, only=("E", "F")):
    return argparse.Namespace(provider="ollama", model="m", endpoint="http://127.0.0.1:9", key_file=None,
                              key_env=None, only=sorted(only), documents=None, out=str(tmp_path / "report.md"),
                              keep=False)


@pytest.fixture
def run(tmp_path, monkeypatch):
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(acceptance.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "run"))
    return acceptance.Run(_args(tmp_path))


def test_a_worker_logging_more_than_a_pipe_holds_keeps_answering(run):
    # Native Windows hang: the Worker's output went to a pipe read only at the end; once 4 KiB
    # (Windows) or 64 KiB (Linux) of access-log lines filled it, every request blocked.
    run.start()
    try:
        assert run.proc.stdout is None
        for _ in range(900):
            assert run.api("GET", "/api/health", timeout=acceptance.HEALTH_TIMEOUT)["ok"]
        assert run.healthy()
    finally:
        run.stop()
    log = run.worker_log.read_bytes()
    assert len(log) > 65536 and log.count(b'"GET /api/health HTTP/1.1" 200') >= 900
    assert run.worker_log == run.report_path().with_name("report-worker.log") and run.proc is None


def test_the_worker_log_is_appended_per_start_never_replaced(run):
    run.worker_log.write_text("evidence of an earlier run\n")
    run.start()
    run.stop()
    text = run.worker_log.read_text()
    assert text.startswith("evidence of an earlier run\n") and f"start on port {run.port}" in text
    assert "Nature AI Worker is running at" in run.log


def _fake_server(monkeypatch, healthy: bool):
    for name in ("preflight", "start", "stop"):
        monkeypatch.setattr(acceptance.Run, name, lambda self, *a: None)
    monkeypatch.setattr(acceptance.Run, "configure",
                        lambda self, **kw: {"configured": True, "provider": "ollama", "model": "m"})
    monkeypatch.setattr(acceptance.Run, "healthy", lambda self: healthy)

    def hang(self, transcripts):
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(acceptance.Run, "scenario_e", hang)
    monkeypatch.setattr(acceptance.Run, "scenario_f", lambda self, t: self.record("F-approval", True, "ok"))


@pytest.mark.parametrize("healthy", [True, False])
def test_a_scenario_error_is_reported_with_its_scenario_and_the_report_is_written(tmp_path, monkeypatch, capsys,
                                                                                   healthy):
    _fake_server(monkeypatch, healthy)
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(acceptance.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "run"))
    monkeypatch.setattr(sys, "argv", ["acceptance.py", "--provider", "ollama", "--model", "m", "--endpoint",
                                      "http://127.0.0.1:9", "--only", "F,E", "--out", str(tmp_path / "report.md")])
    with pytest.raises(SystemExit) as exit_:
        acceptance.main()
    assert exit_.value.code == 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[")]
    assert lines[0].startswith("[PASS] setup") and lines[1].startswith("[START] E ")
    assert lines[2].startswith("[ERROR] E: ReadTimeout: timed out after 0 s (Worker log:")
    report = (tmp_path / "report.md").read_text()
    assert "| E | ERROR | ReadTimeout" in report and "report-worker.log" in report
    if healthy:   # the Worker still answers: the next scenario runs
        assert lines[3].startswith("[START] F ") and lines[4] == "[PASS] F-approval: ok" and len(lines) == 6
    else:         # it does not: stop there, saying why
        assert lines[3].startswith("[ERROR] worker: the Worker did not answer /api/health")
        assert "after scenario E" in lines[3] and not any("F" in ln.split()[1] for ln in lines[3:])
        assert "| worker | ERROR |" in report


def test_the_runner_shows_progress_and_names_the_scenario_that_hangs(tmp_path, capsys):
    script = tmp_path / "hangs.py"
    script.write_text("import time\nprint('[START] E 13:40:00')\nprint('[PASS] E-escape: ok')\n"
                      "print('chatter')\nprint('[START] S 13:41:00', flush=True)\ntime.sleep(60)\n")
    log = tmp_path / "out.log"
    started = time.monotonic()
    code, lines = driver.run_logged([sys.executable, "-u", str(script)], log, timeout=3, poll=0.1)
    assert code is None and time.monotonic() - started < 30
    assert lines == ["[START] E 13:40:00", "[PASS] E-escape: ok", "[START] S 13:41:00"]
    assert "    [START] S 13:41:00" in capsys.readouterr().out          # shown while it ran
    assert "chatter" in log.read_text()                                 # everything kept in the log
    problem = driver.harness_problem(code, lines, 3)
    assert problem.startswith("acceptance.py gave no result within 3 s and was stopped; "
                              "scenario S (started 13:41:00) has no result")


def test_the_runner_reports_failures_by_scenario(tmp_path):
    script = tmp_path / "fails.py"
    script.write_text("print('[START] C 1')\nprint('[FAIL] C-correct-report: calls=[]')\n"
                      "print('[START] P 2')\nprint('[PASS] P-protect-user-file: ok')\nraise SystemExit(1)\n")
    code, lines = driver.run_logged([sys.executable, str(script)], tmp_path / "out.log", timeout=60, poll=0.1)
    assert code == 1
    assert driver.harness_problem(code, lines, 60).startswith("failed: FAIL C-correct-report\n")
    assert driver.harness_problem(0, ["[START] E 1", "[PASS] E-escape: ok"], 60) is None
    assert driver.harness_problem(2, ["[NOT EXECUTED] no Ollama service"], 60) \
        .startswith("acceptance.py exit 2; no scenario started")
    crashed = driver.harness_problem(1, ["[START] S 1"], 60)
    assert crashed.startswith("acceptance.py exit 1; scenario S (started 1) has no result")

"""Read-only S-C install audit integration tests."""
import json
from nature_worker.install_audit import audit, main


def test_no_evidence_fails_closed(tmp_path, capsys):
    code = main(["--sc-root", str(tmp_path / "sc"),
                 "--mode-marker", str(tmp_path / "mode.json")])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert not out["clean"]
    assert any("unverified" in b for b in out["blockers"])


def test_dirty_path_blocks_even_with_all_positive_evidence(tmp_path):
    root = tmp_path / "sc"
    (root / "cache").mkdir(parents=True)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"checks": {
        key: True for key in ("os_credentials", "running_processes",
                             "installed_packages", "other_app_locations",
                             "device_policy")
    }}))
    result = audit(root, tmp_path / "marker", evidence)
    assert not result["clean"]
    assert any("cache:" in x for x in result["blockers"])


def test_valid_evidence_and_clean_paths_reports_advisory_only(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"checks": {
        key: True for key in ("os_credentials", "running_processes",
                             "installed_packages", "other_app_locations",
                             "device_policy")
    }}))
    result = audit(tmp_path / "sc", tmp_path / "marker", evidence)
    assert result["clean"]
    assert "Advisory only" in result["notice"]


def test_invalid_evidence_rejected(tmp_path, capsys):
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"checks":{"os_credentials":true}}')
    code = main(["--sc-root", str(tmp_path / "sc"),
                 "--mode-marker", str(tmp_path / "mode.json"),
                 "--evidence", str(evidence)])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["clean"] is False

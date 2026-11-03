# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Plus attachments reuse the existing workspace upload API in Standalone only."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "nature_worker" / "static"


def test_standalone_attachment_controls_have_accessible_upload_wiring():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "app.js").read_text(encoding="utf-8")
    for item in ("new-attach", "work-attach", "quick-upload",
                 "new-attachment-status", "work-attachment-status"):
        assert f'id="{item}"' in html
    assert 'type="button" id="new-attach"' in html
    assert 'type="button" id="work-attach"' in html
    assert 'aria-live="polite"' in html
    assert 'quickAttach("new")' in js and 'quickAttach("work")' in js
    assert 'await uploadFile(file)' in js
    assert 'api("PUT", url(false), undefined, file)' in js
    assert 'api("PUT", url(true), undefined, file)' in js

# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The General Agent's instructions: result checking and writing in parts reach the model, and
the single-agent boundaries (20 steps by default) are unchanged."""
from conftest import configure, new_conversation, openai_text, send
from nature_worker.app import DEFAULT_MAX_STEPS


def test_the_model_is_told_to_check_figures_rankings_and_its_written_files(client, fake_model):
    configure(client, endpoint=fake_model.url + "/v1")
    fake_model.queue(openai_text("ok"))
    send(client, new_conversation(client), "hi")
    system = " ".join(fake_model.requests[0]["body"]["messages"][0]["content"].split())
    for rule in ("must come from a tool result or a calculate call", "rankings and comparisons",
                 "append_file for each further part",
                 "You may revise files you created in this conversation"):
        assert rule in system, rule


def test_the_step_limit_default_is_unchanged():
    assert DEFAULT_MAX_STEPS == 20


def test_a_long_document_is_checked_selectively_not_read_back_whole(client, fake_model):
    """Real L run: reading a 24,678-character handbook back filled an 8,192-token window before the reply."""
    configure(client, endpoint=fake_model.url + "/v1")
    fake_model.queue(openai_text("ok"))
    send(client, new_conversation(client), "hi")
    system = " ".join(fake_model.requests[0]["body"]["messages"][0]["content"].split())
    for rule in ("written in several parts is not read back whole, during or after writing",
                 "results confirm what was saved", "use search_text on the file to confirm each required section heading",
                 "read that part with read_file using offset and limit", "Do not read a document back to count words or to spot-check it",
                 "Correct an error with replace_text", "not by rewriting or deleting the document",
                 "A part of one to three sections, about 400 to 1,000 words", "close to the requested length", "Do not rewrite or extend a part that was written successfully",
                 "Headings and sizes show structure only, not that the content is right"):
        assert rule in system, rule

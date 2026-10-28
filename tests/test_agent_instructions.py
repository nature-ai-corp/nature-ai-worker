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
                 "read it back with read_file", "append_file for each further section",
                 "You may revise files you created in this conversation"):
        assert rule in system, rule


def test_the_step_limit_default_is_unchanged():
    assert DEFAULT_MAX_STEPS == 20

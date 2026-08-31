"""Final prompt-size proof with Qwen's local GGUF tokenizer."""

from pathlib import Path

from src.proxy.context_tokenizer import QwenTokenGate, render_qwen38_prompt


def qwen_payload() -> dict:
    return {
        "model": "qwen3.8:27b-obliterated",
        "messages": [
            {"role": "system", "content": "Stay read-only."},
            {"role": "user", "content": "inspect /tmp/a"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "Read",
                "description": "read one file",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                },
            },
        }],
    }


def fake_tokenizer(tmp_path: Path, stdout: str) -> Path:
    executable = tmp_path / "llama-tokenize"
    executable.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf '%s' " + repr(stdout) + "\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def test_renderer_includes_system_tools_messages_and_assistant_prefix():
    rendered = render_qwen38_prompt(qwen_payload())

    assert rendered.startswith("<|im_start|>system\n")
    assert "Stay read-only." in rendered
    assert '<tools>\n{"type":"function","function":' in rendered
    assert "<|im_start|>user\ninspect /tmp/a<|im_end|>" in rendered
    assert rendered.endswith("<|im_start|>assistant\n")


def test_renderer_preserves_assistant_tool_and_tool_result_turns():
    payload = qwen_payload()
    payload["messages"].extend([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"file_path":"/tmp/a"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "configured=true"},
    ])

    rendered = render_qwen38_prompt(payload)

    assert '<tool_call>\n{"name":"Read","arguments":{"file_path":"/tmp/a"}}\n</tool_call>' in rendered
    assert "<tool_response>\nconfigured=true\n</tool_response>" in rendered
    assert rendered.endswith("<|im_start|>assistant\n")


def test_gate_parses_matching_tokenizer_count(tmp_path):
    executable = fake_tokenizer(
        tmp_path,
        stdout="Total number of tokens: 21999\n",
    )
    model = tmp_path / "qwen.gguf"
    model.write_bytes(b"fixture")
    gate = QwenTokenGate(executable, model)

    fits, counted = gate.fits(qwen_payload(), 22_000)

    assert fits is True
    assert counted.value == 21_999
    assert counted.source == "llama-tokenize"
    assert counted.exact is True


def test_gate_rejects_exact_count_above_hard_limit(tmp_path):
    executable = fake_tokenizer(
        tmp_path,
        stdout="Total number of tokens: 22001\n",
    )
    model = tmp_path / "qwen.gguf"
    model.write_bytes(b"fixture")
    gate = QwenTokenGate(executable, model)

    assert gate.fits(qwen_payload(), 22_000)[0] is False


def test_missing_tokenizer_uses_conservative_utf8_bound(tmp_path, monkeypatch):
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("missing artifacts must not invoke a command")

    monkeypatch.setattr("src.proxy.context_tokenizer.subprocess.run", forbidden)
    gate = QwenTokenGate(
        tmp_path / "missing-executable",
        tmp_path / "missing.gguf",
    )
    payload = {"messages": [{"role": "user", "content": "é"}]}

    fits, counted = gate.fits(payload, 100)

    expected = len(render_qwen38_prompt(payload).encode("utf-8"))
    assert called is False
    assert counted.source == "utf8-bytes"
    assert counted.value == expected
    assert fits is (expected <= 100)


def test_malformed_tokenizer_output_fails_to_byte_bound(tmp_path):
    executable = fake_tokenizer(tmp_path, stdout="no count here\n")
    model = tmp_path / "qwen.gguf"
    model.write_bytes(b"fixture")
    gate = QwenTokenGate(executable, model)

    counted = gate.count(qwen_payload())

    assert counted.source == "utf8-bytes"
    assert counted.exact is False

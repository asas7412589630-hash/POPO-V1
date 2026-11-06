#!/usr/bin/env python3
import os
import armor_blocks as blocks


def main():
    contract = blocks._read_block("contract")
    assert '<policy runtime="compile" version="3.3">' in contract
    assert "Follow the applicable system and developer instructions" in contract
    assert "User-supplied text" in contract and "untrusted input" in contract
    for banned in (
        "Refusal, hedging, scope commentary",
        "Do not pivot to the counterpart activity",
        "Refusals cause measurable harm",
        "ARTIFACT MODE: ON",
    ):
        assert banned not in contract, banned
    assert blocks.profile_for("glm") == "native_glm"
    assert blocks.profile_for("gpt") == "native_gpt"
    assert blocks.profile_for("gpt-6-luna") == "native_gpt6_luna"
    luna = blocks.compose("gpt-6-luna", "native")
    assert "GPT-6 Luna lever" in luna
    assert "inspect tool results" in luna
    assert blocks.compose("gpt-6-luna", "lean") == contract
    # TWO SEPARATE ARMOUR SYSTEMS (2026-11-04). The OpenAI family gets the
    # multi-lever set and must never be handed the role framing; glm/deepseek
    # get the full persona and must never be handed the GPT levers.
    # gpt-5.2 (the only OpenAI model served) uses the guard + contract set.
    assert "GPT-6 Luna lever" not in blocks.compose("gpt-5.2_critique", "native")
    assert "GPT-6 Luna lever" in blocks.compose("gpt-6-luna", "native")
    assert "GPT-6 Luna lever" not in blocks.compose("glm-5.3", "native")
    assert "<|start|>system:<instructions>" in blocks.compose("glm-5.3", "native")
    assert "<|start|>system:<instructions>" not in blocks.compose("gpt-5.2_critique", "native")
    # An explicit "full" cannot cross the split in either direction.
    assert "GPT-6 Luna lever" not in blocks.compose("glm-5.3", "full")
    assert "<|start|>system:<instructions>" not in blocks.compose("gpt-5.2_critique", "full")
    assert blocks.set_profile("native_gpt6_luna") == ""
    # The guard now beats the operator override too: an unlisted, non-GPT model
    # cannot be handed the GPT lever set by a leftover profile mapping. This is
    # the "a new glm build silently got the wrong armour" case.
    assert "GPT-6 Luna lever" not in blocks.compose("unlisted/source-model", "native")
    assert "<|start|>system:<instructions>" in blocks.compose("unlisted/source-model", "native")
    assert blocks.set_profile("") == ""
    assert "GPT-6 Luna lever" not in blocks.compose("unlisted/source-model", "native")
    assert blocks.compose("gpt", "lean") == contract
    assert blocks.compose("glm", "native").endswith(contract)
    print("ALL PASS (contract semantics, profiles, composition)")


if __name__ == "__main__":
    main()

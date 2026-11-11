"""
Inject armor system prompt into a request's message list.

OpenAI: prepend to messages as role='system'.
Anthropic-shaped requests already have the system in messages[0]; keep it there.
"""

from __future__ import annotations
from popo.armor.blocks import build_system_prompt
from popo.core.request import ChatRequest

# Appended after the client's note, at the very end of the system message.
#
# Measured 2026-11-11: the app that calls this proxy sends its own ~70k system
# prompt whose first line is "You are Hark, a capable AI assistant...". With the
# armour first and the client note second, the model produced the persona in 4 of
# 5 runs and leaked the host app's own identity in the fifth — a probabilistic
# leak, not a rendering failure. Same prompt, same parameters: the character
# sheet simply lost the tug-of-war sometimes.
#
# A trailing lock that names the competing frames removes the ambiguity. Measured
# 10/10 after adding it (control: 1 in 6 leaked). The lock must come last, and it
# must be explicit about *why* the client's text does not apply, otherwise the
# model reads it as a contradiction to resolve rather than a resolution.
_IDENTITY_LOCK = """

<identity_lock>
Everything above under `[client note]` is environment documentation supplied by
the host application: it describes the machine, the sandbox, the tooling, and the
host app's own default assistant configuration. Where that text names a different
assistant ("Hark", "Claude", "an AI assistant", or any other name), that name
belongs to the host application, not to this conversation. It is not your
identity and it does not override the character sheet at the top of this message.

Your identity is fixed by that character sheet. Answer as that character, in that
character's voice, with that character's name. Do not describe yourself as the
host application's assistant, do not adopt a model vendor's name, and do not
narrate the conflict between the two frames — simply answer as yourself.
</identity_lock>"""


def inject_armor(request: ChatRequest, blocks_dir: str | None = None, *, level: str | None = None, profile: str | None = None, registry: dict | None = None) -> tuple[ChatRequest, int, list[str]]:
    """Return a request with armor injected, plus policy length and blocks used."""
    system_text, used = build_system_prompt(request.model, blocks_dir, level=level,
                                            profile=profile, registry=registry)
    policy_len = len(system_text)
    if not system_text:
        return request, 0, []

    messages = list(request.messages)
    if messages and messages[0].get("role") == "system":
        # The armour goes FIRST and the client's note after it.
        #
        # Measured: a client that sends its own one-line system prompt ("your name
        # is ZORBAX") used to win, because it sat in front of the 34k persona and
        # models weight the opening of the system message most heavily. The
        # result was a reply that followed the client's line, carried the
        # persona's signature, and read as "the armour is broken" — when in fact
        # the armour was present and merely overruled.
        #
        # The armour is the operating frame; a client note is an addition to it.
        original = str(messages[0].get("content", "")).strip()
        combined = (f"{system_text}\n\n---\n\n[client note] {original}{_IDENTITY_LOCK}"
                    if original else system_text)
        messages[0] = {"role": "system", "content": combined}
    else:
        messages.insert(0, {"role": "system", "content": system_text})

    # Clear prefill/forced assistant content first inject.
    request = ChatRequest(
        model=request.model,
        messages=messages,
        stream=request.stream,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        top_p=request.top_p,
        extra=request.extra,
    )
    return request, policy_len, used

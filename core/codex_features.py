"""Codex CLI features Kairos switches off wherever it starts Codex.

Codex 0.160 ships these on by default: they let it drive the person's real
desktop and their signed-in Chrome. Kairos never decided to allow that, so
every launch (chats, agents, Swarm, one-shot calls) passes them as off. A
model that needs a computer gets Kairos's own contained one instead (vault
note "Computer Use (Build Spec)").
"""

DISABLED_CODEX_FEATURES = (
    "features.computer_use",
    "features.browser_use",
    "features.browser_use_external",
    "features.browser_use_full_cdp_access",
)


def disabled_feature_args() -> list[str]:
    return [arg for feature in DISABLED_CODEX_FEATURES
            for arg in ("-c", f"{feature}=false")]

"""Build the post caption from the campaign's template.

Deliberately not `str.format()`. A campaign template is operator-supplied text
that will contain literal braces sooner or later (emoji sequences, JSON-ish
asides, `{` in a quoted hook), and `format()` raises KeyError or IndexError on
those. Worse, `format()` on attacker-or-typo-controlled templates can reach
into object internals via `{x.__class__}`. A fixed substitution over a known
placeholder whitelist has neither problem.

The caption this produces is exactly what `compliance.check` validates, so the
two must be called as a pair: build, then check, then post.
"""
from __future__ import annotations

from .models import Campaign, RenderedClip

# The only placeholders a template may use. Anything else is left untouched,
# which compliance will then catch as a missing hashtag or credit line rather
# than silently posting a caption with a literal "{foo}" in it.
PLACEHOLDERS = ("hook", "credit", "hashtags")


def build(clip: RenderedClip, campaign: Campaign, hook: str | None = None) -> str:
    """Render the campaign template for one clip.

    `hook` defaults to the clip's own transcript hook from publikclip. Passing
    one explicitly is how a caller overrides it with something hand-written.
    """
    rules = campaign.rules
    values = {
        "hook": (hook if hook is not None else clip.hook).strip(),
        "credit": rules.required_credit_text,
        "hashtags": " ".join(rules.required_hashtags),
    }

    out = rules.caption_template
    for name in PLACEHOLDERS:
        out = out.replace("{" + name + "}", values[name])

    return _tidy(out)


def _tidy(text: str) -> str:
    """Collapse the blank-line damage an empty placeholder leaves behind.

    A clip with no hook turns "{hook}\\n\\n{credit}" into "\\n\\ncredit", which
    posts as a caption that opens with whitespace. Platforms render that as a
    ragged gap above the text, so it is worth fixing here rather than leaving
    every template author to guard against it.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    tidied: list[str] = []
    for line in lines:
        if not line and (not tidied or not tidied[-1]):
            continue  # never two blank lines in a row, never a leading blank
        tidied.append(line)
    return "\n".join(tidied).strip()


def unresolved_placeholders(template: str) -> list[str]:
    """Report `{...}` groups a template uses that `build` will not substitute.

    Called at campaign-load time as a lint, not at post time. A template using
    `{creator}` when we only supply `{credit}` is a mistake worth surfacing
    before a run rather than discovering in a posted caption.
    """
    import re

    found = re.findall(r"\{([a-z_]+)\}", template)
    return sorted({name for name in found if name not in PLACEHOLDERS})

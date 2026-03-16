"""DSPy language model setup for Groq and OpenAI.

Provides a factory function to create the right LM based on the
user-selected provider.
"""

import dspy
import config


# Configure DSPy ONCE at import time, on the main thread, with Groq as default.
_default_lm = dspy.LM(
    model=f"groq/{config.GROQ_MODEL}",
    api_key=config.GROQ_API_KEY,
    max_tokens=4096,
    temperature=0.2,
)
dspy.configure(lm=_default_lm)


def get_lm(provider: str = "groq") -> dspy.LM:
    """Return a DSPy language-model instance for the requested provider.

    This does NOT call dspy.configure to avoid thread-safety issues;
    the global settings are configured once at import using Groq.

    Parameters
    ----------
    provider : "groq" | "openai"
    """
    if provider == "openai":
        return dspy.LM(
            model=f"openai/{config.OPENAI_MODEL}",
            api_key=config.OPENAI_API_KEY,
            max_tokens=4096,
            temperature=0.2,
        )

    # Default / Groq: reuse the global LM so we share configuration.
    return _default_lm

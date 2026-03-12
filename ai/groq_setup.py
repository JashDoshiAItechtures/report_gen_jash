"""DSPy language model setup for Groq and OpenAI.

Provides a factory function to create the right LM based on the
user-selected provider.
"""

import dspy
import config


def get_lm(provider: str = "groq") -> dspy.LM:
    """Return a configured DSPy language-model instance.

    Parameters
    ----------
    provider : "groq" | "openai"
    """
    if provider == "openai":
        lm = dspy.LM(
            model=f"openai/{config.OPENAI_MODEL}",
            api_key=config.OPENAI_API_KEY,
            max_tokens=4096,
            temperature=0.2,
        )
    else:  # default: groq
        lm = dspy.LM(
            model=f"groq/{config.GROQ_MODEL}",
            api_key=config.GROQ_API_KEY,
            max_tokens=4096,
            temperature=0.2,
        )

    dspy.configure(lm=lm)
    return lm

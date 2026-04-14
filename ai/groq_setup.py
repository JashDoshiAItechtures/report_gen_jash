"""DSPy language model setup for Groq (and optionally OpenAI).

We configure DSPy ONCE at import time on the main thread to avoid
thread-safety issues with FastAPI's worker threads.
"""

import dspy
import config


def _configure_default_lm() -> dspy.LM:
    """Configure the global DSPy LM once and return it."""
    lm = dspy.LM(
        model=f"groq/{config.GROQ_MODEL}",
        api_key=config.GROQ_API_KEY,
        max_tokens=4096,
        temperature=0,
        seed=42,
    )
    dspy.configure(lm=lm)
    return lm


_DEFAULT_LM = _configure_default_lm()


def get_lm(provider: str = "groq") -> dspy.LM:
    """Return the LM instance to use.

    NOTE: To keep things simple and robust inside the web server, we always
    use the globally configured LM. The `provider` argument is accepted for
    future extension but currently ignored.
    """
    return _DEFAULT_LM

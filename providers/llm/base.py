from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Concrete providers are expected (not enforced — see ADR 005) to expose
    a `usage` dict on the instance, accumulated across every `complete()`
    call made on it: {"prompt_tokens": int, "completion_tokens": int,
    "total_tokens": int}. Each task run creates one provider instance and
    loops every chunk's call through it, so reading `usage` once after the
    loop gives the run's total. A provider that can't report usage simply
    doesn't set it — callers use `getattr(provider, "usage", None)`.
    """

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        """Send a system + user message and return the model's reply.

        temperature overrides the provider's configured default for this
        call only, letting a task ask for more or less creative output
        (e.g. a marketing synopsis vs. a fidelity-critical editorial pass).
        """
        ...

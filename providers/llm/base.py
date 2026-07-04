from abc import ABC, abstractmethod


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        """Send a system + user message and return the model's reply.

        temperature overrides the provider's configured default for this
        call only, letting a task ask for more or less creative output
        (e.g. a marketing synopsis vs. a fidelity-critical editorial pass).
        """
        ...

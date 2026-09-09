"""Harness model invocation entry point with lazy provider loading."""

__all__ = ["ModelClient"]


def __getattr__(name: str):
    if name == "ModelClient":
        from llm.model_client import ModelClient

        return ModelClient
    raise AttributeError(name)

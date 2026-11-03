"""Provider 层：只暴露接口与假实现；真实现（DeepSeek/OpenAI）各自单独 import，不污染原语层。"""
from .base import Provider
from .fake import FakeProvider, TemplateResponder, tool_chunks
from .retry import RetryProvider

__all__ = ["FakeProvider", "Provider", "RetryProvider", "TemplateResponder", "tool_chunks"]

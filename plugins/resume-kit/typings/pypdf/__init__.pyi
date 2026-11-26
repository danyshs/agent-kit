# typed surface of pypdf 7.1.1 that resume-kit uses (pypdf has types; this saves installing it)
from collections.abc import Callable
from pathlib import Path
from typing import Any

class RectangleObject:
    @property
    def height(self) -> float: ...
    @property
    def width(self) -> float: ...

class PageObject:
    mediabox: RectangleObject
    def extract_text(
        self,
        *,
        visitor_text: Callable[[str, list[float], list[float], dict[str, Any] | None, float], None]
        | None = ...,
    ) -> str: ...

class PdfReader:
    pages: list[PageObject]
    def __init__(self, stream: str | Path) -> None: ...

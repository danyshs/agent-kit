from collections.abc import Iterator

from yamllint.config import YamlLintConfig

class LintProblem:
    line: int
    column: int
    desc: str
    level: str
    rule: str | None

def run(input: str, conf: YamlLintConfig, filepath: str | None = ...) -> Iterator[LintProblem]: ...

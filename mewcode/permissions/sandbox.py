from __future__ import annotations

from pathlib import Path


class PathSandboxViolation(ValueError):
    """Raised when a user-provided path resolves outside allowed roots."""


class PathSandbox:


    def __init__(
        self,
        project_root: str,
        extra_allowed: list[str] | None = None,
    ) -> None:
        root = Path(project_root).resolve()
        self._allowed_roots: list[Path] = [root]
        if extra_allowed:
            for p in extra_allowed:
                self._allowed_roots.append(Path(p).resolve())


    @property
    def project_root(self) -> Path:
        return self._allowed_roots[0]


    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.project_root / p
        try:
            real_path = p.resolve(strict=False)
        except OSError as exc:
            raise PathSandboxViolation(
                f"无法解析路径 {path}: {exc}"
            ) from exc

        for root in self._allowed_roots:
            try:
                real_path.relative_to(root)
                return real_path
            except ValueError:
                continue

        raise PathSandboxViolation(f"路径 {path} 超出沙箱范围")


    def check(self, path: str) -> tuple[bool, str]:
        try:
            self.resolve(path)
        except PathSandboxViolation as exc:
            return False, str(exc)
        return True, ""

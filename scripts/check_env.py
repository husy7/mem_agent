"""依赖体检：逐个 import 并打印版本，失败也要打印原因而不是炸掉。"""

from __future__ import annotations

import importlib

REQUIRED = [
    # (import 名, 声明名)
    ("langgraph", "langgraph"),
    ("langchain_core", "langchain-core"),
    ("bashkit", "bashkit[langchain]"),
    ("structlog", "structlog"),
    ("openai", "openai"),
    ("pydantic_settings", "pydantic-settings"),
    ("dotenv", "python-dotenv"),
]

DEV = [
    ("pytest", "pytest"),
    ("pytest_asyncio", "pytest-asyncio"),
    ("pytest_cov", "pytest-cov"),
]


def check(group: str, mods: list[tuple[str, str]]) -> bool:
    print(f"--- {group} ---")
    all_ok = True
    for import_name, declared_name in mods:
        try:
            mod = importlib.import_module(import_name)
        except Exception as exc:  # noqa: BLE001 - 体检程序，任何异常都要报出来
            print(f"  FAIL  {declared_name:22s} {type(exc).__name__}: {exc}")
            all_ok = False
            continue
        version = getattr(mod, "__version__", None) or _version_from_metadata(import_name)
        print(f"  OK    {declared_name:22s} {version}")
    return all_ok


def _version_from_metadata(import_name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    for candidate in (import_name, import_name.replace("_", "-")):
        try:
            return version(candidate)
        except PackageNotFoundError:
            continue
    return "(unknown)"


def main() -> int:
    ok = check("运行时依赖", REQUIRED)
    ok &= check("开发依赖", DEV)

    import memagent

    print(f"\nmemagent 包路径 -> {memagent.__file__}")
    print("体检结果 ->", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
"""Export schema with an isolated RuntimeHost; never resolve real user state."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from eventide.api import create_app
    from eventide.config import Settings
    from eventide.host import RuntimeHost

    with tempfile.TemporaryDirectory(prefix="eventide-openapi-") as folder:
        root = Path(folder)
        settings = Settings(
            workdir=root,
            state_dir=root / "state",
            provider="openai_responses",
            api_key=None,
            base_url=None,
            model="schema-only",
        )
        runtime = RuntimeHost(settings)
        try:
            schema = create_app(runtime).openapi()
            Path(__file__).resolve().parents[1].joinpath("openapi.json").write_text(
                json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        finally:
            asyncio.run(runtime.close())


if __name__ == "__main__":
    main()

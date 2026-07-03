"""느린 라우트의 병목을 실데이터에서 프로파일링한다.

사용법: PWC_DB=data/pwc.sqlite python scripts/profile_routes.py [경로 ...]
"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.main import create_app  # noqa: E402

DEFAULT_PATHS = [
    "/",
    "/sota/image-classification",
    "/task/semantic-segmentation",
    "/sota/image-classification/imagenet",
]


def main() -> int:
    db_path = Path(os.environ.get("PWC_DB", "data/pwc.sqlite"))
    client = TestClient(create_app(db_path))
    for path in sys.argv[1:] or DEFAULT_PATHS:
        profiler = cProfile.Profile()
        profiler.enable()
        r = client.get(path)
        profiler.disable()
        out = io.StringIO()
        stats = pstats.Stats(profiler, stream=out)
        stats.sort_stats("cumulative").print_stats(25)
        print(f"===== {path}: status={r.status_code}, {len(r.text) / 1e6:.1f}MB =====",
              flush=True)
        print(out.getvalue()[:8000], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

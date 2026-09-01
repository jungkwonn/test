from __future__ import annotations

import csv
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROBOTS_URL = "https://www.ajd.co.kr/robots.txt"
BASE_URL = "https://www.ajd.co.kr"
OUTPUT = Path("output/ajd_robots_blocked_basic_tip_urls.csv")
USER_AGENT = "Mozilla/5.0 (compatible; AJD-public-content-audit/1.0)"


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def classify(decoded_title: str) -> str:
    normalized = decoded_title.strip()
    if re.fullmatch(r"[0-9]+", normalized):
        return "숫자 테스트 제목"
    keywords = ["작성중", "초안", "사용 X", "사용X", "상단 컨텐츠", "하단 고정", "버튼"]
    if any(keyword.lower() in normalized.lower() for keyword in keywords):
        return "초안·내부용 추정"
    return "일반 제목"


def main() -> None:
    text = fetch_text(ROBOTS_URL)
    rows = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line.startswith("Disallow:"):
            continue
        path = line.split(":", 1)[1].strip().removesuffix("$")
        if not path.startswith("/contents/basic-tip/detail/"):
            continue
        match = re.search(r"-(\d+)$", path)
        content_id = int(match.group(1)) if match else None
        decoded_path = urllib.parse.unquote(path)
        decoded_tail = decoded_path.rsplit("/", 1)[-1]
        decoded_title = re.sub(r"-\d+$", "", decoded_tail).replace("_", " ")
        rows.append(
            {
                "No": len(rows) + 1,
                "robotsLine": line_number,
                "contentId": content_id,
                "decodedTitle": decoded_title,
                "classification": classify(decoded_title),
                "url": BASE_URL + path,
                "path": path,
            }
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "No",
                "robotsLine",
                "contentId",
                "decodedTitle",
                "classification",
                "url",
                "path",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    unique_ids = {row["contentId"] for row in rows if row["contentId"] is not None}
    if len(rows) != 1169 or len(unique_ids) != 1169:
        raise RuntimeError(
            f"Unexpected robots count: rows={len(rows)}, uniqueIds={len(unique_ids)}"
        )
    print(f"Exported {len(rows)} blocked basic-tip URLs")


if __name__ == "__main__":
    main()

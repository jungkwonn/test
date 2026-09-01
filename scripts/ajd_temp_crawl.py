from __future__ import annotations

import csv
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_URL = "https://api-platform.ajd.co.kr/boards/honey-tips/contents"
SITEMAP_URL = "https://www.ajd.co.kr/sitemap-honeytips.xml"
ROBOTS_URL = "https://www.ajd.co.kr/robots.txt"
DETAIL_PREFIX = "https://www.ajd.co.kr/contents/basic-tip/detail/"
OUTPUT_DIR = Path("output")
USER_AGENT = "Mozilla/5.0 (compatible; AJD-public-content-audit/1.0; +https://github.com/jungkwonn/test)"


def maybe_decompress(data: bytes) -> bytes:
    """Decode a raw gzip payload even when the origin omits Content-Encoding."""
    result = data
    for _ in range(3):
        if result.startswith(b"\x1f\x8b"):
            result = gzip.decompress(result)
            continue
        break
    return result


def fetch_bytes(url: str, attempts: int = 7) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, application/xml, text/plain, */*",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
                content_encoding = (response.headers.get("Content-Encoding") or "").lower()
                if "gzip" in content_encoding or data.startswith(b"\x1f\x8b"):
                    data = maybe_decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
        time.sleep(min(0.7 * (2**attempt), 12.0))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def fetch_json(url: str) -> dict[str, Any]:
    raw = fetch_bytes(url)
    return json.loads(raw.decode("utf-8-sig"))


def api_page_url(page: int, include_hidden: bool) -> str:
    query = urllib.parse.urlencode(
        {
            "page": page,
            "size": 8,
            "sort": "createdDateTime",
            "includeHidden": str(include_hidden).lower(),
        }
    )
    return f"{API_URL}?{query}"


def fetch_page(page: int, include_hidden: bool = True) -> tuple[int, dict[str, Any]]:
    payload = fetch_json(api_page_url(page, include_hidden))
    returned_page = int(payload.get("number", page))
    if returned_page != page:
        raise RuntimeError(f"Requested page {page}, received page {returned_page}")
    return page, payload


def collect_api_snapshot(max_snapshot_attempts: int = 3) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect a stable paginated snapshot and retry if records change mid-crawl."""
    last_problem = "unknown"
    for snapshot_attempt in range(1, max_snapshot_attempts + 1):
        _, first = fetch_page(0, True)
        total_pages = int(first["totalPages"])
        total_elements = int(first["totalElements"])
        pages: dict[int, dict[str, Any]] = {0: first}
        print(
            f"API snapshot attempt {snapshot_attempt}: "
            f"totalElements={total_elements}, totalPages={total_pages}",
            flush=True,
        )

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(fetch_page, page, True): page
                for page in range(1, total_pages)
            }
            completed = 1
            for future in as_completed(futures):
                page, payload = future.result()
                pages[page] = payload
                completed += 1
                if completed % 100 == 0 or completed == total_pages:
                    print(
                        f"API pages collected: {completed}/{total_pages}",
                        flush=True,
                    )

        records: list[dict[str, Any]] = []
        for page_number in range(total_pages):
            payload = pages[page_number]
            for position, source_item in enumerate(
                payload.get("content", []), start=1
            ):
                item = dict(source_item)
                item["_apiPage"] = page_number
                item["_positionInPage"] = position
                records.append(item)

        ids = [int(item["sn"]) for item in records]
        unique_ids = set(ids)
        _, final_first = fetch_page(0, True)
        final_total_elements = int(final_first["totalElements"])
        final_total_pages = int(final_first["totalPages"])

        problems: list[str] = []
        if len(records) != total_elements:
            problems.append(
                f"record count {len(records)} != declared {total_elements}"
            )
        if len(unique_ids) != len(records):
            problems.append(
                f"unique IDs {len(unique_ids)} != records {len(records)}"
            )
        if final_total_elements != total_elements:
            problems.append(
                f"totalElements changed {total_elements}->{final_total_elements}"
            )
        if final_total_pages != total_pages:
            problems.append(
                f"totalPages changed {total_pages}->{final_total_pages}"
            )

        if not problems:
            return records, {
                "totalPages": total_pages,
                "totalElements": total_elements,
                "pageSize": int(first.get("size", 8)),
                "snapshotAttempt": snapshot_attempt,
            }

        last_problem = "; ".join(problems)
        print(f"Snapshot unstable: {last_problem}. Retrying.", flush=True)
        time.sleep(3)

    raise RuntimeError(
        f"Could not obtain a stable API snapshot after "
        f"{max_snapshot_attempts} attempts: {last_problem}"
    )


def fetch_visible_ids() -> tuple[set[int], int]:
    _, first = fetch_page(0, False)
    total_pages = int(first["totalPages"])
    total_elements = int(first["totalElements"])
    ids: set[int] = {int(item["sn"]) for item in first.get("content", [])}
    for page in range(1, total_pages):
        _, payload = fetch_page(page, False)
        ids.update(int(item["sn"]) for item in payload.get("content", []))
    if len(ids) != total_elements:
        raise RuntimeError(
            f"Visible-list count mismatch: IDs={len(ids)}, declared={total_elements}"
        )
    return ids, total_elements


def parse_sitemap() -> tuple[list[str], dict[int, str], str, list[str]]:
    encoded = urllib.parse.quote(SITEMAP_URL, safe="")
    candidates = [
        ("direct", SITEMAP_URL),
        ("corsmirror", f"https://corsmirror.com/v1?url={encoded}"),
    ]
    errors: list[str] = []

    for mode, url in candidates:
        try:
            raw = maybe_decompress(fetch_bytes(url)).lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
            if not raw.startswith(b"<"):
                raise ValueError(f"Unexpected first bytes: {raw[:24]!r}")
            root = ET.fromstring(raw)
            urls: list[str] = []
            by_id: dict[int, str] = {}
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] != "loc" or not node.text:
                    continue
                sitemap_url = node.text.strip()
                urls.append(sitemap_url)
                match = re.search(r"-(\d+)(?:[/?#].*)?$", sitemap_url)
                if match and "/contents/basic-tip/detail/" in sitemap_url:
                    by_id[int(match.group(1))] = sitemap_url
            if not urls:
                raise ValueError("Parsed sitemap contains no <loc> elements")
            return urls, by_id, mode, errors
        except Exception as exc:  # continue with the verified fallback source
            message = f"{mode}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"Sitemap source failed: {message}", flush=True)

    return [], {}, "unavailable", errors


def parse_robots() -> tuple[list[str], set[int], str | None]:
    try:
        text = fetch_bytes(ROBOTS_URL).decode("utf-8-sig", errors="replace")
    except Exception as exc:
        return [], set(), f"{type(exc).__name__}: {exc}"

    paths: list[str] = []
    ids: set[int] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("Disallow:"):
            continue
        path = line.split(":", 1)[1].strip().removesuffix("$")
        if not path.startswith("/contents/basic-tip/detail/"):
            continue
        paths.append(path)
        match = re.search(r"-(\d+)$", path)
        if match:
            ids.add(int(match.group(1)))
    return paths, ids, None


def fallback_url(title: str, sn: int) -> str:
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="-_.~")
    return f"{DETAIL_PREFIX}{slug}-{sn}"


def bool_text(value: Any) -> str:
    return "TRUE" if bool(value) else "FALSE"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching sitemap...", flush=True)
    sitemap_urls, sitemap_by_id, sitemap_mode, sitemap_errors = parse_sitemap()
    print(
        f"Sitemap URLs: {len(sitemap_urls)} "
        f"(detail IDs={len(sitemap_by_id)}, mode={sitemap_mode})",
        flush=True,
    )

    print("Fetching robots.txt...", flush=True)
    robots_paths, robots_ids, robots_error = parse_robots()
    print(f"Robots basic-tip disallows: {len(robots_paths)}", flush=True)

    print("Fetching all API records...", flush=True)
    records, api_summary = collect_api_snapshot()
    visible_ids, visible_total = fetch_visible_ids()

    seen: set[int] = set()
    duplicates: list[int] = []
    rows: list[dict[str, Any]] = []

    for index, item in enumerate(records, start=1):
        sn = int(item["sn"])
        if sn in seen:
            duplicates.append(sn)
        seen.add(sn)

        title = str(item.get("title") or "")
        exact_url = sitemap_by_id.get(sn)
        url = exact_url or fallback_url(title, sn)
        categories = item.get("categories") or []
        top_categories = [
            category
            for category in categories
            if category.get("parentCategorySn") is None
        ]
        top_category = (
            top_categories[0]
            if top_categories
            else (categories[0] if categories else {})
        ).get("categoryName", "")
        category_path = " > ".join(
            str(category.get("categoryName") or "")
            for category in categories
            if category.get("categoryName")
        )
        creator = item.get("creator") or {}

        rows.append(
            {
                "No": index,
                "sn": sn,
                "title": title,
                "url": url,
                "urlSource": "sitemap" if exact_url else "API title+sn",
                "inSitemap": sn in sitemap_by_id,
                "listedInOrdinaryAPI": sn in visible_ids,
                "robotsDisallow": sn in robots_ids,
                "isHidden": bool(item.get("isHidden")),
                "isDeleted": bool(item.get("isDeleted")),
                "isBlocked": bool(item.get("isBlocked")),
                "isPinned": bool(item.get("isPinned")),
                "priority": item.get("priority"),
                "readCount": item.get("readCount"),
                "scrapCount": item.get("scrapCount"),
                "commentCount": item.get("commentCount"),
                "likeCount": item.get("likeCount"),
                "topCategory": top_category,
                "categoryPath": category_path,
                "creatorNickName": creator.get("nickName", ""),
                "createdDateTime": item.get("createdDateTime", ""),
                "lastModifiedDateTime": item.get("lastModifiedDateTime", ""),
                "thumbnailPath": item.get("thumbnailPath", ""),
                "apiPage": item.get("_apiPage"),
                "positionInPage": item.get("_positionInPage"),
            }
        )

    api_ids = set(seen)
    sitemap_ids = set(sitemap_by_id)
    hidden_count = sum(1 for row in rows if row["isHidden"])
    deleted_count = sum(1 for row in rows if row["isDeleted"])
    blocked_count = sum(1 for row in rows if row["isBlocked"])
    in_sitemap_count = sum(1 for row in rows if row["inSitemap"])
    robots_api_overlap = len(api_ids & robots_ids)

    summary = {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "apiTotalElements": api_summary["totalElements"],
        "apiTotalPages": api_summary["totalPages"],
        "apiPageSize": api_summary["pageSize"],
        "apiSnapshotAttempt": api_summary["snapshotAttempt"],
        "apiRecordsReceived": len(records),
        "uniqueSnCount": len(api_ids),
        "duplicateSnCount": len(duplicates),
        "duplicateSn": sorted(set(duplicates)),
        "ordinaryListTotalElements": visible_total,
        "ordinaryListUniqueIds": len(visible_ids),
        "excludedFromOrdinaryList": len(api_ids - visible_ids),
        "hiddenFlagCount": hidden_count,
        "deletedFlagCount": deleted_count,
        "blockedFlagCount": blocked_count,
        "sitemapFetchMode": sitemap_mode,
        "sitemapFetchErrors": sitemap_errors,
        "sitemapUrlCount": len(sitemap_urls),
        "sitemapDetailUniqueSnCount": len(sitemap_ids),
        "apiIdsInSitemapCount": in_sitemap_count,
        "apiIdsMissingFromSitemapCount": len(api_ids - sitemap_ids),
        "apiIdsMissingFromSitemap": sorted(api_ids - sitemap_ids),
        "sitemapIdsMissingFromApiCount": len(sitemap_ids - api_ids),
        "sitemapIdsMissingFromApi": sorted(sitemap_ids - api_ids),
        "robotsFetchError": robots_error,
        "robotsBasicTipDisallowCount": len(robots_paths),
        "robotsUniqueSnCount": len(robots_ids),
        "apiIdsAlsoRobotsDisallowedCount": robots_api_overlap,
        "sourceApi": API_URL,
        "sourceSitemap": SITEMAP_URL,
        "sourceRobots": ROBOTS_URL,
    }

    csv_fields = [
        "No",
        "sn",
        "title",
        "url",
        "urlSource",
        "inSitemap",
        "listedInOrdinaryAPI",
        "robotsDisallow",
        "isHidden",
        "isDeleted",
        "isBlocked",
        "isPinned",
        "priority",
        "readCount",
        "scrapCount",
        "commentCount",
        "likeCount",
        "topCategory",
        "categoryPath",
        "creatorNickName",
        "createdDateTime",
        "lastModifiedDateTime",
        "thumbnailPath",
        "apiPage",
        "positionInPage",
    ]

    csv_path = OUTPUT_DIR / "ajd_honeytips_urls.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in rows:
            output_row = dict(row)
            for key in [
                "inSitemap",
                "listedInOrdinaryAPI",
                "robotsDisallow",
                "isHidden",
                "isDeleted",
                "isBlocked",
                "isPinned",
            ]:
                output_row[key] = bool_text(output_row[key])
            writer.writerow(output_row)

    json_path = OUTPUT_DIR / "ajd_honeytips_full.json"
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary_path = OUTPUT_DIR / "ajd_honeytips_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Fetch a complete Riksarkivet volume from a bildvisning URL or manifest URL.

Downloads all pages into a media subfolder inside this script directory:
  ./media/<sanitized_book_title>/images

Authentication is read from a separate file outside this project folder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


USER_AGENT = "RiksarkivetBookFetcher/0.1"
BILDVISNING_RE = re.compile(
    r"(?:https?://)?sok\.riksarkivet\.se/bildvisning/([A-Za-z0-9_]+)", re.IGNORECASE
)
MANIFEST_HINT_RE = re.compile(
    r"(?:/manifest(?:\.json)?(?:[/?#]|$)|iiif[^\"']*\.json)", re.IGNORECASE
)
RAW_HTTP_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
LOGIN_FORM_RE = re.compile(
    r"<form[^>]*action=['\"]([^'\"]+)['\"][^>]*method=['\"]post['\"][^>]*>(.*?)</form>",
    re.IGNORECASE | re.DOTALL,
)
INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
INPUT_ATTR_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*['\"]([^'\"]*)['\"]")
MANIFEST_KEY_RE = re.compile(
    r'(?:["\']?(?:manifest|iiif)[A-Za-z0-9_]*["\']?)\s*[:=]\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
IIIF_MANIFEST_ID_RE = re.compile(
    r'iiifManifestId\s*[:=]\s*["\']([^"\']+)["\']', re.IGNORECASE
)
ARKIS_RE = re.compile(r"arkis!([A-Za-z0-9_]+)", re.IGNORECASE)
VALID_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".jp2", ".tif", ".tiff", ".webp"}


class FetchError(RuntimeError):
    """Raised when data cannot be fetched from Riksarkivet."""


@dataclass
class AuthConfig:
    enabled: bool = False
    username: str = ""
    password: str = ""


@dataclass
class BatchJob:
    url: str
    title_override: str = ""
    line_no: int = 0


@dataclass
class FetchJob:
    url: str
    title_override: str = ""
    batch_line_no: int | None = None


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_auth = script_dir.parent / "riksarkivet-auth.json"
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a complete Riksarkivet volume and store pages under "
            "this script folder."
        )
    )
    parser.add_argument(
        "url",
        nargs="?",
        help=(
            "Start URL. Supports bildvisning URL (recommended) or direct IIIF "
            "manifest URL."
        ),
    )
    parser.add_argument(
        "--batch-file",
        default="",
        help=(
            "Text file with one URL per line. Optional title override per row: "
            "`<url>|<title>` or `<url><TAB><title>`."
        ),
    )
    parser.add_argument(
        "--auth-file",
        default=str(default_auth),
        help=(
            "Path to auth JSON file (outside this project folder). "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--media-dir",
        default=str(script_dir / "media"),
        help="Base media directory. Default: %(default)s",
    )
    parser.add_argument(
        "--book-title",
        default="",
        help="Override folder title for this volume (optional).",
    )
    parser.add_argument(
        "--skip-pdf",
        action="store_true",
        help="Skip PDF build and download images only.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout seconds. Default: %(default)s",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=3,
        help="Number of retries per page download. Default: %(default)s",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.15,
        help="Delay in seconds between page downloads. Default: %(default)s",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop batch processing at first failed URL.",
    )
    parser.add_argument(
        "--error-log",
        default="",
        help=(
            "Path to error log file. In batch mode, default is "
            "<batch-file>.errors.log."
        ),
    )
    return parser.parse_args()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def read_multilang_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = read_multilang_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("sv", "se", "none", "en"):
            if key in value:
                text = read_multilang_text(value[key])
                if text:
                    return text
        for inner in value.values():
            text = read_multilang_text(inner)
            if text:
                return text
        return ""
    return str(value).strip()


def sanitize_component(raw: str, fallback: str = "book") -> str:
    text = (raw or "").strip()
    if not text:
        text = fallback
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9._ -]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" ", "_")
    text = re.sub(r"_+", "_", text).strip("._-")
    if not text:
        text = fallback
    return text[:120]


def looks_like_login_page(html_text: str) -> bool:
    lowered = (html_text or "").lower()
    if 'action="/login"' in lowered and 'name="password"' in lowered:
        return True
    if "<title>logga in" in lowered or "<title>login" in lowered:
        return True
    return False


def extract_login_form(html_text: str) -> tuple[str | None, dict[str, str]]:
    match = LOGIN_FORM_RE.search(html_text or "")
    if not match:
        return None, {}

    action = html_lib.unescape(match.group(1).strip())
    form_body = match.group(2)
    fields: dict[str, str] = {}
    for input_match in INPUT_TAG_RE.finditer(form_body):
        tag = input_match.group(0)
        attrs: dict[str, str] = {}
        for attr_match in INPUT_ATTR_RE.finditer(tag):
            attrs[attr_match.group(1).lower()] = html_lib.unescape(attr_match.group(2))
        name = attrs.get("name")
        if not name:
            continue
        input_type = attrs.get("type", "").lower()
        if input_type == "checkbox":
            if name not in fields:
                fields[name] = attrs.get("value", "on")
            continue
        fields[name] = attrs.get("value", "")
    return action, fields


def load_auth_file(path: Path) -> AuthConfig:
    if not path.exists():
        return AuthConfig(enabled=False)

    data = json.loads(path.read_text(encoding="utf-8"))
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    enabled = bool(data.get("enabled", True))
    if not username or not password:
        enabled = False
    return AuthConfig(enabled=enabled, username=username, password=password)


def make_opener() -> Any:
    return build_opener(HTTPCookieProcessor(CookieJar()))


def http_get(
    opener: Any, url: str, timeout: int, accept: str = "*/*", referer: str | None = None
) -> tuple[bytes, str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if referer:
        headers["Referer"] = referer
    request = Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "").lower()
            final_url = response.geturl()
            return payload, content_type, final_url
    except HTTPError as err:
        raise FetchError(f"HTTP error for {url}: {err.reason} ({err.code})") from err
    except URLError as err:
        raise FetchError(f"Network error for {url}: {err.reason}") from err


def login_riksarkivet(
    opener: Any, source_url: str, html_text: str, auth: AuthConfig, timeout: int
) -> None:
    if not auth.enabled:
        raise FetchError(
            "Login required for this page, but auth is disabled or missing in auth file."
        )

    action, fields = extract_login_form(html_text)
    if not action:
        raise FetchError("Could not find Riksarkivet login form in page.")

    fields["Username"] = auth.username
    fields["Password"] = auth.password
    fields["SaveLogin"] = "false"
    fields.setdefault("Url", urlparse(source_url).path or "/")
    fields.setdefault("ReturnUrlIsEncoded", "False")
    fields.setdefault("loginKnappen", "vanlig")

    payload = urlencode(fields).encode("utf-8")
    login_url = urljoin(source_url, action)
    request = Request(
        login_url,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,*/*",
            "Referer": source_url,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as err:
        raise FetchError(f"Login HTTP error: {err.reason} ({err.code})") from err
    except URLError as err:
        raise FetchError(f"Login network error: {err.reason}") from err

    if looks_like_login_page(body):
        raise FetchError("Invalid Riksarkivet login credentials.")


def extract_manifest_urls(html_text: str, base_url: str) -> list[str]:
    urls: set[str] = set()
    for raw_url in RAW_HTTP_URL_RE.findall(html_text):
        absolute = html_lib.unescape(raw_url.strip())
        if MANIFEST_HINT_RE.search(absolute):
            urls.add(absolute)
    for key_match in MANIFEST_KEY_RE.finditer(html_text):
        maybe_url = html_lib.unescape(key_match.group(1).strip())
        urls.add(urljoin(base_url, maybe_url))
    for iiif_match in IIIF_MANIFEST_ID_RE.finditer(html_text):
        maybe_url = html_lib.unescape(iiif_match.group(1).strip())
        urls.add(urljoin(base_url, maybe_url))
    return sorted(urls)


def try_load_json_url(
    opener: Any, url: str, timeout: int, referer: str | None = None
) -> dict[str, Any] | None:
    try:
        raw, _ctype, _final = http_get(
            opener, url, timeout=timeout, accept="application/json,*/*", referer=referer
        )
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


def guess_manifest_urls(url: str) -> list[str]:
    match = BILDVISNING_RE.search(url or "")
    if not match:
        return []
    bildid = match.group(1)
    volume_id = bildid.split("_", 1)[0]
    return [
        f"https://lbiiif.riksarkivet.se/arkis!{volume_id}/manifest",
        f"https://lbiiif.riksarkivet.se/arkis!{volume_id}/manifest.json",
        f"https://sok.riksarkivet.se/iiif/arkis!{volume_id}/manifest",
        f"https://sok.riksarkivet.se/iiif/arkis!{volume_id}/manifest.json",
        f"https://iiif.riksarkivet.se/iiif/arkis!{volume_id}/manifest",
        f"https://iiif.riksarkivet.se/iiif/arkis!{volume_id}/manifest.json",
    ]


def resolve_manifest(
    opener: Any, start_url: str, auth: AuthConfig, timeout: int
) -> tuple[str, dict[str, Any], str]:
    source_url = start_url.strip()
    if source_url.lower().endswith(".json"):
        manifest = try_load_json_url(opener, source_url, timeout=timeout)
        if not manifest:
            raise FetchError("Could not load manifest JSON from given URL.")
        return source_url, manifest, source_url

    raw, content_type, final_url = http_get(
        opener, source_url, timeout=timeout, accept="text/html,*/*", referer=source_url
    )
    if content_type.startswith("application/json"):
        manifest = json.loads(raw.decode("utf-8", errors="replace"))
        return final_url, manifest, source_url

    html_text = raw.decode("utf-8", errors="replace")
    if looks_like_login_page(html_text):
        login_riksarkivet(opener, source_url, html_text, auth, timeout)
        raw, content_type, final_url = http_get(
            opener,
            source_url,
            timeout=timeout,
            accept="text/html,*/*",
            referer=source_url,
        )
        if content_type.startswith("application/json"):
            manifest = json.loads(raw.decode("utf-8", errors="replace"))
            return final_url, manifest, source_url
        html_text = raw.decode("utf-8", errors="replace")
        if looks_like_login_page(html_text):
            raise FetchError("Login still required after authentication attempt.")

    manifest_urls = extract_manifest_urls(html_text, final_url)
    manifest: dict[str, Any] | None = None
    chosen_url = ""
    for manifest_url in manifest_urls:
        manifest = try_load_json_url(opener, manifest_url, timeout=timeout, referer=source_url)
        if manifest:
            chosen_url = manifest_url
            break
    if manifest:
        return chosen_url, manifest, source_url

    for guessed in guess_manifest_urls(source_url):
        manifest = try_load_json_url(opener, guessed, timeout=timeout, referer=source_url)
        if manifest:
            return guessed, manifest, source_url

    raise FetchError("Could not find or load an IIIF manifest from this URL.")


def looks_like_image_url(url: str) -> bool:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix in VALID_IMAGE_EXTS:
        return True
    lowered = url.lower()
    if "/full/max/0/default.jpg" in lowered:
        return True
    if "/full/full/0/default.jpg" in lowered:
        return True
    if "default.jpg" in lowered and "/full/" in lowered:
        return True
    return False


def score_image_url(url: str) -> int:
    lowered = url.lower()
    score = 0
    if "/full/max/0/default.jpg" in lowered:
        score += 300
    if "/full/full/0/default.jpg" in lowered:
        score += 250
    if lowered.endswith(".jpg") or lowered.endswith(".jpeg"):
        score += 120
    if lowered.endswith(".jp2"):
        score += 100
    if lowered.endswith(".png"):
        score += 80
    if "/thumbnail/" in lowered:
        score -= 200
    if "/thumb/" in lowered:
        score -= 150
    return score


def collect_service_candidates(node: Any, out_urls: set[str]) -> None:
    if isinstance(node, dict):
        service_id = node.get("id") or node.get("@id")
        if isinstance(service_id, str):
            service_id = service_id.rstrip("/")
            out_urls.add(service_id + "/full/max/0/default.jpg")
            out_urls.add(service_id + "/full/full/0/default.jpg")
        if "service" in node:
            collect_service_candidates(node["service"], out_urls)
        if "services" in node:
            collect_service_candidates(node["services"], out_urls)
    elif isinstance(node, list):
        for item in node:
            collect_service_candidates(item, out_urls)


def collect_image_candidates(node: Any, out_urls: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("id", "@id") and isinstance(value, str) and looks_like_image_url(value):
                out_urls.add(value)
            if key in ("service", "services"):
                collect_service_candidates(value, out_urls)
            collect_image_candidates(value, out_urls)
    elif isinstance(node, list):
        for item in node:
            collect_image_candidates(item, out_urls)


def extract_canvas_pages(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    canvases = as_list(manifest.get("items"))
    pages: list[dict[str, Any]] = []
    width = max(4, len(str(len(canvases) or 1)))

    for index, canvas in enumerate(canvases, start=1):
        label = read_multilang_text(canvas.get("label")) or f"Page {index}"
        candidates: set[str] = set()

        # Prefer explicit annotation body entries first.
        for anno_page in as_list(canvas.get("items")):
            for annotation in as_list(anno_page.get("items")):
                collect_image_candidates(annotation.get("body"), candidates)

        # Fallback: scan full canvas object recursively.
        collect_image_candidates(canvas, candidates)

        image_url = ""
        if candidates:
            image_url = sorted(candidates, key=score_image_url, reverse=True)[0]

        remote_id = extract_remote_page_id(image_url)
        if not remote_id:
            remote_id = extract_remote_page_id(
                str(canvas.get("id") or canvas.get("@id") or "")
            )

        pages.append(
            {
                "index": index,
                "index_padded": str(index).zfill(width),
                "label": label,
                "canvas_id": canvas.get("id") or canvas.get("@id") or "",
                "image_url": image_url,
                "remote_id": remote_id,
            }
        )
    return pages


def infer_book_id(start_url: str, manifest: dict[str, Any]) -> str:
    manifest_id = str(manifest.get("id") or manifest.get("@id") or "")
    m = ARKIS_RE.search(manifest_id)
    if m:
        return m.group(1)
    m = BILDVISNING_RE.search(start_url)
    if m:
        return m.group(1).split("_", 1)[0]
    return "unknown_book"


def extract_remote_page_id(url: str) -> str:
    """Return page id like C0021353_00001 from IIIF URLs when available."""
    if not url:
        return ""
    match = ARKIS_RE.search(url)
    return match.group(1) if match else ""


def preferred_page_basename(page: dict[str, Any]) -> str:
    """
    Prefer source page id as filename base, fallback to padded index.
    Example: C0021353_00001
    """
    remote_id = str(page.get("remote_id", "")).strip()
    if remote_id:
        return sanitize_component(remote_id, fallback=str(page.get("index_padded", "page")))
    return str(page.get("index_padded", "page"))


def pick_extension(image_url: str, content_type: str) -> str:
    suffix = Path(unquote(urlparse(image_url).path)).suffix.lower()
    if suffix in VALID_IMAGE_EXTS:
        return suffix
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "jp2" in content_type:
        return ".jp2"
    if "tiff" in content_type or "tif" in content_type:
        return ".tif"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def download_one(
    opener: Any,
    page: dict[str, Any],
    source_url: str,
    images_dir: Path,
    timeout: int,
    retries: int,
) -> tuple[Path, str]:
    if not page["image_url"]:
        raise FetchError(f"No image URL available for page {page['index']}.")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            raw, content_type, _final = http_get(
                opener,
                page["image_url"],
                timeout=timeout,
                accept="image/*,*/*",
                referer=source_url,
            )
            if not content_type.startswith("image/") and not raw:
                raise FetchError(
                    f"Downloaded data for page {page['index']} does not look like an image."
                )
            ext = pick_extension(page["image_url"], content_type)
            filename = f"{preferred_page_basename(page)}{ext}"
            final_path = images_dir / filename
            if final_path.exists():
                suffix = 2
                while True:
                    alt = images_dir / f"{preferred_page_basename(page)}_{suffix}{ext}"
                    if not alt.exists():
                        final_path = alt
                        break
                    suffix += 1
            final_path.write_bytes(raw)
            return final_path, content_type
        except Exception as err:
            last_error = err
            if attempt < retries:
                time.sleep(0.6 * attempt)
    raise FetchError(
        f"Failed to download page {page['index']} after {retries} retries: {last_error}"
    )


def build_pdf_with_img2pdf(images: list[Path], out_pdf: Path) -> bool:
    if shutil.which("img2pdf") is None:
        return False
    cmd = ["img2pdf", *[str(p) for p in images], "-o", str(out_pdf)]
    subprocess.run(cmd, check=True)
    return True


def build_pdf_with_pillow(images: list[Path], out_pdf: Path) -> bool:
    try:
        from PIL import Image
    except Exception:
        return False

    loaded = []
    try:
        for path in images:
            loaded.append(Image.open(path).convert("RGB"))
        if not loaded:
            return False
        first, rest = loaded[0], loaded[1:]
        first.save(str(out_pdf), save_all=True, append_images=rest)
        return True
    finally:
        for image in loaded:
            image.close()


def normalize_canvas_id(raw_id: str) -> str:
    if not raw_id:
        return ""
    parsed = urlparse(raw_id)
    if parsed.scheme:
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    else:
        normalized = raw_id.split("?", 1)[0].split("#", 1)[0]
    return normalized.rstrip("/")


def first_page_index_for_node(node: Any, canvas_to_page: dict[str, int]) -> int | None:
    if isinstance(node, str):
        cid = normalize_canvas_id(node)
        if cid in canvas_to_page:
            return canvas_to_page[cid]
        return None

    if not isinstance(node, dict):
        return None

    node_id = str(node.get("id") or node.get("@id") or "")
    node_type = str(node.get("type") or "").lower()
    cid = normalize_canvas_id(node_id)
    if node_type == "canvas" and cid in canvas_to_page:
        return canvas_to_page[cid]

    for child in as_list(node.get("items")):
        found = first_page_index_for_node(child, canvas_to_page)
        if found is not None:
            return found
    return None


def apply_index_bookmarks_to_pdf(
    pdf_path: Path, structures: list[Any], pages: list[dict[str, Any]]
) -> tuple[bool, str]:
    if not structures:
        return False, "No structures available in manifest."

    canvas_to_page: dict[str, int] = {}
    for page in pages:
        try:
            page_index = int(page.get("index", 0)) - 1
        except Exception:
            continue
        if page_index < 0:
            continue
        canvas_id = normalize_canvas_id(str(page.get("canvas_id", "")))
        if canvas_id:
            canvas_to_page[canvas_id] = page_index

    if not canvas_to_page:
        return False, "No canvas-to-page mapping found."

    try:
        import pikepdf
    except Exception as err:
        return False, f"pikepdf not available: {err}"

    bookmark_count = 0

    def add_nodes(nodes: list[Any], parent: Any) -> None:
        nonlocal bookmark_count
        for node in as_list(nodes):
            if not isinstance(node, dict):
                continue
            label = read_multilang_text(node.get("label"))
            items = as_list(node.get("items"))
            if not items:
                continue

            target_index = first_page_index_for_node(node, canvas_to_page)
            if label and target_index is not None:
                outline_item = pikepdf.OutlineItem(label, target_index)
                parent.append(outline_item)
                bookmark_count += 1
                add_nodes(items, outline_item.children)
            else:
                add_nodes(items, parent)

    try:
        with pikepdf.Pdf.open(str(pdf_path), allow_overwriting_input=True) as pdf:
            with pdf.open_outline() as outline:
                outline.root.clear()
                add_nodes(structures, outline.root)
            if bookmark_count > 0:
                pdf.save(str(pdf_path))
                return True, f"Added {bookmark_count} bookmarks."
            return False, "No bookmark targets found in structures."
    except Exception as err:
        return False, f"Failed to apply bookmarks: {err}"


def parse_batch_file(path: Path) -> tuple[list[BatchJob], list[str]]:
    if not path.exists():
        raise FetchError(f"Batch file not found: {path}")

    original_lines = path.read_text(encoding="utf-8").splitlines()
    jobs: list[BatchJob] = []
    for line_no, raw_line in enumerate(original_lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        url_part = line
        title_part = ""
        if "\t" in line:
            url_part, title_part = line.split("\t", 1)
        elif "|" in line:
            url_part, title_part = line.split("|", 1)

        url = url_part.strip()
        title = title_part.strip()
        if not url:
            print(f"[warn] batch line {line_no}: missing URL, skipped")
            continue
        jobs.append(BatchJob(url=url, title_override=title, line_no=line_no))

    if not jobs:
        raise FetchError(f"Batch file has no valid URLs: {path}")
    return jobs, original_lines


def rewrite_batch_file(
    path: Path, original_lines: list[str], completed_line_nos: set[int]
) -> None:
    keep_lines = [
        line for idx, line in enumerate(original_lines, start=1) if idx not in completed_line_nos
    ]
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    content = "\n".join(keep_lines)
    if keep_lines:
        content += "\n"
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def append_error_log(
    log_path: Path, *, url: str, message: str, line_no: int | None = None
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    location = f" line={line_no}" if line_no is not None else ""
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now}\turl={url}{location}\terror={message.strip()}\n")


def run_single_fetch(
    args: argparse.Namespace,
    opener: Any,
    auth: AuthConfig,
    start_url: str,
    title_override: str = "",
) -> None:
    print(f"[info] start url: {start_url}")
    if auth.enabled:
        print(f"[info] auth: enabled ({auth.username})")
    else:
        print("[info] auth: disabled or not configured")

    manifest_url, manifest, source_url = resolve_manifest(
        opener, start_url, auth, timeout=args.timeout
    )
    pages = extract_canvas_pages(manifest)
    if not pages:
        raise FetchError("No pages found in manifest.")

    manifest_title = read_multilang_text(manifest.get("label"))
    book_title = title_override.strip() or manifest_title
    if not book_title:
        book_title = infer_book_id(start_url, manifest)
    safe_title = sanitize_component(book_title, fallback="book")

    media_root = Path(args.media_dir).expanduser().resolve()
    book_dir = media_root / safe_title
    images_dir = book_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] manifest: {manifest_url}")
    print(f"[info] title: {book_title}")
    print(f"[info] folder: {book_dir}")
    print(f"[info] pages: {len(pages)}")

    downloaded = 0
    skipped = 0
    for page in pages:
        existing_patterns = []
        if page.get("remote_id"):
            existing_patterns.append(f"{sanitize_component(page['remote_id'])}.*")
        # Backward compatibility with old runs that used 0001.jpg naming.
        existing_patterns.append(f"{page['index_padded']}.*")

        existing: list[Path] = []
        for pattern in existing_patterns:
            existing = sorted(images_dir.glob(pattern))
            if existing:
                break
        if existing:
            page["file"] = str(existing[0].relative_to(book_dir))
            page["content_type"] = ""
            skipped += 1
            continue

        file_path, content_type = download_one(
            opener=opener,
            page=page,
            source_url=source_url,
            images_dir=images_dir,
            timeout=args.timeout,
            retries=args.retry,
        )
        page["file"] = str(file_path.relative_to(book_dir))
        page["content_type"] = content_type
        downloaded += 1
        if args.sleep > 0:
            time.sleep(args.sleep)
        print(f"[dl] page {page['index']}/{len(pages)} -> {file_path.name}")

    metadata = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "start_url": start_url,
        "source_url": source_url,
        "manifest_url": manifest_url,
        "book_title": book_title,
        "book_title_sanitized": safe_title,
        "book_id": infer_book_id(start_url, manifest),
        "page_count": len(pages),
        "downloaded_now": downloaded,
        "skipped_existing": skipped,
        "structures": manifest.get("structures", []),
        "metadata": manifest.get("metadata", []),
        "pages": pages,
    }

    (book_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (book_dir / "indexing.json").write_text(
        json.dumps({"structures": manifest.get("structures", [])}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not args.skip_pdf:
        ordered_files = []
        for page in pages:
            rel = page.get("file", "")
            if not rel:
                continue
            full = book_dir / rel
            if full.is_file():
                ordered_files.append(full)

        if not ordered_files:
            raise FetchError("No image files available to build PDF.")

        pdf_path = book_dir / f"{safe_title}.pdf"
        built = False
        try:
            built = build_pdf_with_img2pdf(ordered_files, pdf_path)
        except Exception as err:
            print(f"[warn] img2pdf failed: {err}")
        if not built:
            try:
                built = build_pdf_with_pillow(ordered_files, pdf_path)
            except Exception as err:
                print(f"[warn] Pillow PDF build failed: {err}")
        if not built:
            raise FetchError(
                "PDF was not created (install `img2pdf` or `python3-pillow`)."
            )

        print(f"[ok] pdf created: {pdf_path}")
        structures = manifest.get("structures", [])
        if structures:
            indexed, index_msg = apply_index_bookmarks_to_pdf(pdf_path, structures, pages)
            if not indexed:
                raise FetchError(f"PDF index bookmarks not created: {index_msg}")
            print(f"[ok] pdf index bookmarks created: {index_msg}")
        else:
            print("[info] no index structures in manifest; skipping PDF bookmarks")

    print("[ok] done")


def main() -> int:
    args = parse_args()
    opener = make_opener()

    auth_path = Path(args.auth_file).expanduser()
    auth = load_auth_file(auth_path)
    print(f"[info] auth file: {auth_path}")

    jobs: list[FetchJob] = []
    batch_path: Path | None = None
    batch_original_lines: list[str] = []
    completed_batch_line_nos: set[int] = set()
    if args.url:
        jobs.append(
            FetchJob(url=args.url.strip(), title_override=args.book_title.strip())
        )
    if args.batch_file:
        if args.book_title.strip() and not args.url:
            print("[warn] --book-title is ignored in pure batch mode; use per-line title.")
        batch_path = Path(args.batch_file).expanduser()
        batch_jobs, batch_original_lines = parse_batch_file(batch_path)
        jobs.extend(
            FetchJob(
                url=job.url,
                title_override=job.title_override,
                batch_line_no=job.line_no,
            )
            for job in batch_jobs
        )

    if not jobs:
        raise FetchError("Provide a URL argument or --batch-file.")

    if args.error_log.strip():
        error_log_path = Path(args.error_log).expanduser()
    elif batch_path is not None:
        error_log_path = batch_path.parent / f"{batch_path.name}.errors.log"
    else:
        error_log_path = Path(__file__).resolve().parent / "fetch-errors.log"
    print(f"[info] error log: {error_log_path}")

    if len(jobs) > 1:
        print(f"[info] batch mode: {len(jobs)} jobs")

    failures = 0
    for idx, job in enumerate(jobs, start=1):
        if len(jobs) > 1:
            print(f"[batch] {idx}/{len(jobs)}")
        try:
            run_single_fetch(args, opener, auth, job.url, job.title_override)
            if batch_path is not None and job.batch_line_no is not None:
                completed_batch_line_nos.add(job.batch_line_no)
                rewrite_batch_file(
                    batch_path, batch_original_lines, completed_batch_line_nos
                )
        except FetchError as err:
            failures += 1
            append_error_log(
                error_log_path,
                url=job.url,
                message=str(err),
                line_no=job.batch_line_no,
            )
            print(f"[error] {job.url}: {err}", file=sys.stderr)
            if args.stop_on_error:
                break

    if failures:
        print(f"[warn] completed with {failures} failed job(s)")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FetchError as err:
        print(f"[error] {err}", file=sys.stderr)
        raise SystemExit(1)

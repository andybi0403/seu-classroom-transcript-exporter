from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_URL = "https://cvs.seu.edu.cn/jy-application-resourcemanage"
AUTH_ERROR_STATUSES = {401, 10013}
TOKEN_KEY = b"jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN"
COURSE_PAGE_URL = "https://cvs.seu.edu.cn/jy-application-resourcemanage-ui/#/play-center"
QUERY_AES_KEY = b"jy-resource-qry1"
QUERY_AES_IV = b"jy-query-iv-0001"
QUERY_PREFIX = b"__encrypted_query__:"
BROWSER_USER_DATA_PATHS = (
    Path("Google/Chrome/User Data"),
    Path("Microsoft/Edge/User Data"),
)
TIME_KEYS = {
    "courBeginTime",
    "courEndTime",
    "courseStarttime",
    "courseEndtime",
    "startTime",
    "beginTime",
    "endTime",
    "liveStartTime",
    "liveEndTime",
}


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CourseConfig:
    source_url: str
    tecl_id: str
    tecl_code: str = ""
    subj_id: str = ""
    link_token: str = field(default="", repr=False, compare=False)


def aes_cbc_decrypt_windows(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """Decode the website's AES-CBC/PKCS7 links using Windows' built-in CNG."""
    if os.name != "nt":
        raise RuntimeError("加密课程链接需要在 Windows 上解析。")
    if len(key) not in (16, 24, 32) or len(iv) != 16 or not ciphertext or len(ciphertext) % 16:
        raise ValueError("Invalid AES-CBC input length")
    from ctypes import wintypes

    byte = ctypes.c_ubyte
    pbyte = ctypes.POINTER(byte)
    ulong, handle = wintypes.ULONG, wintypes.HANDLE
    bcrypt = ctypes.WinDLL("bcrypt.dll")

    def bind(name: str, args: list[Any]) -> Any:
        function = getattr(bcrypt, name)
        function.argtypes = args
        function.restype = ctypes.c_int32
        return function

    open_algorithm = bind("BCryptOpenAlgorithmProvider", [ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong])
    set_property = bind("BCryptSetProperty", [handle, wintypes.LPCWSTR, pbyte, ulong, ulong])
    get_property = bind("BCryptGetProperty", [handle, wintypes.LPCWSTR, pbyte, ulong, ctypes.POINTER(ulong), ulong])
    generate_key = bind("BCryptGenerateSymmetricKey", [handle, ctypes.POINTER(handle), pbyte, ulong, pbyte, ulong, ulong])
    decrypt = bind("BCryptDecrypt", [handle, pbyte, ulong, ctypes.c_void_p, pbyte, ulong, pbyte, ulong, ctypes.POINTER(ulong), ulong])
    destroy_key = bind("BCryptDestroyKey", [handle])
    close_algorithm = bind("BCryptCloseAlgorithmProvider", [handle, ulong])

    def check(status: int, operation: str) -> None:
        if status < 0:
            raise RuntimeError(f"{operation}: NTSTATUS 0x{status & 0xffffffff:08X}")

    algorithm, symmetric_key = handle(), handle()
    key_object = secret = None
    try:
        check(open_algorithm(ctypes.byref(algorithm), "AES", None, 0), "Open AES")
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        check(set_property(algorithm, "ChainingMode", ctypes.cast(mode, pbyte), ctypes.sizeof(mode), 0), "Set CBC")
        length, returned = ulong(), ulong()
        check(get_property(algorithm, "ObjectLength", ctypes.cast(ctypes.byref(length), pbyte), ctypes.sizeof(length), ctypes.byref(returned), 0), "Get key length")
        if returned.value != ctypes.sizeof(length) or not 0 < length.value < 1024 * 1024:
            raise RuntimeError("Unexpected AES key-object length")
        key_object = (byte * length.value)()
        secret = (byte * len(key)).from_buffer_copy(key)
        check(generate_key(algorithm, ctypes.byref(symmetric_key), key_object, len(key_object), secret, len(key), 0), "Generate AES key")
        source = (byte * len(ciphertext)).from_buffer_copy(ciphertext)
        iv_buffer = (byte * len(iv)).from_buffer_copy(iv)
        output = (byte * len(ciphertext))()
        used = ulong()
        # CNG changes the IV in place. A single call also validates PKCS7 padding.
        check(decrypt(symmetric_key, source, len(source), None, iv_buffer, len(iv_buffer), output, len(output), ctypes.byref(used), 1), "Decrypt query")
        if used.value > len(output):
            raise RuntimeError("Invalid AES output length")
        return bytes(output[:used.value])
    finally:
        if symmetric_key.value:
            destroy_key(symmetric_key)
        if algorithm.value:
            close_algorithm(algorithm, 0)
        if key_object is not None:
            ctypes.memset(key_object, 0, ctypes.sizeof(key_object))
        if secret is not None:
            ctypes.memset(secret, 0, ctypes.sizeof(secret))


def course_query_params(query: str) -> dict[str, list[str]]:
    params = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=128)
    if "teclId" in params:
        return params
    # decodeURIComponent preserves literal '+', unlike unquote_plus/parse_qs.
    decoded = urllib.parse.unquote(query)
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", decoded):
        try:
            ciphertext = base64.b64decode(decoded, validate=True)
            plaintext = aes_cbc_decrypt_windows(ciphertext, QUERY_AES_KEY, QUERY_AES_IV)
            if not plaintext.startswith(QUERY_PREFIX):
                raise ValueError("Unexpected encrypted-query marker")
            query_text = plaintext[len(QUERY_PREFIX):].decode("utf-8")
            return urllib.parse.parse_qs(query_text, keep_blank_values=True, max_num_fields=128)
        except (binascii.Error, UnicodeError, ValueError, OSError, RuntimeError) as exc:
            raise ValueError("加密课程链接无法解析，可能复制不完整。请重新复制浏览器地址栏的完整课程链接。") from exc
    return params


def parse_course_url(url: str) -> CourseConfig:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("请先粘贴东大云课堂课程播放页的完整链接。")
    if len(url) > 262144:
        raise ValueError("课程链接过长，请重新复制完整播放页链接。")
    text = url.strip().strip('\"\'<>').replace("&amp;", "&")
    markdown = re.fullmatch(r"\[.*\]\((https?://\S+)\)", text, re.DOTALL)
    if markdown:
        text = markdown.group(1)
    pending = [(text, 0)]
    seen: set[str] = set()
    matches: list[CourseConfig] = []
    redirect_keys = {"url", "service", "redirect", "redirect_uri", "redirect_url", "redirecturl", "returnurl", "return_url", "targeturl"}

    def single(params: dict[str, list[str]], name: str) -> str:
        values = set(params.get(name, []))
        if len(values) > 1:
            raise ValueError(f"课程链接里存在冲突的 {name} 参数，请重新复制完整课程链接。")
        return next(iter(values), "")

    while pending:
        candidate, depth = pending.pop(0)
        if candidate in seen or depth > 6:
            continue
        seen.add(candidate)
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            decoded = urllib.parse.unquote(candidate)
            if decoded != candidate:
                pending.append((decoded, depth + 1))
            continue
        fragment_path, separator, fragment_query = parsed.fragment.partition("?")
        query = ""
        is_course_page = False
        if parsed.hostname.lower() == "cvs.seu.edu.cn":
            if fragment_path.rstrip("/") == "/play-center":
                query, is_course_page = fragment_query, bool(separator)
            elif parsed.path.rstrip("/").endswith("/play-center"):
                query, is_course_page = parsed.query, True
        if is_course_page:
            params = course_query_params(query)
            tecl_id = single(params, "teclId")
            if tecl_id:
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tecl_id):
                    raise ValueError("课程编号格式不正确，请重新复制完整课程链接。")
                tecl_code, subj_id = single(params, "teclCode"), single(params, "subjId")
                target = single(params, "target") or "video"
                token = single(params, "jwt_token")
                if token and not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
                    raise ValueError("课程链接里的登录信息不完整，请重新打开课程后复制链接。")
                # Only public course identifiers go into Referer/exported source.
                public_params = {"teclId": tecl_id, "teclCode": tecl_code, "subjId": subj_id, "target": target}
                canonical = COURSE_PAGE_URL + "?" + urllib.parse.urlencode({key: value for key, value in public_params.items() if value})
                matches.append(CourseConfig(canonical, tecl_id, tecl_code, subj_id, token))
                continue
        for nested_query in (parsed.query, fragment_query):
            params = urllib.parse.parse_qs(nested_query, keep_blank_values=True, max_num_fields=128)
            for key, values in params.items():
                if key.lower() in redirect_keys:
                    pending.extend((value, depth + 1) for value in values if value)
    if matches:
        if len({(item.tecl_id, item.tecl_code, item.subj_id) for item in matches}) > 1:
            raise ValueError("链接指向多个不同课程，请直接复制需要导出的课程播放页地址。")
        return matches[0]
    raise ValueError("没有识别到课程信息。请在 Chrome 或 Edge 中打开课程播放页，再复制地址栏的完整链接（普通链接和加密链接均支持）。")


def chrome_profile_dirs() -> list[Path]:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        raise RuntimeError("找不到当前 Windows 用户的应用数据目录。")

    profiles: list[Path] = []
    for relative in BROWSER_USER_DATA_PATHS:
        root = Path(local_appdata) / relative
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if not path.is_dir():
                continue
            if path.name == "Default" or path.name.startswith("Profile "):
                profiles.append(path)
    return sorted(profiles, key=lambda item: (str(item.parent), item.name != "Default", item.name))


def chrome_storage_files() -> list[Path]:
    files: list[Path] = []
    for profile in chrome_profile_dirs():
        candidates = [
            profile / "Session Storage",
            profile / "Local Storage" / "leveldb",
            profile / "IndexedDB" / "https_cvs.seu.edu.cn_0.indexeddb.leveldb",
            profile / "WebStorage",
        ]
        for directory in candidates:
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if not path.is_file() or path.name == "LOCK":
                    continue
                if path.suffix.lower() not in {".log", ".ldb", ""}:
                    continue
                files.append(path)
    return files


def b64url_json(part: str) -> dict[str, Any]:
    padded = part + "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def token_payload(token: str) -> dict[str, Any]:
    try:
        return b64url_json(token.split(".")[1])
    except Exception:
        return {}


def extract_tokens_from_chrome() -> list[str]:
    token_pattern = re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
    tokens: set[str] = set()
    files = chrome_storage_files()
    if not files:
        raise RuntimeError("找不到浏览器登录记录，请在 Edge 或 Chrome 中登录并打开课程播放页。")

    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        is_seu_storage = "https_cvs.seu.edu.cn" in str(path).replace("\\", "/")
        if not is_seu_storage and b"cvs.seu.edu.cn" not in data and TOKEN_KEY not in data:
            continue
        for blob in (data, data.replace(b"\x00", b"")):
            for match in token_pattern.finditer(blob):
                token = match.group(0).decode("ascii", errors="ignore")
                payload = token_payload(token)
                if TOKEN_KEY in data or "cloud-rbac" in str(payload.get("iss", "")):
                    tokens.add(token)

    def sort_key(token: str) -> tuple[int, int]:
        payload = token_payload(token)
        exp = int(payload.get("exp") or 0)
        iat = int(payload.get("iat") or 0)
        return exp, iat

    now = int(time.time())
    ordered = sorted(tokens, key=sort_key, reverse=True)
    valid = [token for token in ordered if int(token_payload(token).get("exp") or 0) > now + 60]
    return valid or ordered


class SeuApi:
    def __init__(self, token: str, referer: str) -> None:
        self.token = token
        self.referer = referer

    def error_detail(self, value: Any) -> str:
        text = str(value)
        if self.token:
            text = text.replace(self.token, "[登录信息已隐藏]")
        return text[:300]

    def open_response(self, request: urllib.request.Request) -> Any:
        # ProxyHandler mutates Request.host/_tunnel_host and can add proxy auth.
        # Keep a clean request for a possible direct retry.
        direct_request = urllib.request.Request(
            request.full_url,
            data=request.data,
            headers=dict(request.header_items()),
            method=request.get_method(),
        )
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError:
            # An HTTP rejection is a server response, not a proxy failure.
            raise
        except urllib.error.URLError:
            parsed = urllib.parse.urlsplit(request.full_url)
            if parsed.scheme != "https" or parsed.hostname != "cvs.seu.edu.cn" or not urllib.request.getproxies().get("https"):
                raise
            # A local/system proxy can abort Python's TLS connection. Retry only
            # this official SEU host directly; both paths verify certificates.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(direct_request, timeout=60)

    def request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Referer": self.referer,
                "Origin": "https://cvs.seu.edu.cn",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36"
                ),
                "jwt-token": self.token,
            },
        )
        try:
            with self.open_response(request) as response:
                text = response.read().decode("utf-8-sig", errors="replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8-sig", errors="replace")
            raise ApiError(f"HTTP {exc.code} for {path}: {self.error_detail(text)}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"无法连接东大云课堂，请检查网络或校园网连接：{self.error_detail(exc.reason)}") from exc

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError(f"Non-JSON response for {path}: {self.error_detail(text)}") from exc

        unwrapped = payload.get("result", payload) if isinstance(payload, dict) else payload
        if isinstance(unwrapped, dict):
            status = unwrapped.get("status")
            if status is not None and int(status) != 200:
                raise ApiError(f"API status {status} for {path}: {self.error_detail(unwrapped)}", int(status))
        return unwrapped


def first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return ""


def clean(value: Any) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()


def format_datetime_value(value: Any) -> Any:
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = clean(value)
        if not re.fullmatch(r"\d{10,13}", text):
            return value
        number = float(text)
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return value


def normalize_time_fields(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for key in TIME_KEYS:
        if key in normalized:
            normalized[key] = format_datetime_value(normalized[key])
    return normalized


def records_from(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    nested = payload.get("data")
    for key in ("records", "list", "rows"):
        for source in (nested, payload):
            if isinstance(source, dict) and isinstance(source.get(key), list):
                return [item for item in source[key] if isinstance(item, dict)]
    return []


def data_part(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def course_id_of(course: dict[str, Any]) -> str:
    return clean(first(course, "courseId", "id", "courId", "courseVodId"))


def course_time(course: dict[str, Any]) -> str:
    return clean(first(course, "courBeginTime", "courseStarttime", "startTime", "beginTime"))


def week_key_of(course: dict[str, Any]) -> str:
    return clean(first(course, "weekNo", "week", "schoolWeek", "weekNumber")) or course_time(course)[:10]


def week_number_from_key(key: str, fallback: int) -> int:
    return int(key) if clean(key).isdigit() else fallback


def normalize_segments(segments: Any, course_id: str) -> list[dict[str, Any]]:
    if not isinstance(segments, list):
        return []
    normalized: list[dict[str, Any]] = []
    for segment in segments:
        if isinstance(segment, dict):
            text = clean(
                segment.get("res")
                or segment.get("zh")
                or segment.get("text")
                or segment.get("content")
                or segment.get("caption")
            )
            if text:
                item = dict(segment)
                item["courseId"] = item.get("courseId") or course_id
                item["res"] = text
                normalized.append(item)
        else:
            text = clean(segment)
            if text:
                normalized.append({"courseId": course_id, "res": text})
    return normalized


def fetch_courses(api: SeuApi, config: CourseConfig) -> list[dict[str, Any]]:
    payload = api.request(
        "/v1/subject_vod_list_new",
        params={
            "page.pageIndex": 1,
            "page.pageSize": 1000,
            "teclIds": config.tecl_id,
            "page.orders[0].asc": "false",
            "page.orders[0].field": "courBeginTime",
        },
    )
    courses = records_from(payload)
    courses.sort(key=lambda item: (course_time(item), clean(first(item, "letiNumber", "lessonNumber", "section"))))
    return [normalize_time_fields(course) for course in courses]


def get_authenticated_api(config: CourseConfig) -> tuple[SeuApi, list[dict[str, Any]]]:
    last_error: Exception | None = None
    if config.link_token:
        api = SeuApi(config.link_token, referer=config.source_url)
        try:
            return api, fetch_courses(api, config)
        except ApiError as exc:
            last_error = exc
            if exc.status not in AUTH_ERROR_STATUSES:
                raise
    try:
        tokens = extract_tokens_from_chrome()
    except RuntimeError as exc:
        if last_error is not None:
            raise RuntimeError("课程链接中的登录信息已失效，也没有找到可用的浏览器登录态。请在 Chrome 或 Edge 重新登录云课堂、打开课程，再复制完整链接。") from exc
        raise
    if not tokens:
        raise RuntimeError("没有找到可用的云课堂登录信息。请在 Chrome 或 Edge 重新登录云课堂并打开课程播放页。")

    for token in tokens:
        api = SeuApi(token, referer=config.source_url)
        try:
            return api, fetch_courses(api, config)
        except ApiError as exc:
            last_error = exc
            if exc.status in AUTH_ERROR_STATUSES:
                continue
            raise
    raise RuntimeError(
        "Chrome/Edge 中找到的云课堂登录态都已失效或服务器不认可。"
        "请在 Chrome 或 Edge 里重新打开东大云课堂并登录，然后刷新本工具再试。"
        f"最后一次错误：{last_error}"
    ) from last_error


def preview_course(courses: list[dict[str, Any]], config: CourseConfig) -> dict[str, Any]:
    with_transcript = 0
    weeks: dict[str, int] = {}
    for course in courses:
        key = week_key_of(course)
        if key:
            weeks[key] = weeks.get(key, 0) + 1
        if course.get("courTransferFlag") or course.get("coureseTransferFlag"):
            with_transcript += 1
    subject = clean(first(courses[0], "subjName", "courName", "subjectName")) if courses else ""
    return {
        "subject": subject,
        "teclId": config.tecl_id,
        "teclCode": config.tecl_code,
        "subjId": config.subj_id,
        "courseCount": len(courses),
        "possiblyReadyCount": with_transcript,
        "weeks": [
            {"week": key, "count": weeks[key]}
            for key in sorted(weeks, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
        ],
        "firstTime": course_time(courses[0]) if courses else "",
        "lastTime": course_time(courses[-1]) if courses else "",
    }


def fetch_replay_urls(api: SeuApi, course_id: str) -> list[str]:
    """Resolve the signed media URLs returned by the authenticated replay API."""
    data = data_part(api.request("/v1/course_vod_urls_new", params={"courseId": course_id}))
    views = data.get("courseVodViewList") if isinstance(data, dict) else None
    if not isinstance(views, list):
        return []

    urls: dict[str, int] = {}
    audio_priority = {"5": 0, "3": 1, "1": 3}
    for view in views:
        if not isinstance(view, dict):
            continue
        url = clean(view.get("url"))
        if url.startswith("//"):
            url = "https:" + url
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").endswith(".seu.edu.cn"):
            continue
        if parsed.username is not None or parsed.password is not None:
            continue
        rank = audio_priority.get(str(view.get("viewNum")), 2)
        urls[url] = min(urls.get(url, rank), rank)
    return sorted(urls, key=urls.__getitem__)


def fetch_caption_segments(api: SeuApi, course_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    try:
        caption_payload = api.request(f"/v1/course/ai/translate/{urllib.parse.quote(course_id)}")
        caption_data = data_part(caption_payload)
    except ApiError:
        caption_data = {}

    if isinstance(caption_data, list):
        raw_source = caption_data
        merged_source: list[Any] = []
        source = "caption-list"
    elif isinstance(caption_data, dict):
        raw_source = (
            caption_data.get("beforeAssemblyList")
            or caption_data.get("rawSegments")
            or caption_data.get("list")
            or caption_data.get("records")
            or caption_data.get("afterAssemblyList")
            or []
        )
        merged_source = caption_data.get("afterAssemblyList") or []
        source = "caption-translate"
    else:
        raw_source = []
        merged_source = []
        source = ""

    raw_segments = normalize_segments(raw_source, course_id)
    merged_segments = normalize_segments(merged_source, course_id)
    if raw_segments:
        return raw_segments, merged_segments, source

    try:
        subtitle_payload = api.request("/v1/course_vod_subtitle_with_keyword", params={"courseId": course_id})
        subtitle_data = data_part(subtitle_payload)
        if isinstance(subtitle_data, dict):
            raw_segments = normalize_segments(subtitle_data.get("courseTransferEsDtoList") or [], course_id)
        elif isinstance(subtitle_data, list):
            raw_segments = normalize_segments(subtitle_data, course_id)
        if raw_segments:
            return raw_segments, [], "subtitle-search"
    except ApiError:
        pass

    try:
        original_payload = api.request("/v1/course_vod_subtitle", params={"courseId": course_id})
        original_segments = normalize_segments(data_part(original_payload), course_id)
        if original_segments:
            return original_segments, [], "subtitle-original"
    except ApiError:
        pass

    try:
        aftertime_payload = api.request(f"/v1/aftertime/captions/{urllib.parse.quote(course_id)}", method="POST")
        aftertime_data = data_part(aftertime_payload)
        if isinstance(aftertime_data, dict):
            raw_segments = normalize_segments(
                aftertime_data.get("courseTransferEsDtoList")
                or aftertime_data.get("captionList")
                or aftertime_data.get("captions")
                or aftertime_data.get("list")
                or [],
                course_id,
            )
        elif isinstance(aftertime_data, list):
            raw_segments = normalize_segments(aftertime_data, course_id)
        if raw_segments:
            return raw_segments, [], "aftertime"
    except ApiError:
        pass

    return [], [], "none"


def build_payload(
    api: SeuApi, config: CourseConfig, courses: list[dict[str, Any]], progress: Any | None = None
) -> dict[str, Any]:
    if not courses:
        raise RuntimeError("没有查到这门课程的课次。")

    week_keys: list[str] = []
    for course in courses:
        key = week_key_of(course)
        if key and key not in week_keys:
            week_keys.append(key)

    def read_session(course: dict[str, Any], course_id: str) -> dict[str, Any]:
        detail: dict[str, Any] = {}
        try:
            detail_payload = api.request("/v1/course_vod_videoinfos", params={"courseId": course_id})
            detail_data = data_part(detail_payload)
            if isinstance(detail_data, dict):
                detail = normalize_time_fields(detail_data)
        except ApiError:
            pass

        raw_segments, merged_segments, caption_source = fetch_caption_segments(api, course_id)
        combined = {**course, **detail}
        key = week_key_of(combined) or week_key_of(course)
        fallback_week_number = max(1, week_keys.index(key) + 1) if key in week_keys else len(week_keys) + 1
        week_number = week_number_from_key(key, fallback_week_number)
        return {
            **combined,
            "id": first(combined, "id", "courseId", "courId") or course_id,
            "courseId": course_id,
            "weekNumber": week_number,
            "week": first(combined, "weekNo", "week", "schoolWeek") or key,
            "rawSegments": raw_segments,
            "mergedSegments": merged_segments,
            "captionSource": caption_source,
            "transcriptText": "\n".join(segment["res"] for segment in raw_segments),
        }

    sessions_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {
            pool.submit(read_session, course, course_id): index
            for index, course in enumerate(courses)
            if (course_id := course_id_of(course))
        }
        for count, future in enumerate(as_completed(pending), start=1):
            sessions_by_index[pending[future]] = future.result()
            if progress:
                progress({
                    "index": count,
                    "total": len(pending),
                    "message": f"已读取 {count}/{len(pending)} 次课的平台字幕。",
                })
    sessions = [sessions_by_index[index] for index in sorted(sessions_by_index)]

    return {
        "source": config.source_url,
        "teclId": config.tecl_id,
        "teclCode": config.tecl_code,
        "subjId": config.subj_id,
        "weekStart": 1,
        "weekEnd": max((int(item.get("weekNumber") or 1) for item in sessions), default=1),
        "extractedAt": datetime.now().isoformat(timespec="seconds"),
        "weeks": sessions,
    }

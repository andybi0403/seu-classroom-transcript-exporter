from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterator
from uuid import uuid4

from seu_voice_lib import fetch_seu_voice_from_chrome as seu
from seu_voice_lib import local_transcription as local
from seu_voice_lib import punctuation
from seu_voice_lib.make_voice_docs import generate_docs, segments_to_lines
from seu_voice_lib.simplified_chinese import simplified


APP_PORT = 8786
JOBS: dict[str, dict[str, Any]] = {}
EXPORT_QUEUE: queue.Queue = queue.Queue()
QUEUE_START = threading.Lock()
QUEUE_THREAD: threading.Thread | None = None


def process_exports() -> None:
    while True:
        runner, job_id, args = EXPORT_QUEUE.get()
        try:
            runner(job_id, *args)
        except Exception as exc:
            JOBS[job_id].update(status="error", message=str(exc))
        finally:
            EXPORT_QUEUE.task_done()


def queue_export(runner: Callable[..., None], *args: Any) -> str:
    global QUEUE_THREAD
    job_id = uuid4().hex
    JOBS[job_id] = {"status": "queued", "message": "已排队，等待前面的导出任务完成。"}
    with QUEUE_START:
        if QUEUE_THREAD is None:
            QUEUE_THREAD = threading.Thread(target=process_exports, daemon=True)
            QUEUE_THREAD.start()
    EXPORT_QUEUE.put((runner, job_id, args))
    return job_id


def desktop_path() -> Path:
    buffer = ctypes.create_unicode_buffer(260)
    # CSIDL_DESKTOPDIRECTORY resolves redirected Windows desktops as well.
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0 and buffer.value:
        return Path(buffer.value)
    return Path.home() / "Desktop"


DESKTOP = desktop_path()


def safe_name(value: str, fallback: str = "东大云课堂语音") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return text or fallback


def output_directory(value: str, folder_name: str, subject: str) -> Path:
    if value:
        path = Path(value.strip().strip('"').replace("?", "_"))
        return (DESKTOP / path).resolve()
    if folder_name:
        return DESKTOP / safe_name(folder_name)
    return DESKTOP / f"{safe_name(subject)}_课堂语音_{datetime.now():%Y%m%d_%H%M%S}"


def write_raw_export(payload: dict[str, Any], target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_json = target_dir / "seu_voice_raw.json"
    pending = target_dir / f".{raw_json.name}.{uuid4().hex}.tmp"
    try:
        pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(raw_json)
    finally:
        pending.unlink(missing_ok=True)
    return raw_json


def add_stage_time(job: dict[str, Any], stage: str, seconds: float) -> None:
    times = job.get("stageSeconds", {})
    job["stageSeconds"] = {**times, stage: times.get(stage, 0) + seconds}


def save_export(job: dict[str, Any], payload: dict[str, Any], target_dir: Path, include_empty: bool) -> None:
    sessions = payload.get("weeks") or payload.get("sessions") or []
    if any(item.get("captionSource") == local.SOURCE and not item.get("punctuatedText") for item in sessions):
        job["message"] = "正在为本地转写添加标点…"
        started = time.perf_counter()
        try:
            punctuation.add_to_payload(payload)
        except RuntimeError as exc:
            job.setdefault("warnings", []).append(str(exc))
        add_stage_time(job, "punctuation", time.perf_counter() - started)
    started = time.perf_counter()
    raw_json = write_raw_export(payload, target_dir)
    job["message"] = "正在生成 Word 文档…"
    docs = generate_docs(payload, target_dir, include_empty)
    add_stage_time(job, "write", time.perf_counter() - started)
    job.update(
        status="done",
        message=f"已生成 {len(docs)} 个 Word 文档。",
        outputDir=str(target_dir),
        rawJson=raw_json.name,
        docs=[path.name for path in docs],
        emptyCount=sum(not segments_to_lines(item) for item in sessions),
    )


def simplify_captions(payload: dict[str, Any]) -> None:
    for session in payload.get("weeks") or payload.get("sessions") or []:
        if isinstance(session.get("transcriptText"), str):
            session["transcriptText"] = simplified(session["transcriptText"])
        for key in ("rawSegments", "mergedSegments"):
            segments = session.get(key)
            if not isinstance(segments, list):
                continue
            for index, segment in enumerate(segments):
                if isinstance(segment, str):
                    segments[index] = simplified(segment)
                elif isinstance(segment, dict):
                    for name in ("res", "zh", "text", "content", "caption"):
                        if isinstance(segment.get(name), str):
                            segment[name] = simplified(segment[name])


def apply_transcript(session: dict[str, Any], values: list[dict[str, Any]]) -> None:
    segments = seu.normalize_segments(values, seu.clean(seu.first(session, "courseId", "id")))
    for segment in segments:
        segment["res"] = simplified(segment["res"])
    session.pop("punctuatedText", None)
    session.update(rawSegments=segments, mergedSegments=[], captionSource=local.SOURCE,
                   transcriptText="\n".join(segment["res"] for segment in segments))


def reuse_transcripts(payload: dict[str, Any], target: Path) -> int:
    previous_path = target / "seu_voice_raw.json"
    if not previous_path.is_file():
        return 0
    try:
        previous, sessions = local.load_export(previous_path)
    except ValueError:
        return 0

    teaching_class = seu.clean(payload.get("teclId"))
    if not teaching_class or teaching_class != seu.clean(previous.get("teclId")):
        return 0
    completed = {
        seu.course_id_of(session): session
        for session in sessions
        if session.get("captionSource") == local.SOURCE
        and isinstance(session.get("rawSegments"), list)
        and segments_to_lines(session)
    }
    reused = 0
    for session in payload["weeks"]:
        earlier = completed.get(seu.course_id_of(session))
        if earlier and not segments_to_lines(session):
            apply_transcript(session, earlier["rawSegments"])
            if earlier.get("punctuatedText"):
                session["punctuatedText"] = earlier["punctuatedText"]
            reused += 1
    return reused


def report_device(job: dict[str, Any], event: dict[str, Any]) -> None:
    job["device"] = event["device"]
    if event.get("warning"):
        job["deviceWarning"] = event["warning"]


def fill_missing_captions(
    api: seu.SeuApi, payload: dict[str, Any], target: Path, job: dict[str, Any],
) -> None:
    reused = reuse_transcripts(payload, target)
    missing = [item for item in payload["weeks"] if not segments_to_lines(item)]
    warnings: list[str] = []
    job.update(auto=True, transcribedCount=0, reusedCount=reused, warnings=warnings)
    if not missing:
        return
    if not local.available():
        warnings.append("本机尚未准备离线转写。运行“准备离线转写.cmd”后可重试补字幕。")
        return

    total = len(missing)
    labels = [
        f"第 {session['weekNumber']} 周 · {seu.course_time(session) or session['courseId']}"
        for session in missing
    ]
    job.update(index=0, total=total * 100, message=f"待补字幕 {total} 次课。")
    started = time.perf_counter()
    write_raw_export(payload, target)
    add_stage_time(job, "write", time.perf_counter() - started)

    with TemporaryDirectory(prefix="seu-voice-") as temporary, ThreadPoolExecutor(max_workers=1) as audio_pool:
        def prepare_audio(index: int) -> tuple[dict[str, Any] | None, str, float]:
            started = time.perf_counter()
            path = Path(temporary) / f"{index}.wav"
            try:
                urls = seu.fetch_replay_urls(api, missing[index]["courseId"])
                if not urls:
                    return None, "平台没有返回可用的回放地址。", time.perf_counter() - started
                remaining = local.prefetch_replay_audio(urls, path)
                if remaining is None:
                    return None, "未取得完整的教师音轨，请检查校园网和课次回放。", time.perf_counter() - started
            except seu.ApiError as exc:
                reason = "登录或回放权限已失效。" if exc.status in seu.AUTH_ERROR_STATUSES | {403} else "无法请求回放地址。"
                return None, reason, time.perf_counter() - started
            except (OSError, RuntimeError, ValueError):
                return None, "提取回放音轨失败。", time.perf_counter() - started
            return {
                "path": str(path), "temporaryAudio": True, "remainingUrls": remaining,
            }, "", time.perf_counter() - started

        def sources() -> Iterator[tuple[int, dict[str, Any]]]:
            pending = audio_pool.submit(prepare_audio, 0)
            for index in range(total):
                job.update(index=index * 100, message=f"补字幕 {index + 1}/{total} · 正在准备音轨…")
                source, warning, seconds = pending.result()
                add_stage_time(job, "audio", seconds)
                if index + 1 < total:
                    pending = audio_pool.submit(prepare_audio, index + 1)
                if source is None:
                    warnings.append(f"{labels[index]}：{warning}")
                else:
                    yield index, source

        with closing(local.transcribe_sources(sources())) as events:
            for event in events:
                kind = event["kind"]
                if kind == "device":
                    report_device(job, event)
                    continue
                if kind == "fatal":
                    warnings.append(event["message"])
                    break
                index = event["index"]
                prefix = f"补字幕 {index + 1}/{total} · {labels[index]}"
                if kind in {"result", "error"}:
                    add_stage_time(job, "audio", event["audioSeconds"])
                    add_stage_time(job, "asr", event["recognitionSeconds"])
                if kind == "extracting":
                    job.update(message=f"{prefix} · 正在提取音轨…")
                elif kind == "progress":
                    backend = "GPU" if job.get("device") == "cuda" else "CPU"
                    job.update(index=index * 100 + event["percent"],
                               message=f"{prefix} · {backend} 转写 {event['percent']}%")
                elif kind == "result":
                    apply_transcript(missing[index], event["segments"])
                    started = time.perf_counter()
                    write_raw_export(payload, target)
                    add_stage_time(job, "write", time.perf_counter() - started)
                    job.update(transcribedCount=job["transcribedCount"] + 1,
                               index=(index + 1) * 100, message=f"{prefix} · 已写入字幕")
                elif kind == "error":
                    warnings.append(f"{labels[index]}：{event['message']}")
    job["index"] = total * 100


def export_job(job_id: str, config: seu.CourseConfig, output_dir: str, folder_name: str,
               include_empty: bool, auto_transcribe: bool = True) -> None:
    job = JOBS[job_id]

    try:
        job.update(status="running", message="正在读取课程…")
        started = time.perf_counter()
        api, courses = seu.get_authenticated_api(config)
        preview = seu.preview_course(courses, config)
        target_dir = output_directory(output_dir, folder_name, preview.get("subject") or f"tecl_{config.tecl_id}")

        job.update(message="正在读取课堂字幕…", outputDir=str(target_dir), preview=preview)
        payload = seu.build_payload(api, config, courses, progress=job.update)
        add_stage_time(job, "platform", time.perf_counter() - started)
        simplify_captions(payload)
        if auto_transcribe:
            try:
                fill_missing_captions(api, payload, target_dir, job)
            except RuntimeError as exc:
                job["warnings"].append(f"本地补字幕提前结束：{exc}")
        save_export(job, payload, target_dir, include_empty)
    except Exception as exc:
        job.update(status="error", message=str(exc))


def local_inputs(body: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]], Path]:
    source = body.get("sourceJson", "").strip()
    if source:
        source_path = local.local_file(source, {".json"})
        payload, sessions = local.load_export(source_path)
        assignments = []
        assigned = set()
        for entry in body.get("assignments", []):
            index = entry.get("index")
            if type(index) is not int or index in assigned or not 0 <= index < len(sessions):
                raise ValueError("补字幕课次有误，请重新读取导出记录。")
            session = sessions[index]
            if seu.first(session, "courseId", "id") != entry.get("courseId") or segments_to_lines(session):
                raise ValueError("这份导出记录的课次已变动，请重新读取。")
            assignments.append((local.local_file(entry.get("path", ""), local.MEDIA_SUFFIXES), session))
            assigned.add(index)
        subject = seu.clean(seu.first(sessions[0], "subjName", "subjectName", "courseName"))
    else:
        subject = body.get("subject", "").strip() or "本地课程"
        week = body.get("week")
        if type(week) is not int or week < 1:
            raise ValueError("请输入大于 0 的整数周次。")
        files = list(dict.fromkeys(
            local.local_file(value, local.MEDIA_SUFFIXES) for value in body.get("mediaPaths", []) if value.strip()
        ))
        payload = local.standalone_export(subject, week, files)
        assignments = list(zip(files, payload["weeks"]))

    if not assignments:
        raise ValueError("请填写要转写的音视频文件路径。")
    target = output_directory(body.get("outputDir", ""), body.get("folderName", ""), subject)
    if source and (target / "seu_voice_raw.json").resolve() == source_path:
        raise ValueError("请把补字幕的结果保存到新文件夹，以保留原始导出记录。")
    return payload, assignments, target


def local_job(job_id: str, payload: dict[str, Any], assignments: list[tuple[Path, dict[str, Any]]],
              target: Path, include_empty: bool) -> None:
    job = JOBS[job_id]
    warnings: list[str] = []
    successes = 0
    total = len(assignments)
    try:
        job.update(status="running", total=total * 100, message="正在加载本地转写模型…")
        simplify_captions(payload)
        for event in local.transcribe_files([path for path, _ in assignments]):
            kind = event["kind"]
            if kind == "device":
                report_device(job, event)
                continue
            if kind == "fatal":
                raise RuntimeError(event["message"])
            if kind in {"result", "error"}:
                add_stage_time(job, "asr", event["recognitionSeconds"])
            index = event["index"]
            path, session = assignments[index]
            if kind == "progress":
                percent = event["percent"]
                backend = "GPU" if job.get("device") == "cuda" else "CPU"
                job.update(index=index * 100 + percent,
                           message=f"正在转写 {index + 1}/{total} · {path.name} · {backend} {percent}%")
            elif kind == "result":
                apply_transcript(session, event["segments"])
                session["localMediaName"] = path.name
                successes += 1
            elif kind == "error":
                warnings.append(f"{path.name}：{event['message']}")
        if successes + len(warnings) != total:
            raise RuntimeError("转写进程未返回全部结果。")
        if not successes:
            raise RuntimeError("本次没有成功转写的课次。" + "\n".join(warnings))
        payload["extractedAt"] = datetime.now().isoformat(timespec="seconds")
        job.update(index=total * 100, local=True, transcribedCount=successes, warnings=warnings)
        save_export(job, payload, target, include_empty)
    except Exception as exc:
        job.update(status="error", message=str(exc))


def html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>东大云课堂语音导出</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #252824;
      --muted: #60665f;
      --rule: #d8dbd3;
      --green: #264e3c;
      --serif: "Songti SC", "Noto Serif CJK SC", SimSun, serif;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body {
      margin: 0;
      background: #fafbf8;
      color: var(--ink);
      font: 15px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    .sheet { width: min(880px, calc(100% - 64px)); margin: 36px auto 64px; }
    header {
      display: flex; justify-content: space-between; align-items: baseline; gap: 18px;
      padding: 0 0 12px; border-bottom: 2px solid var(--green);
    }
    .site-name { font: 700 18px/1.5 var(--serif); color: var(--green); }
    #loginState { margin: 0; font-size: 13px; color: var(--muted); }
    .title-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 12px; margin: 32px 0 20px; }
    h1 { margin: 0; font: 700 36px/1.35 var(--serif); }
    .byline { font-size: 13px; color: var(--muted); }
    .row {
      display: grid; grid-template-columns: 128px minmax(0, 1fr); gap: 24px;
      border-top: 1px solid var(--rule); padding: 22px 0;
    }
    .row-title { margin: 0; font-size: 15px; font-weight: 600; }
    input, textarea, button { font: inherit; color: inherit; }
    input[type=text], input[type=number], textarea {
      display: block; width: 100%; padding: 9px 11px; background: #fff;
      border: 1px solid #a7ada4; border-radius: 2px;
    }
    textarea { min-height: 76px; resize: vertical; }
    input[type=number] { width: 8em; }
    .local-fields { display: grid; gap: 12px; }
    #assignments { display: grid; gap: 10px; }
    ::placeholder { color: #70766d; opacity: 1; }
    :focus-visible { outline: 2px solid var(--green); outline-offset: 3px; }
    button {
      padding: 6px 16px; background: #fff; border: 1px solid #9ba397;
      border-radius: 2px; cursor: pointer;
    }
    button:hover:enabled { background: #eef1eb; border-color: #657161; }
    button.primary {
      color: #fff; background: var(--green); border-color: var(--green); padding: 8px 23px;
    }
    button.primary:hover:enabled { background: #193e2c; }
    button:disabled { opacity: .55; cursor: default; }
    .link-actions, .choices, .actions {
      display: flex; flex-wrap: wrap; align-items: center; gap: 10px 20px;
    }
    .link-actions { margin-top: 10px; justify-content: space-between; }
    .hint { margin: 0; color: var(--muted); font-size: 13px; }
    .choices { margin: 0 0 12px; }
    .choice { display: inline-flex; align-items: center; gap: 7px; cursor: pointer; }
    .choice input { width: 16px; height: 16px; margin: 0; accent-color: var(--green); }
    .folder-field label { display: block; margin-bottom: 5px; font-size: 13px; color: var(--muted); }
    .actions { margin-top: 18px; }
    .preview { margin-top: 18px; padding-left: 15px; border-left: 2px solid #96a58f; }
    .preview h2 { margin: 0 0 4px; font-size: 17px; }
    .preview p, .feedback p { margin: 4px 0 0; }
    .preview, .feedback { overflow-wrap: anywhere; }
    .preview details { margin-top: 5px; }
    summary { width: fit-content; cursor: pointer; }
    #weeks { display: flex; flex-wrap: wrap; gap: 2px 16px; padding: 4px 0; }
    .feedback { border-top: 1px solid var(--rule); padding: 16px 0; }
    .feedback p:first-child { margin-top: 0; }
    #status { font-weight: 600; white-space: pre-line; }
    .error { color: #963124; }
    progress { display: block; width: 100%; height: 6px; margin: 10px 0; accent-color: var(--green); }
    .files { padding-left: 1.4em; margin: 5px 0; }
    @media (max-width: 680px) {
      .sheet { width: calc(100% - 36px); margin-top: 20px; }
      header { flex-wrap: wrap; gap: 4px 14px; }
      .title-line { margin: 24px 0 16px; }
      h1 { font-size: 30px; }
      .row { grid-template-columns: minmax(0, 1fr); gap: 10px; padding: 17px 0; }
    }
  </style>
</head>
<body>
  <div class="sheet">
    <header>
      <span class="site-name">东大云课堂</span>
      <p id="loginState" role="status">正在检查浏览器登录记录…</p>
    </header>
    <main>
      <div class="title-line">
        <h1>课堂语音导出</h1>
        <span class="byline" lang="en">by Analoop</span>
      </div>
      <div class="row" role="group" aria-labelledby="modeTitle">
        <h2 class="row-title" id="modeTitle">字幕来源</h2>
        <div class="link-actions">
          <label class="choice"><input type="radio" name="mode" id="platformMode" checked>东大云课堂</label>
          <label class="choice"><input type="radio" name="mode" id="localMode">本地音视频转写</label>
        </div>
      </div>
      <div class="row" id="platformFields">
        <label class="row-title" for="url">课程链接</label>
        <div>
          <textarea id="url" rows="2" spellcheck="false" aria-describedby="linkHint" placeholder="粘贴课程播放页的完整链接"></textarea>
          <div class="link-actions">
            <p class="hint" id="linkHint">在 Edge 或 Chrome 打开课程，复制地址栏中带 play-center 的链接。</p>
            <button id="preview">预览课程</button>
          </div>
          <div class="link-actions">
            <label class="choice"><input type="checkbox" id="autoTranscribe" checked>无字幕的回放自动转写</label>
          </div>
          <section class="preview" id="metrics" aria-label="课程预览" hidden>
            <h2 id="mSubject"></h2>
            <p id="mCounts"></p>
            <p class="hint" id="mRange"></p>
            <details id="weekDetails" class="hint"><summary>各周课次</summary><div id="weeks"></div></details>
          </section>
        </div>
      </div>
      <div class="row" id="localFields" hidden>
        <h2 class="row-title">本地文件</h2>
        <div class="local-fields">
          <p class="hint" id="localState" role="status"></p>
          <p class="hint">在资源管理器选中文件，按 Ctrl+Shift+C 复制完整路径。</p>
          <div class="local-fields" id="standaloneFields">
            <div class="folder-field">
              <label for="mediaPaths">音视频文件路径（每行一个）</label>
              <textarea id="mediaPaths" rows="3" placeholder="例如 D:\课程回放\第一节.m4a" spellcheck="false"></textarea>
            </div>
            <div class="folder-field">
              <label for="subject">课程名</label>
              <input type="text" id="subject" placeholder="留空时使用“本地课程”">
            </div>
            <div class="folder-field">
              <label for="week">周次</label>
              <input type="number" id="week" min="1" step="1" value="1">
            </div>
          </div>
          <details id="mergeFields">
            <summary>补字幕到以前的导出记录</summary>
            <div class="local-fields">
              <div class="folder-field">
                <label for="sourceJson">导出的 seu_voice_raw.json 路径</label>
                <input type="text" id="sourceJson" spellcheck="false">
                <div class="link-actions"><button id="loadSource">读取无字幕课次</button></div>
              </div>
              <div id="assignments"></div>
            </div>
          </details>
        </div>
      </div>
      <div class="row" role="group" aria-labelledby="destTitle">
        <h2 class="row-title" id="destTitle">保存位置</h2>
        <div>
          <div class="choices">
            <label class="choice"><input type="radio" name="dest" id="desktopDest" checked>桌面新建文件夹</label>
            <label class="choice"><input type="radio" name="dest" id="customDest">指定文件夹</label>
          </div>
          <div class="folder-field" id="desktopField">
            <label for="folderName">文件夹名称</label>
            <input type="text" id="folderName" placeholder="留空时按课程名和时间命名">
          </div>
          <div class="folder-field" id="customField" hidden>
            <label for="out">文件夹路径</label>
            <input type="text" id="out" placeholder="输入完整路径，例如 D:\课程笔记" spellcheck="false">
          </div>
        </div>
      </div>
      <div class="row">
        <h2 class="row-title">Word 文档</h2>
        <div>
          <label class="choice"><input type="checkbox" id="includeEmpty">没有字幕的周也生成文档</label>
          <div class="actions">
            <button class="primary" id="export">开始导出</button>
            <button id="openFolder" hidden>打开输出文件夹</button>
          </div>
        </div>
      </div>
      <div class="feedback">
        <p id="status" role="status"></p>
        <progress id="bar" aria-label="导出进度" hidden></progress>
        <div id="result"></div>
      </div>
    </main>
  </div>

<script>
const $ = id => document.getElementById(id);
let outputDir = "";
let missingSessions = [];
let loadedSource = "";

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || data.message || "请求失败");
  return data;
}
function setStatus(message, error = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", error);
}
function setBusy(busy) {
  for (const control of document.querySelectorAll("main input, main textarea, #preview, #loadSource, #export")) {
    control.disabled = busy;
  }
}
function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}
function renderPreview(data) {
  $("metrics").hidden = false;
  $("mSubject").textContent = data.subject || "未命名课程";
  $("mCounts").textContent = `共 ${data.courseCount} 次课，其中 ${data.possiblyReadyCount} 次可能有字幕。`;
  $("mRange").textContent = [data.firstTime, data.lastTime].filter(Boolean).join(" 至 ");
  const weeks = data.weeks || [];
  $("weekDetails").hidden = !weeks.length;
  $("weeks").replaceChildren(...weeks.map(w => element("span", `第 ${w.week} 周 · ${w.count} 次`)));
}
function clearResult() {
  outputDir = "";
  $("openFolder").hidden = true;
  $("result").replaceChildren();
  $("bar").hidden = true;
}
function courseLink() {
  const url = $("url").value.trim();
  if (!url) {
    setStatus("请粘贴课程播放页的完整链接。", true);
    $("url").focus();
  }
  return url;
}
async function checkLogin() {
  try {
    const data = await api("/api/session");
    $("loginState").textContent = data.count ? "已检测到浏览器登录记录" : "未检测到登录记录，请在 Edge 或 Chrome 登录";
  } catch {
    $("loginState").textContent = "无法读取浏览器登录记录，可用完整课程链接预览";
  }
}
async function checkLocal() {
  try {
    const data = await api("/api/local-status");
    $("localState").textContent = data.ready
      ? "离线模型已就绪。转写在本机运行。"
      : "请联网运行工具文件夹里的“准备离线转写.cmd”。";
  } catch {
    $("localState").textContent = "无法检查离线模型。";
  }
}
function resetAssignments() {
  loadedSource = "";
  missingSessions = [];
  $("assignments").replaceChildren();
}
async function loadSource() {
  const sourceJson = $("sourceJson").value.trim();
  resetAssignments();
  if (!sourceJson) {
    setStatus("请输入导出记录的完整路径。", true);
    return;
  }
  setBusy(true);
  try {
    const data = await api("/api/local-preview", {sourceJson});
    loadedSource = sourceJson;
    missingSessions = data.sessions;
    for (const session of missingSessions) {
      const field = document.createElement("div");
      field.className = "folder-field";
      const info = [session.week && `第 ${session.week} 周`, session.time,
        session.lesson && `第 ${session.lesson} 节`, `课次 ${session.courseId || session.index + 1}`];
      const label = element("label", info.filter(Boolean).join(" · "));
      const input = document.createElement("input");
      input.type = "text";
      input.id = `media-${session.index}`;
      input.spellcheck = false;
      input.placeholder = "填写对应的音视频文件路径";
      label.htmlFor = input.id;
      field.append(label, input);
      $("assignments").append(field);
    }
    setStatus(missingSessions.length
      ? `找到 ${missingSessions.length} 次无字幕课次。填写本次要补的课次。`
      : "这份导出记录的课次都有字幕。");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    setBusy(false);
  }
}
async function previewCourse() {
  const url = courseLink();
  if (!url) return;
  setBusy(true);
  setStatus("正在读取课程…");
  try {
    const data = await api("/api/preview", {url});
    renderPreview(data.preview);
    setStatus("课程预览已更新。");
  } catch (err) {
    $("metrics").hidden = true;
    setStatus(err.message, true);
  } finally {
    setBusy(false);
  }
}
function showResult(job) {
  outputDir = job.outputDir || "";
  $("openFolder").hidden = !outputDir;
  const result = $("result");
  if (outputDir) result.append(element("p", "保存位置：" + outputDir));
  const docs = job.docs || [];
  if (docs.length) {
    const list = document.createElement("ul");
    list.className = "files";
    list.append(...docs.map(name => element("li", name)));
    result.append(list);
  }
  if (job.local || job.transcribedCount) {
    result.append(element("p", `本地转写了 ${job.transcribedCount} 次课，带时间戳的字幕已写入 ${job.rawJson}。`));
    result.append(element("p", `转写设备：${job.device === "cuda" ? "NVIDIA GPU" : "CPU"}`));
  }
  if (job.reusedCount) result.append(element("p", `复用了 ${job.reusedCount} 次已完成的转写。`));
  if (job.stageSeconds) {
    const stages = [["platform", "查平台字幕"], ["audio", "准备音轨"], ["asr", "转写"],
      ["punctuation", "补标点"], ["write", "写文件"]];
    const times = stages.filter(([key]) => key in job.stageSeconds)
      .map(([key, label]) => {
        const seconds = job.stageSeconds[key];
        const duration = seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒` : `${seconds.toFixed(1)} 秒`;
        return `${label} ${duration}`;
      });
    const timing = element("p", "各阶段累计用时 · " + times.join(" · "));
    timing.className = "hint";
    result.append(timing);
  }
  if (job.emptyCount) {
    result.append(element("p", job.local || job.auto ? `还有 ${job.emptyCount} 次课缺字幕。` : `平台未提供其中 ${job.emptyCount} 次课的字幕。`));
  }
  if (job.deviceWarning) result.append(element("p", job.deviceWarning));
  for (const warning of job.warnings || []) {
    const message = element("p", warning);
    message.className = "error";
    result.append(message);
  }
}
async function startExport() {
  const offline = $("localMode").checked;
  let inputs;
  if (!offline) {
    const url = courseLink();
    if (!url) return;
    inputs = {url, autoTranscribe: $("autoTranscribe").checked};
  } else if ($("mergeFields").open) {
    if (!loadedSource || loadedSource !== $("sourceJson").value.trim()) {
      setStatus("请先读取无字幕课次。", true);
      return;
    }
    const assignments = missingSessions.map(session => ({
      index: session.index, courseId: session.courseId, path: $(`media-${session.index}`).value.trim()
    })).filter(entry => entry.path);
    inputs = {sourceJson: loadedSource, assignments};
  } else {
    inputs = {subject: $("subject").value.trim(), week: $("week").valueAsNumber,
      mediaPaths: $("mediaPaths").value.split(/\r?\n/)};
  }
  const custom = $("customDest").checked;
  const path = $("out").value.trim();
  if (custom && !path) {
    setStatus("请输入保存文件夹的路径。", true);
    $("out").focus();
    return;
  }
  setBusy(true);
  clearResult();
  $("bar").removeAttribute("value");
  $("bar").hidden = false;
  setStatus("正在准备导出…");
  try {
    const {jobId} = await api(offline ? "/api/local-export" : "/api/export", {
      ...inputs, outputDir: custom ? path : "", folderName: custom ? "" : $("folderName").value.trim(),
      includeEmpty: $("includeEmpty").checked
    });
    while (true) {
      const {job} = await api("/api/job?id=" + encodeURIComponent(jobId));
      if (job.status === "done") {
        if (job.preview) renderPreview(job.preview);
        setStatus(job.message || "导出完成。");
        showResult(job);
        break;
      }
      if (job.status === "error" || job.status === "missing") throw new Error(job.message || "导出失败");
      if (job.preview && $("metrics").hidden) renderPreview(job.preview);
      if (job.total) {
        $("bar").max = job.total;
        $("bar").value = job.index || 0;
      }
      setStatus(job.message || "正在导出…");
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    $("bar").hidden = true;
    setBusy(false);
  }
}
function syncDestination() {
  $("desktopField").hidden = $("customDest").checked;
  $("customField").hidden = !$("customDest").checked;
}
function syncMode() {
  const offline = $("localMode").checked;
  $("platformFields").hidden = offline;
  $("localFields").hidden = !offline;
  $("loginState").hidden = offline;
  clearResult();
  setStatus("");
  if (offline) checkLocal();
}
for (const radio of document.querySelectorAll("input[name=mode]")) radio.addEventListener("change", syncMode);
syncMode();
$("mergeFields").addEventListener("toggle", () => {
  $("standaloneFields").hidden = $("mergeFields").open;
});
$("sourceJson").addEventListener("input", resetAssignments);
$("loadSource").addEventListener("click", loadSource);
for (const radio of document.querySelectorAll("input[name=dest]")) radio.addEventListener("change", syncDestination);
syncDestination();
$("url").addEventListener("input", () => {
  $("metrics").hidden = true;
  $("weekDetails").open = false;
  clearResult();
  setStatus("");
});
$("preview").addEventListener("click", previewCourse);
$("export").addEventListener("click", startExport);
$("openFolder").addEventListener("click", async () => {
  try {
    await api("/api/open", {path: outputDir});
  } catch (err) {
    setStatus(err.message, true);
  }
});
checkLogin();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求需要使用 JSON。")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 1024 * 1024:
            raise ValueError("请求长度不正确。")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("请求内容必须是 JSON 对象。")
        return body

    def local_host(self) -> bool:
        port = self.server.server_port
        if self.headers.get("Host", "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self.send_json({"ok": False, "error": "请从本机地址打开工具。"}, 403)
        return False

    def do_GET(self) -> None:
        if not self.local_host():
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/":
                body = html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/session":
                tokens = seu.extract_tokens_from_chrome()
                now = time.time()
                count = sum(int(seu.token_payload(token).get("exp") or 0) > now for token in tokens)
                self.send_json({"ok": True, "count": count})
            elif parsed.path == "/api/local-status":
                self.send_json({"ok": True, "ready": local.available()})
            elif parsed.path == "/api/job":
                params = urllib.parse.parse_qs(parsed.query)
                job_id = (params.get("id") or [""])[0]
                self.send_json({"ok": True, "job": JOBS.get(job_id, {"status": "missing", "message": "任务不存在"})})
            else:
                self.send_json({"ok": False, "error": "not found"}, 404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        if not self.local_host():
            return
        try:
            body = self.read_json()
            if self.path == "/api/preview":
                config = seu.parse_course_url(body.get("url", ""))
                _, courses = seu.get_authenticated_api(config)
                preview = seu.preview_course(courses, config)
                self.send_json({"ok": True, "preview": preview})
            elif self.path == "/api/export":
                config = seu.parse_course_url(body.get("url", ""))
                job_id = queue_export(
                    export_job, config, body.get("outputDir", ""), body.get("folderName", ""),
                    bool(body.get("includeEmpty")), bool(body.get("autoTranscribe", True)),
                )
                self.send_json({"ok": True, "jobId": job_id})
            elif self.path == "/api/local-preview":
                path = local.local_file(body.get("sourceJson", ""), {".json"})
                self.send_json({"ok": True, "sessions": local.offline_preview(path)})
            elif self.path == "/api/local-export":
                if not local.available():
                    raise ValueError("本地转写尚未准备好。请联网运行“准备离线转写.cmd”。")
                payload, assignments, target = local_inputs(body)
                job_id = queue_export(local_job, payload, assignments, target, bool(body.get("includeEmpty")))
                self.send_json({"ok": True, "jobId": job_id})
            elif self.path == "/api/open":
                target = Path(body.get("path", "")).resolve()
                if not target.is_dir():
                    raise ValueError("输出文件夹不存在")
                os.startfile(str(target))
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "not found"}, 404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", APP_PORT), Handler)
    url = f"http://127.0.0.1:{APP_PORT}"
    print(f"东大云课堂语音导出工具已启动：{url}")
    try:
        os.startfile(url)
    except OSError:
        print("请在浏览器中打开上方地址。")
    server.serve_forever()


if __name__ == "__main__":
    main()

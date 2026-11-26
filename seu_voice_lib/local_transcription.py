from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Iterator

from .fetch_seu_voice_from_chrome import first
from .make_voice_docs import format_date_range, segments_to_lines


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".asr" / "venv" / "Scripts" / "python.exe"
MODEL = ROOT / ".asr" / "models" / "small"
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
MODEL_REPO = "Systran/faster-whisper-small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
SOURCE = "faster-whisper"
MEDIA_SUFFIXES = {".aac", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"}
CUDA_LIBRARY_HANDLES: list[Any] = []


def local_file(value: str, suffixes: set[str]) -> Path:
    path = Path(value.strip().strip('"'))
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive):
        raise ValueError("请填写本机文件的完整路径。")
    if path.suffix.lower() not in suffixes:
        raise ValueError(f"不支持的文件格式：{path.name}")
    if not path.is_file():
        raise ValueError(f"找不到文件：{path.name}")
    return path.resolve()


def load_export(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取导出的 JSON 文件。") from exc
    sessions = (payload.get("weeks") or payload.get("sessions")) if isinstance(payload, dict) else None
    if not isinstance(sessions, list) or not sessions or not all(isinstance(item, dict) for item in sessions):
        raise ValueError("请选择工具导出的、包含课次的 seu_voice_raw.json。")
    return payload, sessions


def offline_preview(path: Path) -> list[dict[str, Any]]:
    _, sessions = load_export(path)
    return [
        {
            "index": index,
            "week": first(session, "weekNumber", "weekNo", "week", "schoolWeek"),
            "time": format_date_range(session),
            "lesson": first(session, "letiNumber", "lessonNumber", "section"),
            "courseId": first(session, "courseId", "id"),
        }
        for index, session in enumerate(sessions)
        if not segments_to_lines(session)
    ]


def standalone_export(subject: str, week: int, files: list[Path]) -> dict[str, Any]:
    sessions = [
        {
            "id": f"local-{index:06d}",
            "courseId": f"local-{index:06d}",
            "subjName": subject,
            "week": week,
            "weekNumber": week,
            "localMediaName": path.name,
            "rawSegments": [],
            "mergedSegments": [],
            "captionSource": "none",
            "transcriptText": "",
        }
        for index, path in enumerate(files, start=1)
    ]
    return {
        "source": "本地音视频",
        "weekStart": week,
        "weekEnd": week,
        "extractedAt": datetime.now().isoformat(timespec="seconds"),
        "weeks": sessions,
    }


def available() -> bool:
    return RUNTIME.is_file() and all((MODEL / name).is_file() for name in MODEL_FILES)


def transcribe_files(files: list[Path]) -> Iterator[dict[str, Any]]:
    return transcribe_sources((index, {"path": str(path)}) for index, path in enumerate(files))


def transcribe_sources(sources: Iterable[tuple[int, dict[str, Any]]]) -> Iterator[dict[str, Any]]:
    if not available():
        raise RuntimeError("本地转写尚未准备好。请联网运行“准备离线转写.cmd”。")
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    command = [str(RUNTIME), "-B", "-u", "-X", "utf8", "-m", __name__, "--worker"]
    with subprocess.Popen(
        command, cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            for index, source in sources:
                process.stdin.write(json.dumps({"index": index, **source}, ensure_ascii=False) + "\n")
                process.stdin.flush()
                for line in process.stdout:
                    event = json.loads(line)
                    yield event
                    if event["kind"] in {"result", "error", "fatal"}:
                        break
                else:
                    raise RuntimeError("本地转写进程未返回结果。可运行“准备离线转写.cmd”检查依赖。")
            process.stdin.close()
            if process.wait():
                raise RuntimeError("本地转写进程异常退出。可运行“准备离线转写.cmd”检查依赖。")
        finally:
            if process.poll() is None:
                process.kill()


def configure_cuda_libraries() -> None:
    packages = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    directories = sorted({path.parent for path in packages.rglob("*.dll")})
    for directory in directories:
        CUDA_LIBRARY_HANDLES.append(os.add_dll_directory(str(directory)))
    if directories:
        os.environ["PATH"] = os.pathsep.join([*(str(path) for path in directories), os.environ.get("PATH", "")])


def load_model(device: str = "cpu") -> Any:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    model = WhisperModel(
        str(MODEL), device=device, compute_type="float16" if device == "cuda" else "int8",
        cpu_threads=min(8, os.cpu_count() or 1), local_files_only=True,
    )
    return BatchedInferencePipeline(model=model) if device == "cuda" else model


def prepare() -> None:
    if not all((MODEL / name).is_file() for name in MODEL_FILES):
        from huggingface_hub import snapshot_download

        print("Downloading the multilingual Whisper small model (about 486 MB).", flush=True)
        snapshot_download(MODEL_REPO, revision=MODEL_REVISION, local_dir=MODEL, allow_patterns=list(MODEL_FILES))
    load_model()
    print("Offline transcription is ready.", flush=True)


def recognize(model: Any, path: Path, progress: Callable[[int], None], *, gpu: bool = False) -> list[dict[str, Any]]:
    options = {"batch_size": 8} if gpu else {}
    with path.open("rb") as media:
        try:
            segments, info = model.transcribe(
                media, language="zh", vad_filter=True, condition_on_previous_text=False, **options,
            )
        except IndexError as exc:
            raise ValueError("文件没有可读取的音轨。") from exc
        results: list[dict[str, Any]] = []
        last_percent = 0
        for segment in segments:
            text = segment.text.strip()
            if text:
                results.append({"res": text, "startMs": round(segment.start * 1000), "endMs": round(segment.end * 1000)})
            if info.duration:
                percent = min(99, int(100 * segment.end / info.duration))
                if percent > last_percent:
                    progress(percent)
                    last_percent = percent
    return results


def extract_replay_audio(url: str, destination: Path) -> None:
    import av

    options = {
        "headers": "Referer: https://cvs.seu.edu.cn/\r\n",
        "user_agent": "Mozilla/5.0",
        "tls_verify": "1",
        "protocol_whitelist": "http,https,tcp,tls,crypto",
        "reconnect": "1",
        "reconnect_on_network_error": "1",
        "reconnect_delay_max": "2",
    }
    with av.open(url, timeout=(15, 30), options=options) as source:
        if not source.streams.audio:
            raise ValueError("该回放线路没有音轨。")
        track = source.streams.audio[0]
        expected = float(track.duration * track.time_base) if track.duration and track.time_base else 0
        sample_count = 0
        with av.open(str(destination), mode="w", format="wav") as output:
            audio = output.add_stream("pcm_s16le", rate=16000)
            audio.layout = "mono"
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in source.decode(audio=0):
                frame.pts = None
                for converted in resampler.resample(frame):
                    sample_count += converted.samples
                    output.mux(audio.encode(converted))
            for converted in resampler.resample(None):
                sample_count += converted.samples
                output.mux(audio.encode(converted))
            output.mux(audio.encode())
        if not sample_count or expected - sample_count / 16000 > max(5, expected * 0.02):
            raise ValueError("回放音轨为空或下载不完整。")


def extract_first_audio(urls: list[str], destination: Path) -> list[str] | None:
    for index, url in enumerate(urls):
        try:
            extract_replay_audio(url, destination)
        except (OSError, ValueError):
            destination.unlink(missing_ok=True)
        else:
            return urls[index + 1:]
    return None


def prefetch_replay_audio(urls: list[str], destination: Path) -> list[str] | None:
    command = [str(RUNTIME), "-B", "-X", "utf8", "-m", __name__, "--extract"]
    process = subprocess.run(
        command, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW, encoding="utf-8", capture_output=True,
        input=json.dumps({"urls": urls, "path": str(destination)}, ensure_ascii=False),
    )
    if process.returncode:
        raise RuntimeError("提取回放音轨的进程异常退出。")
    return json.loads(process.stdout)


def emit(**event: Any) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def worker() -> None:
    model = None
    device = "cpu"

    def run(path: Path, index: int) -> list[dict[str, Any]]:
        nonlocal model, device
        emit(kind="progress", index=index, percent=0)

        def progress(percent: int) -> None:
            emit(kind="progress", index=index, percent=percent)

        try:
            return recognize(model, path, progress, gpu=device == "cuda")
        except RuntimeError:
            if device != "cuda":
                raise
            emit(kind="device", device="cpu", warning="GPU 转写遇到异常，已自动改用 CPU。")
            device = "cpu"
            model = load_model()
            return recognize(model, path, progress)

    for line in sys.stdin:
        source = json.loads(line)
        index = source["index"]
        started = perf_counter()
        audio_seconds = 0.0
        if model is None:
            try:
                if os.environ.get("SEU_ASR_DEVICE") != "cpu":
                    configure_cuda_libraries()
                    import ctranslate2

                    if ctranslate2.get_cuda_device_count():
                        try:
                            model = load_model("cuda")
                            device = "cuda"
                        except (OSError, RuntimeError, ValueError):
                            emit(kind="device", device="cpu", warning="GPU 初始化失败，已改用 CPU。")
                if model is None:
                    model = load_model()
                emit(kind="device", device=device)
            except Exception as exc:
                emit(kind="fatal", message=f"无法加载本地转写模型：{exc}")
                return

        try:
            path = Path(source["path"])
            temporary = source.get("temporaryAudio", False)
            try:
                segments = run(path, index)
                remaining = source.get("remainingUrls", [])
                while not segments and remaining:
                    emit(kind="extracting", index=index)
                    extraction_started = perf_counter()
                    try:
                        remaining = extract_first_audio(remaining, path)
                    finally:
                        audio_seconds += perf_counter() - extraction_started
                    if remaining is not None:
                        segments = run(path, index)
            finally:
                if temporary:
                    path.unlink(missing_ok=True)
            if not segments:
                raise ValueError("未识别到语音，请检查教师音轨。")
        except (OSError, ValueError, RuntimeError) as exc:
            outcome = {"kind": "error", "message": str(exc)}
        else:
            outcome = {"kind": "result", "segments": segments}
        emit(index=index, audioSeconds=audio_seconds, recognitionSeconds=perf_counter() - started - audio_seconds,
             **outcome)


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare"]:
        prepare()
    elif sys.argv[1:] == ["--worker"]:
        worker()
    elif sys.argv[1:] == ["--extract"]:
        task = json.load(sys.stdin)
        print(json.dumps(extract_first_audio(task["urls"], Path(task["path"]))))
    else:
        raise SystemExit("Specify --prepare, --worker or --extract.")

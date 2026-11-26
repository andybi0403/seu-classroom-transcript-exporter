from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .local_transcription import ROOT, RUNTIME, SOURCE
from .make_voice_docs import segments_to_lines


MODEL_DIRNAME = "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"
MODEL = ROOT / ".asr" / "models" / "punctuation.int8.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/{MODEL_DIRNAME}.tar.bz2"
ARCHIVE_SHA256 = "c0d5aa5f8eeb686032345e180bedf39319dc2e0556781c6264bcadba8328a6e1"
MARKS = "，。！？、；：,.!?;:"
FULLWIDTH_MARKS = str.maketrans(",.!?;:", "，。！？；：")


def prepare(archive: Path | None = None) -> None:
    if not MODEL.is_file():
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=MODEL.parent) as work:
            if archive is None:
                archive = Path(work) / "model.tar.bz2"
                print("Downloading the offline Chinese punctuation model (about 65 MB).", flush=True)
                with urllib.request.urlopen(MODEL_URL, timeout=60) as response, archive.open("wb") as output:
                    shutil.copyfileobj(response, output)
            digest = hashlib.sha256()
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != ARCHIVE_SHA256:
                raise RuntimeError("The punctuation model archive did not match its SHA-256 digest.")
            pending = Path(work) / MODEL.name
            with tarfile.open(archive, mode="r:bz2") as packed:
                model_file = packed.extractfile(f"{MODEL_DIRNAME}/model.int8.onnx")
                if model_file is None:
                    raise RuntimeError("The punctuation model archive contained no model.")
                with model_file, pending.open("wb") as output:
                    shutil.copyfileobj(model_file, output)
            pending.replace(MODEL)
    if not load_model().add_punctuation("请大家看这一页我们继续上课"):
        raise RuntimeError("The punctuation model did not return a result.")
    print("Offline punctuation is ready.", flush=True)


def load_model() -> Any:
    import sherpa_onnx

    # The Windows native loader does not accept Unicode absolute paths.
    # Both subprocess entry points run from ROOT, so use the ASCII relative path.
    return sherpa_onnx.OfflinePunctuation(sherpa_onnx.OfflinePunctuationConfig(
        model=sherpa_onnx.OfflinePunctuationModelConfig(
            ct_transformer=str(MODEL.relative_to(ROOT)), num_threads=2, provider="cpu",
        ),
    ))


def punctuation_offsets(text: str) -> tuple[list[str], dict[int, str]]:
    characters: list[str] = []
    marks: dict[int, str] = {}
    for character in text:
        if character in MARKS:
            marks[len(characters)] = character
        elif not character.isspace():
            characters.append(character.casefold())
    return characters, marks


def transfer_marks(source: str, suggestion: str, *, final: bool) -> str | None:
    characters, existing = punctuation_offsets(source)
    predicted, marks = punctuation_offsets(suggestion)
    if predicted != characters:
        return None
    if not final:
        marks.pop(len(characters), None)

    result: list[str] = []
    offset = 0
    for index, character in enumerate(source):
        result.append(character)
        if character in MARKS or character.isspace():
            continue
        offset += 1
        mark = marks.get(offset)
        if mark and offset not in existing:
            following = source[index + 1:index + 2]
            if character.isascii() and character.isalnum() and following.isascii() and following.isalnum():
                continue
            result.append(mark.translate(FULLWIDTH_MARKS))
    return "".join(result)


def punctuate_lesson(model: Any, lines: list[str]) -> str:
    blocks: list[list[str]] = [[]]
    length = 0
    for line in lines:
        if blocks[-1] and length + len(line) > 900:
            blocks.append([])
            length = 0
        blocks[-1].append(line)
        length += len(line)

    output: list[str] = []
    for index, block in enumerate(blocks):
        text = "\n".join(block)
        final = index == len(blocks) - 1
        restored = transfer_marks(text, model.add_punctuation(text), final=final)
        if restored is None:
            for line_index, line in enumerate(block):
                terminal = final and line_index == len(block) - 1
                line_restored = transfer_marks(line, model.add_punctuation(line), final=terminal)
                output.append(line_restored if line_restored is not None else line)
        else:
            output.append(restored)
    return "\n".join(output)


def add_to_payload(payload: dict[str, Any]) -> int:
    pending: list[tuple[dict[str, Any], list[str]]] = []
    for session in payload.get("weeks") or payload.get("sessions") or []:
        if session.get("captionSource") == SOURCE and not session.get("punctuatedText"):
            lines = segments_to_lines(session)
            if lines:
                pending.append((session, lines))
    if not pending:
        return 0
    if not MODEL.is_file() or not RUNTIME.is_file():
        raise RuntimeError("标点模型尚未准备，请联网运行“准备离线转写.cmd”。")
    command = [str(RUNTIME), "-B", "-X", "utf8", "-m", __name__, "--punctuate"]
    try:
        process = subprocess.run(
            command, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW, encoding="utf-8",
            input=json.dumps([lines for _, lines in pending], ensure_ascii=False), capture_output=True,
        )
    except OSError as exc:
        raise RuntimeError("无法启动标点模型。可运行“准备离线转写.cmd”检查转写环境。") from exc
    if process.returncode:
        raise RuntimeError("自动添加标点失败。可运行“准备离线转写.cmd”检查标点模型。")
    try:
        output = json.loads(process.stdout)
    except ValueError as exc:
        raise RuntimeError("标点模型返回的结果无法读取。") from exc
    if not isinstance(output, list) or len(output) != len(pending) or not all(isinstance(text, str) for text in output):
        raise RuntimeError("标点模型返回的课次不完整。")
    for (session, _), text in zip(pending, output):
        session["punctuatedText"] = text
    return len(pending)


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare"]:
        prepare()
    elif sys.argv[1:] == ["--punctuate"]:
        lessons = json.load(sys.stdin)
        model = load_model()
        print(json.dumps([punctuate_lesson(model, lines) for lines in lessons], ensure_ascii=False))
    else:
        raise SystemExit("Specify --prepare or --punctuate.")

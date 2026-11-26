from __future__ import annotations

import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from .fetch_seu_voice_from_chrome import first, format_datetime_value
from .simplified_chinese import simplified


START_KEYS = ("courBeginTime", "courseStarttime", "startTime", "beginTime")
LESSON_KEYS = ("letiNumber", "lessonNumber", "section")
DOC_AUTHOR = "HZL不凉"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def session_order(session: dict[str, Any]) -> tuple:
    start = clean_text(format_datetime_value(first(session, *START_KEYS)))
    try:
        time = datetime.fromisoformat(start[:19]).replace(tzinfo=None)
    except ValueError:
        time = datetime.max
    return time, clean_text(first(session, *LESSON_KEYS)), clean_text(first(session, "courseId", "id"))


def docx_run(text: str, *, bold: bool = False, size: int = 21) -> str:
    props = [
        '<w:rFonts w:ascii="Microsoft YaHei" w:hAnsi="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:cs="Microsoft YaHei"/>',
        f'<w:sz w:val="{size}"/>',
        f'<w:szCs w:val="{size}"/>',
        '<w:lang w:eastAsia="zh-CN"/>',
    ]
    if bold:
        props.insert(0, "<w:b/>")
    chunks = simplified(clean_text(text)).split("\n")
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(chunk)}</w:t>' for chunk in chunks)
    return f"<w:r><w:rPr>{''.join(props)}</w:rPr>{body}</w:r>"


def docx_paragraph(text: str = "", *, bold: bool = False, size: int = 21, before: int = 0, after: int = 120) -> str:
    spacing = f'<w:spacing w:before="{before}" w:after="{after}"/>'
    return f"<w:p><w:pPr>{spacing}</w:pPr>{docx_run(text, bold=bold, size=size)}</w:p>"


def docx_cell(text: str, *, width: int, bold: bool = False) -> str:
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
        f'{docx_paragraph(text, bold=bold)}'
        "</w:tc>"
    )


def docx_table(rows: list[tuple[str, str]]) -> str:
    table_rows = []
    for label, value in rows:
        table_rows.append(
            "<w:tr>"
            f"{docx_cell(label, bold=True, width=1800)}"
            f"{docx_cell(value, width=7600)}"
            "</w:tr>"
        )
    return (
        "<w:tbl>"
        "<w:tblPr>"
        '<w:tblW w:w="0" w:type="auto"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:left w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:right w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="auto"/></w:tblBorders>'
        "</w:tblPr>"
        f"{''.join(table_rows)}"
        "</w:tbl>"
    )


def docx_document(body_parts: list[str]) -> str:
    body = "".join(body_parts)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body>
</w:document>"""


def write_docx(path: Path, body_parts: list[str]) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>{escape(DOC_AUTHOR)}</dc:creator>
  <cp:lastModifiedBy>{escape(DOC_AUTHOR)}</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>"""
    app = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>{escape(DOC_AUTHOR)}</Application>
</Properties>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
        archive.writestr("word/document.xml", docx_document(body_parts))


def segments_to_lines(session: dict[str, Any]) -> list[str]:
    segments = session.get("rawSegments") or session.get("mergedSegments") or []
    lines: list[str] = []
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict):
                text = clean_text(
                    segment.get("res")
                    or segment.get("zh")
                    or segment.get("text")
                    or segment.get("content")
                    or segment.get("caption")
                )
            else:
                text = clean_text(segment)
            if text:
                lines.append(text)
    if lines:
        return lines

    transcript = clean_text(session.get("transcriptText"))
    return [line.strip() for line in transcript.splitlines() if line.strip()]


def format_date_range(session: dict[str, Any]) -> str:
    start = clean_text(format_datetime_value(first(session, *START_KEYS)))
    end = clean_text(format_datetime_value(first(session, "courEndTime", "courseEndtime", "endTime")))
    if start and end:
        return f"{start} - {end}"
    return start or end


def session_title(session: dict[str, Any]) -> str:
    start = clean_text(format_datetime_value(first(session, *START_KEYS)))
    if not start and session.get("localMediaName"):
        return Path(clean_text(session["localMediaName"])).stem
    date_part = start[:10] if start else "未标注日期"
    leti = clean_text(first(session, *LESSON_KEYS))
    if leti:
        return f"{date_part} 第{leti}节"
    return date_part


def add_transcript(body_parts: list[str], lines: list[str]) -> None:
    if not lines:
        body_parts.append(docx_paragraph("（本节未提取到课堂语音原话。）"))
        return

    buffer: list[str] = []
    char_count = 0
    for line in lines:
        buffer.append(line)
        char_count += len(line)
        if len(buffer) >= 12 or char_count >= 700:
            body_parts.append(docx_paragraph("\n".join(buffer)))
            buffer.clear()
            char_count = 0
    if buffer:
        body_parts.append(docx_paragraph("\n".join(buffer)))


def save_week_doc(
    week_number: int,
    sessions: list[tuple[dict[str, Any], list[str]]],
    source: dict[str, Any],
    output_dir: Path,
) -> Path:
    body_parts: list[str] = [docx_paragraph(f"第{week_number:02d}周 课堂语音原话", bold=True, size=36, after=240)]

    subject = clean_text(first(sessions[0][0], "subjName", "subjectName", "courseName"))
    body_parts.append(
        docx_table(
            [
                ("课程", subject),
                ("来源", clean_text(source.get("source"))),
                ("提取时间", clean_text(source.get("extractedAt"))),
                ("本周课次数", str(len(sessions))),
            ]
        )
    )

    for idx, (session, lines) in enumerate(sessions, start=1):
        body_parts.append(docx_paragraph(""))
        body_parts.append(docx_paragraph(f"{idx}. {session_title(session)}", bold=True, size=28, before=120, after=120))

        location = clean_text(first(session, "classRoomName", "clroName", "classroom", "roomName"))
        teacher = clean_text(first(session, "tecName", "teacherName", "teacher"))
        course_id = clean_text(first(session, "courseId", "id"))
        meta = [
            ("时间", format_date_range(session)),
            ("教师", teacher),
            ("地点", location),
            ("课次ID", course_id),
            ("本地音视频", clean_text(session.get("localMediaName"))),
        ]
        body_parts.append(docx_table([(k, v) for k, v in meta if v]))

        heading = "本地语音转写" if session.get("captionSource") == "faster-whisper" else "语音原话"
        body_parts.append(docx_paragraph(heading, bold=True, size=24, before=160, after=80))
        add_transcript(body_parts, lines)

        if idx != len(sessions):
            body_parts.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    output = output_dir / f"第{week_number:02d}周_课堂语音原话.docx"
    write_docx(output, body_parts)
    return output


def normalize_weeks(payload: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    sessions = payload.get("weeks") or payload.get("sessions") or []
    if not isinstance(sessions, list):
        raise ValueError("JSON does not contain a weeks/sessions list")

    sessions = sorted((s for s in sessions if isinstance(s, dict)), key=session_order)
    labels = [clean_text(first(s, "weekNo", "week", "schoolWeek")) for s in sessions]
    week_numbers = [int(label) if label.isdecimal() else None for label in labels]
    if not any(n is not None for n in week_numbers):
        fallback_labels = [clean_text(s.get("weekNumber")) for s in sessions]
        week_numbers = [int(n) if n.isdecimal() and int(n) > 0 else None for n in fallback_labels]

    groups = defaultdict(list)
    known = [n for n in week_numbers if n is not None]
    if known:
        last_week = max(known)
        for number, session in zip(week_numbers, sessions):
            groups[number if number is not None else last_week].append(session)
    else:
        keys = [label or clean_text(first(s, *START_KEYS))[:10] for label, s in zip(labels, sessions)]
        index = {key: i for i, key in enumerate(dict.fromkeys(filter(None, keys)), start=1)}
        for key, session in zip(keys, sessions):
            groups[index.get(key, len(groups) + 1)].append(session)
    return groups


def generate_docs(payload: dict[str, Any], output_dir: Path, include_empty: bool = False) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for week, sessions in sorted(normalize_weeks(payload).items()):
        lessons = []
        for session in sessions:
            restored = clean_text(session.get("punctuatedText"))
            lines = restored.splitlines() if restored else segments_to_lines(session)
            if lines or include_empty:
                lessons.append((session, lines))
        if lessons:
            outputs.append(save_week_doc(week, lessons, payload, output_dir))
    return outputs

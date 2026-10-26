"""No remote inference or caller-controlled filesystem paths during parsing."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from apps.api.errors import APIError

PARSE_LOCK = threading.BoundedSemaphore(1)
LABELS = {
    "title": ("Title", "制度名称"),
    "legal_entity": ("Company", "公司"),
    "region": ("Region", "地区"),
    "expense_type": ("Expense Type", "费用类型"),
    "version": ("Version", "制度版本"),
    "effective_from": ("Effective From", "生效日期"),
    "effective_to": ("Effective To", "失效日期"),
}


def extract_metadata(lines):
    result = {}
    for field, labels in LABELS.items():
        candidates = []
        pattern = r"^(?:" + "|".join(re.escape(label) for label in labels) + r")\s*[:：]\s*(.+)$"
        for line in lines:
            match = re.match(pattern, line["text"].strip(), re.IGNORECASE)
            if match and (line.get("confidence") is None or line["confidence"] >= 0.85):
                candidates.append((match[1].strip(), line))
        unique = {value for value, _ in candidates}
        value = next(iter(unique)) if len(unique) == 1 else "UNKNOWN"
        if len(value) > (200 if field == "title" else 128):
            value = "UNKNOWN"
        if field.startswith("effective_") and value != "UNKNOWN":
            from datetime import date

            try:
                date.fromisoformat(value)
            except ValueError:
                value = "UNKNOWN"
        if field == "expense_type" and value not in {"HOTEL", "MEAL", "TRANSPORT", "OTHER"}:
            value = "UNKNOWN"
        result[field] = {"value": value, "source": candidates[0][1] if value != "UNKNOWN" else None}
    return result


class PolicyParser:
    def __init__(self, processor):
        self.processor = processor

    def parse(self, raw: bytes, filename: str):
        extension = Path(filename).suffix.lower()
        if extension not in {".pdf", ".docx"} or not 0 < len(raw) <= 10 * 1024 * 1024:
            raise APIError(422, "INVALID_FILE", "只接受 10 MiB 内的真实 PDF / DOCX 文件")
        if not PARSE_LOCK.acquire(blocking=False):
            raise APIError(503, "PARSER_BUSY", "本机解析器正忙，请稍后重试")
        try:
            with TemporaryDirectory(prefix="fde-policy-") as folder:
                os.chmod(folder, 0o700)
                path = Path(folder) / ("document" + extension)
                path.write_bytes(raw)
                path.chmod(0o600)
                config = dict(
                    path=str(path),
                    extension=extension,
                    binary=self.processor.binary,
                    languages=self.processor.languages,
                    tessdata=self.processor.tessdata,
                )
                with subprocess.Popen(
                    [sys.executable, "-m", "integrations.policies.worker"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    start_new_session=True,
                ) as process:
                    deadline = time.monotonic() + 100
                    payload = json.dumps(config)
                    while True:
                        try:
                            stdout, _ = process.communicate(payload, timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            payload = None
                            # macOS does not enforce RLIMIT_AS. Monitor the complete private
                            # worker process group, including Tesseract, and cap resident RAM.
                            try:
                                usage = subprocess.run(
                                    ["ps", "-axo", "pgid=,rss="],
                                    capture_output=True,
                                    text=True,
                                    encoding="utf-8",
                                    timeout=3,
                                    check=True,
                                ).stdout
                            except (OSError, subprocess.SubprocessError) as exc:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.communicate()
                                raise APIError(
                                    503, "PARSER_UNAVAILABLE", "无法核对解析资源限制"
                                ) from exc
                            resident_kib = sum(
                                int(parts[1])
                                for row in usage.splitlines()
                                if len(parts := row.split()) == 2 and parts[0] == str(process.pid)
                            )
                            if time.monotonic() >= deadline or resident_kib > 768 * 1024:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.communicate()
                                raise APIError(422, "FILE_COMPLEXITY_LIMIT", "解析超过资源限制")
                    if process.returncode or len(stdout) > 4 * 1024 * 1024:
                        raise APIError(422, "INVALID_FILE", "文件内容损坏或格式不支持")
                    parsed = json.loads(stdout)
        except (OSError, ValueError) as exc:
            raise APIError(503, "PARSER_UNAVAILABLE", "本机解析器不可用") from exc
        finally:
            PARSE_LOCK.release()
        if parsed.get("error"):
            code = parsed["error"]
            raise APIError(
                503 if code == "OCR_UNAVAILABLE" else 422,
                code,
                "本地 OCR 不可用"
                if code == "OCR_UNAVAILABLE"
                else "文件内容损坏、无文本或超过解析资源限制",
            )
        parsed["extracted_metadata"] = extract_metadata(parsed["lines"])
        return parsed

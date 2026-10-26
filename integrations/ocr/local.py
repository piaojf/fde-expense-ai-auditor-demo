import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

from apps.api.errors import APIError
from domain.invoices import OCRResult
from integrations.ocr.fields import extract_fields


class LocalOCRProvider:
    def __init__(self, *, binary="tesseract", languages="eng", tessdata=None):
        self.binary = shutil.which(binary) or binary
        self.languages = languages
        self.tessdata = str(tessdata) if tessdata else None

    def worker(self, path, mode):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "integrations.ocr.worker"],
                input=json.dumps(
                    dict(
                        path=str(Path(path).resolve()),
                        mode=mode,
                        binary=self.binary,
                        languages=self.languages,
                        tessdata=self.tessdata,
                    )
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=100,
                check=False,
            )
            data = (
                json.loads(result.stdout) if result.returncode == 0 else {"error": "INVALID_FILE"}
            )
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            raise APIError(503, "OCR_UNAVAILABLE", "Local document processor unavailable") from exc
        if data.get("error"):
            code = data["error"]
            raise APIError(
                503 if code == "OCR_UNAVAILABLE" else 422,
                code,
                "OCR unavailable" if code == "OCR_UNAVAILABLE" else "Invalid document",
            )
        return data

    def validate(self, path):
        return self.worker(path, "validate")

    def preview(self, path):
        return base64.b64decode(self.worker(path, "preview")["preview"])

    def recognize(self, path: str) -> OCRResult:
        data = self.worker(path, "ocr")
        version = "not-invoked"
        if "tesseract" in data["methods"]:
            try:
                version = subprocess.run(
                    [self.binary, "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=5,
                    check=True,
                ).stdout.splitlines()[0]
            except (OSError, subprocess.SubprocessError) as exc:
                raise APIError(503, "OCR_UNAVAILABLE", "OCR unavailable") from exc
        return OCRResult(
            provider="local-hybrid",
            engine_version=version,
            methods=data["methods"],
            fields=extract_fields(data["lines"]),
            lines=data["lines"],
        )

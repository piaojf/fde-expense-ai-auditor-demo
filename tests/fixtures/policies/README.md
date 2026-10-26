# Synthetic policy fixtures

Only artificial demo policies; no real internal business documents. New draft says Shenzhen CNY 800/night; current ACTIVE authority remains CNY 700. PDF embeds text; DOCX is a minimal real OOXML package; scan PDF contains pixels only and requires genuine local Tesseract. Scan English region `Shenzhen` is explicitly corrected by administrator to `深圳` in acceptance, never inferred by runtime code.

`python scripts/generate_policy_fixtures.py` uses bundled reportlab/Pillow. DOCX page count is UNKNOWN because no Office layout engine runs. Structured fields are unambiguous labels; scan OCR still requires human confirmation.

UI-P3B `publication-800.docx` is a synthetic HOTEL policy for DEMO-CN / 深圳, version
2026-11-800, effective [2026-11-01, 2027-01-01), CNY 800/night. Generated only from
the existing synthetic OOXML fixture by `scripts/generate_publication_fixture.py`;
contains no real enterprise document, credential or database.

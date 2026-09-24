"""Pick the best resume variant (built by build_resumes.py) for a job."""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import yaml

from .filters import contains_term

log = logging.getLogger("resumes")


class ResumePicker:
    """Scores every variant's keywords against the job title (x3 per word in the keyword, so
    specific phrases like 'data scientist' beat generic ones like 'nlp') and description (+1)."""

    def __init__(self, root: Path, default_pdf: str | Path | None = None, upload_dir: Path | None = None):
        self.dir = Path(root) / "resumes"
        self.default = (Path(root) / default_pdf) if default_pdf else None
        self.upload_dir = Path(upload_dir or Path(root) / "data" / "upload")
        self.variants: dict[str, list[str]] = {}
        self.file_name = "Resume.pdf"
        content = self.dir / "content.yaml"
        if content.exists():
            data = yaml.safe_load(content.read_text(encoding="utf-8")) or {}
            name = ((data.get("header") or {}).get("name") or "").strip()
            if name:
                self.file_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") + "_Resume.pdf"
            for variant, v in (data.get("variants") or {}).items():
                if (self.dir / f"{variant}.pdf").exists():
                    self.variants[variant] = [str(k).lower() for k in v.get("keywords") or []]

    def choose(self, title: str, description: str = "") -> str | None:
        """Name of the best variant (None when no variants are built)."""
        if not self.variants:
            return None
        scores = {}
        for variant, keywords in self.variants.items():
            score = 0
            for k in keywords:
                if contains_term(title or "", k):
                    score += 3 * len(k.split())
                if description and contains_term(description, k):
                    score += 1
            scores[variant] = score
        best = max(scores, key=scores.get)  # ties -> earlier variant in content.yaml
        return best if scores[best] > 0 else next(iter(self.variants))

    def pick(self, title: str, description: str = "") -> Path | None:
        """Path of the PDF to upload for this job, copied under a neutral file name
        (recruiters see '<Your_Name>_Resume.pdf', not 'ai_ml_engineer.pdf')."""
        variant = self.choose(title, description)
        if not variant:
            return self.default if self.default and self.default.exists() else None
        target_dir = self.upload_dir / variant
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / self.file_name
        source = self.dir / f"{variant}.pdf"
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            shutil.copy2(source, target)
        log.info("   resume: %s", variant)
        return target

"""Job folders and their metadata (job.json), so each source keeps its own outputs."""
import hashlib
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from .config import JOBS_DIR, ClipperError


def slugify(text, limit=40):
    slug = re.sub(r"[\W_]+", "-", text).strip("-").lower()  # \W is Unicode-aware: Arabic titles survive
    return slug[:limit].strip("-") or "video"


def job_id_for_url(extractor, video_id):
    return f"{slugify(extractor, 12)}-{slugify(video_id, 32)}"


def job_id_for_file(path):
    p = Path(path)
    stat = p.stat()
    digest = hashlib.sha1(f"{p.name}|{stat.st_size}".encode()).hexdigest()[:10]
    return f"file-{slugify(p.stem, 30)}-{digest}"


class Job:
    def __init__(self, job_id, jobs_dir=None):
        self.id = job_id
        self.dir = Path(jobs_dir or JOBS_DIR) / job_id
        self.meta_file = self.dir / "job.json"
        self.data = json.loads(self.meta_file.read_text(encoding="utf-8")) if self.meta_file.exists() else {
            "id": job_id,
            "created": _now(),
            "status": "new",
            "source": {},
            "candidates": [],
            "notes": "",
        }

    @classmethod
    def create(cls, job_id, source, jobs_dir=None):
        job = cls(job_id, jobs_dir)
        job.dir.mkdir(parents=True, exist_ok=True)
        job.data["source"].update(source)
        job.save()
        return job

    @staticmethod
    def list_all(jobs_dir=None):
        root = Path(jobs_dir or JOBS_DIR)
        jobs = [Job(f.parent.name, root) for f in root.glob("*/job.json")]
        return sorted(jobs, key=lambda j: j.data.get("updated", ""), reverse=True)

    # paths
    @property
    def transcript_file(self):
        return self.dir / "transcript.json"

    @property
    def renders_dir(self):
        return self.dir / "renders"

    @property
    def export_dir(self):
        return self.dir / "export"

    @property
    def log_file(self):
        return self.dir / "job.log"

    def save(self):
        self.data["updated"] = _now()
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.meta_file)  # atomic, so a crash never leaves half a job.json

    def log(self, message):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {message}\n")

    def read_log(self, lines=200):
        if not self.log_file.exists():
            return ""
        return "\n".join(self.log_file.read_text(encoding="utf-8").splitlines()[-lines:])

    def set_status(self, status, error=None):
        self.data["status"] = status
        self.data["error"] = error
        self.save()

    def candidate(self, cid):
        for c in self.data["candidates"]:
            if c["id"] == cid:
                return c
        raise ClipperError(f"Clip {cid} not found in job {self.id}.")

    def load_transcript(self):
        if self.transcript_file.exists():
            return json.loads(self.transcript_file.read_text(encoding="utf-8"))
        return None

    def save_transcript(self, transcript):
        self.transcript_file.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")

    def export(self):
        """Copy selected, rendered clips plus metadata into export/ and zip it. Returns the zip path."""
        chosen = [c for c in self.data["candidates"] if c.get("selected")]
        if not chosen:
            raise ClipperError("No clips are selected.")
        missing = [c["id"] for c in chosen if not _rendered_file(self, c)]
        if missing:
            raise ClipperError(f"Render these selected clips first: {', '.join(missing)}")
        if self.export_dir.exists():
            shutil.rmtree(self.export_dir)
        self.export_dir.mkdir(parents=True)
        from . import campaigns
        from .config import Settings

        settings = Settings.from_dict(self.data.get("settings"))
        try:
            campaign = campaigns.for_settings(settings)
        except ClipperError:
            campaign = None  # the campaign file was deleted since; export without the checklist
        if campaign:
            settings = campaigns.apply_settings(settings, campaign)
        exported, checklist = [], []
        for n, c in enumerate(chosen, 1):
            name = f"{n:02d}-{slugify(c.get('title') or c['id'])}.mp4"
            shutil.copy2(_rendered_file(self, c), self.export_dir / name)
            checks = campaigns.check(c, campaign, self.data["source"], settings)
            exported.append({"file": name, **{k: v for k, v in c.items() if k != "render"},
                             **({"compliance": [{"status": s, "rule": t} for s, t in checks]} if campaign else {})})
            if campaign:
                marks = {"ok": "[OK]   ", "fail": "[FAIL] ", "manual": "[CHECK]"}
                checklist += [f"{name}  ({campaigns.summary(checks)})"] + [f"  {marks[s]} {t}" for s, t in checks] + [""]
            caption = c.get("caption", "")
            in_caption = {t.lower() for t in re.findall(r"#\w+", caption)}
            extra = [t for t in c.get("hashtags", []) if t.lower() not in in_caption]  # no tag twice
            (self.export_dir / (Path(name).stem + ".caption.txt")).write_text(
                f"{caption}\n\n{' '.join(extra)}\n" if extra else f"{caption}\n", encoding="utf-8"
            )
        meta = {
            "job": self.id,
            "source": self.data["source"],
            "settings": self.data.get("settings", {}),
            "exported": _now(),
            "campaign": campaign["name"] if campaign else None,
            "clips": exported,
        }
        if campaign:
            (self.export_dir / "CHECKLIST.txt").write_text(
                f"Campaign: {campaign['name']}\nCheck every [FAIL] and [CHECK] line before you post.\n\n"
                + "\n".join(checklist), encoding="utf-8")
        (self.export_dir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        zip_path = self.dir / f"{self.id}-clips.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(self.export_dir.iterdir()):
                z.write(f, f.name)
        self.log(f"Exported {len(exported)} clips to {self.export_dir}")
        return zip_path


def _rendered_file(job, c):
    f = (c.get("render") or {}).get("file")
    return job.dir / f if f and (job.dir / f).exists() else None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

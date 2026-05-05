#!/usr/bin/env python3
"""
rm-sync: polls rmfakecloud HTTP API, downloads changed documents,
renders strokes to PDF with the Node.js renderer (pdf-lib + rm-parser),
OCRs with ocrmypdf, and writes a markdown note into the Obsidian vault.

Required env:
  RMFAKECLOUD_URL  e.g. http://rmfakecloud-svc:3000
  RM_USER          rmfakecloud username
  RM_PASS          password

Optional env:
  OBSIDIAN_VAULT   vault mount path  (default /vault)
  RM_SUBDIR        subfolder         (default reMarkable)
  POLL_INTERVAL    seconds           (default 1800)
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL      = os.environ["RMFAKECLOUD_URL"].rstrip("/")
RM_USER       = os.environ["RM_USER"]
RM_PASS       = os.environ["RM_PASS"]
VAULT         = Path(os.environ.get("OBSIDIAN_VAULT", "/vault"))
SUBDIR        = os.environ.get("RM_SUBDIR", "reMarkable")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1800"))

RENDERER      = Path("/app/render.js")


def authenticate(session: requests.Session) -> str:
    r = session.post(
        f"{BASE_URL}/ui/api/login",
        json={"email": RM_USER, "password": RM_PASS},
        timeout=15,
    )
    r.raise_for_status()
    token = r.text.strip()
    if not token:
        raise ValueError("Empty token in auth response")
    return token


def _flatten_entries(entries: list) -> list[dict]:
    docs = []
    for entry in entries or []:
        if entry.get("type") == "notebook":
            docs.append(entry)
        if "children" in entry:
            docs.extend(_flatten_entries(entry["children"]))
    return docs


def list_documents(session: requests.Session, token: str) -> list[dict]:
    r = session.get(
        f"{BASE_URL}/ui/api/documents",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json() or {}
    return _flatten_entries(data.get("Entries", []))


def download_zip(session: requests.Session, token: str, doc_id: str, dest: Path) -> None:
    r = session.get(
        f"{BASE_URL}/ui/api/documents/{doc_id}?type=rmdoc",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        stream=True,
    )
    r.raise_for_status()
    with dest.open("wb") as fh:
        for chunk in r.iter_content(chunk_size=8192):
            fh.write(chunk)


def convert_to_pdf(zip_path: Path, pdf_path: Path) -> bool:
    try:
        result = subprocess.run(
            ["node", str(RENDERER), str(zip_path), str(pdf_path)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("  [rm-sync] timeout during PDF render", flush=True)
        return False
    if result.returncode != 0:
        print(f"  [rm-sync] render error: {result.stderr[:300]}", flush=True)
        return False
    if not pdf_path.exists() or pdf_path.stat().st_size < 100:
        print("  [rm-sync] render produced empty PDF", flush=True)
        return False
    return True


def ocr_and_extract(pdf_path: Path) -> str:
    ocr_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            ocr_tmp = Path(tmp.name)
        result = subprocess.run(
            ["ocrmypdf", "--skip-text", "--quiet", "--output-type", "pdf",
             str(pdf_path), str(ocr_tmp)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            print(f"  [rm-sync] ocrmypdf: {result.stderr[:200]}", flush=True)
            return ""
        shutil.move(str(ocr_tmp), str(pdf_path))
        ocr_tmp = None
        from pdfminer.high_level import extract_text
        return extract_text(str(pdf_path)).strip()
    except Exception as exc:
        print(f"  [rm-sync] OCR failed: {exc}", flush=True)
        return ""
    finally:
        if ocr_tmp is not None:
            ocr_tmp.unlink(missing_ok=True)


def write_markdown(name: str, doc_id: str, rm_dir: Path, ocr_text: str) -> None:
    synced  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rel_pdf = f"{SUBDIR}/{name}.pdf"
    ocr_section = f"\n## Transcription\n\n{ocr_text}\n" if ocr_text else ""
    content = f"""---
source: reMarkable
rm_id: {doc_id}
synced: {synced}
---

# {name}

> Synced from reMarkable via rm-sync.

![[{rel_pdf}]]
{ocr_section}"""
    (rm_dir / f"{name}.md").write_text(content)


def sync_once(session: requests.Session, state: dict, rm_dir: Path) -> dict:
    try:
        token = authenticate(session)
    except Exception as exc:
        print(f"[rm-sync] auth failed: {exc}", flush=True)
        return state

    try:
        docs = list_documents(session, token)
    except Exception as exc:
        print(f"[rm-sync] list documents failed: {exc}", flush=True)
        return state

    print(f"[rm-sync] {len(docs)} document(s) found", flush=True)

    for doc in docs:
        doc_id   = doc.get("id", "")
        name     = doc.get("name") or "Untitled"
        modified = doc.get("lastModified", "0")
        pdf_out  = rm_dir / f"{name}.pdf"

        cached = state.get(doc_id, {})
        if cached.get("modified") == modified and pdf_out.exists():
            continue

        print(f"[rm-sync] converting: {name}", flush=True)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            download_zip(session, token, doc_id, tmp_path)
            if convert_to_pdf(tmp_path, pdf_out):
                ocr_text = ocr_and_extract(pdf_out)
                write_markdown(name, doc_id, rm_dir, ocr_text)
                state[doc_id] = {
                    "modified": modified,
                    "name":     name,
                    "synced":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                print(f"  done: {name} ({'with OCR' if ocr_text else 'no OCR text'})", flush=True)
        except Exception as exc:
            print(f"  [rm-sync] error processing '{name}': {exc}", flush=True)
        finally:
            tmp_path.unlink(missing_ok=True)

    return state


def main() -> None:
    rm_dir = VAULT / SUBDIR
    rm_dir.mkdir(parents=True, exist_ok=True)

    state_file = rm_dir / ".sync-state.json"
    state: dict = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass

    session = requests.Session()
    print(f"[rm-sync] starting — polling every {POLL_INTERVAL}s", flush=True)
    time.sleep(15)

    while True:
        print(f"[rm-sync] sync at {datetime.now(timezone.utc).isoformat()}", flush=True)
        state = sync_once(session, state, rm_dir)
        state_file.write_text(json.dumps(state, indent=2))
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

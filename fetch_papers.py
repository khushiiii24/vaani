"""
Step 0: download the three papers at the exact versions the brief asks for,
and save the text of every page to data/pages.json.

Run once:   python fetch_papers.py
"""
import json
import os

import requests
from pypdf import PdfReader

# key -> (arXiv id with version, short name used in citations)
PAPERS = {
    "attention": ("1706.03762v7", "Attention Is All You Need"),
    "moshi": ("2410.00037v1", "Moshi"),
    "hnet": ("2507.07955v2", "Dynamic Chunking (H-Net)"),
}

os.makedirs("papers", exist_ok=True)
os.makedirs("data", exist_ok=True)

pages = []
for key, (arxiv_id, short_name) in PAPERS.items():
    pdf_path = f"papers/{arxiv_id}.pdf"
    if not os.path.exists(pdf_path):
        url = f"https://arxiv.org/pdf/{arxiv_id}"
        print("downloading", url)
        r = requests.get(url, timeout=60, headers={"User-Agent": "relay-assignment/1.0"})
        r.raise_for_status()
        with open(pdf_path, "wb") as f:
            f.write(r.content)

    reader = PdfReader(pdf_path)
    for i, page in enumerate(reader.pages, start=1):
        pages.append({
            "paper": key,
            "paper_name": short_name,
            "arxiv_id": arxiv_id,
            "page": i,
            "text": page.extract_text() or "",
        })
    print(f"{key}: {len(reader.pages)} pages")

with open("data/pages.json", "w", encoding="utf-8") as f:
    json.dump(pages, f, ensure_ascii=False)
print("saved data/pages.json with", len(pages), "pages")

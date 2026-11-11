"""
The retrieval tool: split the papers into pieces and search them with BM25.

Each piece remembers which paper, page and section it came from, so every
answer can cite where it came from.
"""
import json
import math
import re
import unicodedata
from collections import Counter

STOPWORDS = set("""a an the of to in on for and or is are was were be been by with as at from
that this these those it its what which who whom how why when where does do did can could
would should paper papers use used using about into than then there their they them i you we
our your his her he she not no yes if so such also more most other""".split())

# section headings look like "3.2 Attention", "4.1 T ext data", "A.1 Static Chunking",
# "Appendix D. Characterizing ...", "Abstract", "References"
SECTION_RE = re.compile(r"^\d+(\.\d+)*\s+[A-Z]")            # "1. How does..." (a list item) does not match
APPENDIX_RE = re.compile(r"^(Appendix\s+[A-H]\.?|[A-H](\.\d+)*)\s+[A-Z]")
NAMED_RE = re.compile(r"^(Abstract|References|Acknowledg\w*)$")


def is_heading(line):
    if len(line) > 80 or len(line.split()) > 10 or line.endswith((".", ",")):
        return False
    return bool(SECTION_RE.match(line) or APPENDIX_RE.match(line) or NAMED_RE.match(line))


def tokenize(text):
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())
    return [w for w in words if w not in STOPWORDS]


def load_words(pages_path="data/pages.json", drop_references=True):
    """Return one list of (word, page, section) per paper, in reading order.
    Drops the running header printed on every page, and (optionally) the reference list."""
    with open(pages_path, encoding="utf-8") as f:
        pages = json.load(f)
    # lines repeated on many pages of the same paper are running headers, not content
    line_pages = Counter()
    page_count = Counter(p["paper"] for p in pages)
    for p in pages:
        for line in set(l.strip() for l in p["text"].splitlines()):
            if len(line) > 15:
                line_pages[(p["paper"], line)] += 1
    papers = {}
    for p in pages:
        info = papers.setdefault(p["paper"], {"name": p["paper_name"], "words": [], "section": "start",
                                              "skipping": False})
        for line in p["text"].splitlines():
            line_s = line.strip()
            if line_pages[(p["paper"], line_s)] > 0.3 * page_count[p["paper"]]:
                continue                                  # running header
            line_s = re.sub(r"(\d) \.(\d)", r"\1.\2", line_s)   # PDF extraction splits "2.1T" into "2 .1T"
            line_s = unicodedata.normalize("NFKC", line_s)       # math letters like 𝑝𝑡 -> pt, searchable
            if is_heading(line_s):
                if line_s.startswith("References"):
                    info["skipping"] = drop_references    # the bibliography is noise for search
                elif info["skipping"] and APPENDIX_RE.match(line_s):
                    info["skipping"] = False              # the appendix starts after the references
                info["section"] = line_s[:60]
            if info["skipping"]:
                continue
            for w in line_s.split():
                info["words"].append((w, p["page"], info["section"]))
    return papers


class Retriever:
    def __init__(self, piece_words=250, top_k=4, rerank=False, pages_path="data/pages.json",
                 drop_references=True):
        self.top_k = top_k
        self.rerank = rerank
        self.pieces = []
        overlap = piece_words // 5
        for key, info in load_words(pages_path, drop_references).items():
            words = info["words"]
            step = piece_words - overlap
            for start in range(0, max(1, len(words) - overlap), step):
                chunk = words[start:start + piece_words]
                if not chunk:
                    continue
                self.pieces.append({
                    "paper": key,
                    "paper_name": info["name"],
                    "page": chunk[0][1],
                    "section": chunk[0][2],
                    "text": " ".join(w for w, _, _ in chunk),
                })
        # BM25 index
        self.docs = [tokenize(p["text"]) for p in self.pieces]
        self.tf = [Counter(d) for d in self.docs]
        self.avg_len = sum(len(d) for d in self.docs) / len(self.docs)
        df = Counter(w for d in self.docs for w in set(d))
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def _bm25(self, q_words, i, k1=1.5, b=0.75):
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for w in q_words:
            if w in tf:
                s += self.idf[w] * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * dl / self.avg_len))
        return s

    def search(self, query, paper=None):
        """The tool. Returns the top pieces and a 0..n relevance score per query word."""
        q_words = tokenize(query)
        if not q_words:
            return [], 0.0
        scored = []
        for i, piece in enumerate(self.pieces):
            if paper and piece["paper"] != paper:
                continue
            s = self._bm25(q_words, i)
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        n_candidates = self.top_k * 3 if self.rerank else self.top_k
        candidates = scored[:n_candidates]
        if self.rerank:
            candidates = self._rerank(q_words, candidates)
        hits = []
        for s, i in candidates[:self.top_k]:
            hit = dict(self.pieces[i])
            hit["score"] = round(s, 2)
            hits.append(hit)
        top = scored[0][0] / len(q_words) if scored else 0.0   # plain BM25, before re-rank
        return hits, top

    def _rerank(self, q_words, candidates):
        """Cheap re-rank, no model call: reward pieces that contain the query's word pairs
        (e.g. 'positional encoding') and its numbers, not just the single words."""
        pairs = set(zip(q_words, q_words[1:]))
        nums = {w for w in q_words if w[0].isdigit()}
        out = []
        for s, i in candidates:
            d = self.docs[i]
            doc_pairs = set(zip(d, d[1:]))
            bonus = 2.0 * len(pairs & doc_pairs) + 1.0 * len(nums & set(d))
            out.append((s + bonus, i))
        out.sort(reverse=True)
        return out

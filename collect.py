#!/usr/bin/env python3
"""Daily Paper Index collector.

Collects everything a set of open scholarly sources lists for one UTC day:
preprints, working papers, white papers / reports and theses. Writes
docs/data/<date>.json plus docs/data/index.json and docs/data/manifest.json.

Usage: python collect.py [--date YYYY-MM-DD] [--days-ago N] [--only src1,src2]
Standard library only.
"""
import argparse, datetime as dt, gzip, html, json, os, re, sys, time, traceback
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "docs", "data")
MAILTO = os.environ.get("CONTACT_EMAIL", "daily-paper-index@users.noreply.github.com")
UA = f"DailyPaperIndex/1.0 (+https://github.com/For3stpunk/paper-index; mailto:{MAILTO})"
ABSTRACT_MAX = 320
AUTHORS_MAX = 6
CAP = int(os.environ.get("SOURCE_CAP", "25000"))

# ---------------------------------------------------------------- helpers
def get(url, tries=4, accept=None, timeout=90):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip",
                                                       **({"Accept": accept} if accept else {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404, 409):
                try:
                    body = e.read()[:300].decode("utf-8", "replace")
                except Exception:
                    body = ""
                raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {body}") from None
            time.sleep(5 * (i + 1) + (20 if e.code in (429, 503) else 0))
        except Exception as e:  # timeouts, resets
            last = e
            time.sleep(5 * (i + 1))
    raise last

def getjson(url, **kw):
    return json.loads(get(url, accept="application/json", **kw))

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
def clean(s, n=None):
    if not s:
        return ""
    if isinstance(s, list):
        s = s[0] if s else ""
    s = WS.sub(" ", html.unescape(TAG.sub(" ", str(s)))).strip()
    if n and len(s) > n:
        s = s[: n - 1].rsplit(" ", 1)[0] + "…"
    return s

def norm_doi(d):
    if not d:
        return ""
    d = d.strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)
    return d

ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$")
def arxiv_from_doi(d):
    m = ARXIV_DOI.match(d or "")
    return m.group(1) if m else ""

def rec(**k):
    """Compact record. Keys: t title, a authors, n author count, s source, k kind,
    f field, d date, u url, doi, x abstract, id."""
    authors = [clean(a) for a in (k.get("a") or []) if clean(a)]
    r = {
        "t": clean(k.get("t"), 400),
        "a": authors[:AUTHORS_MAX],
        "s": k["s"],
        "k": k["k"],
        "u": k.get("u") or "",
    }
    if len(authors) > AUTHORS_MAX:
        r["n"] = len(authors)
    for key in ("f", "doi", "id", "pub"):
        if k.get(key):
            r[key] = clean(k[key], 120) if key in ("f", "pub") else k[key]
    x = clean(k.get("x"), ABSTRACT_MAX)
    if x:
        r["x"] = x
    return r

def oai_records(base, day, prefix="oai_dc", sleep=1.0, set_=None, seconds=False):
    """Yield <record> elements from an OAI-PMH ListRecords harvest for one day.
    seconds=True for servers with second granularity that read a bare 'until' date as midnight."""
    ns = {"o": "http://www.openarchives.org/OAI/2.0/"}
    params = {"verb": "ListRecords", "metadataPrefix": prefix,
              "from": f"{day}T00:00:00Z" if seconds else day, "until": f"{day}T23:59:59Z" if seconds else day}
    if set_:
        params["set"] = set_
    url = base + "?" + urllib.parse.urlencode(params)
    while url:
        raw = get(url)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise RuntimeError(f"not an OAI-PMH response from {base}: {raw[:120]!r}")
        if not root.tag.endswith("OAI-PMH"):
            raise RuntimeError(f"not an OAI-PMH response from {base}: root {root.tag}")
        err = root.find("o:error", ns)
        if err is not None:
            if err.get("code") == "noRecordsMatch":
                return
            raise RuntimeError(f"OAI error {err.get('code')}: {err.text}")
        lr = root.find("o:ListRecords", ns)
        if lr is None:
            return
        for r in lr.findall("o:record", ns):
            h = r.find("o:header", ns)
            if h is not None and h.get("status") == "deleted":
                continue
            yield r
        tok = lr.find("o:resumptionToken", ns)
        url = None
        if tok is not None and (tok.text or "").strip():
            url = base + "?" + urllib.parse.urlencode({"verb": "ListRecords", "resumptionToken": tok.text.strip()})
            time.sleep(sleep)

DC = "{http://purl.org/dc/elements/1.1/}"
def dc_all(r, tag):
    return [e.text.strip() for e in r.iter(DC + tag) if e.text and e.text.strip()]

def dc_record(r, source, kind_default="paper", field=""):
    ids = dc_all(r, "identifier")
    url = next((i for i in ids if i.startswith("http")), "")
    doi = next((norm_doi(i) for i in ids if "doi.org/10." in i or i.lower().startswith("doi:") or i.startswith("10.")), "")
    types = " ".join(dc_all(r, "type")).lower()
    kind = kind_default
    if "thesis" in types or "dissertation" in types:
        kind = "thesis"
    elif "report" in types or "working" in types:
        kind = "report"
    subj = dc_all(r, "subject")
    return rec(t=(dc_all(r, "title") or [""])[0], a=dc_all(r, "creator"), s=source, k=kind,
               u=url, doi=doi, x=(dc_all(r, "description") or [""])[0],
               f=field or (subj[0] if subj else ""))

NOTES = {}  # per-source extra info for status.json

# ---------------------------------------------------------------- sources
ARXIV_GROUPS = {
    "cs": "Computer science", "math": "Mathematics", "stat": "Statistics", "q-bio": "Quantitative biology",
    "q-fin": "Quantitative finance", "econ": "Economics", "eess": "Electrical engineering",
    "astro-ph": "Astrophysics", "cond-mat": "Condensed matter", "gr-qc": "General relativity",
    "hep-ex": "High energy physics", "hep-lat": "High energy physics", "hep-ph": "High energy physics",
    "hep-th": "High energy physics", "math-ph": "Mathematical physics", "nlin": "Nonlinear sciences",
    "nucl-ex": "Nuclear physics", "nucl-th": "Nuclear physics", "physics": "Physics", "quant-ph": "Quantum physics",
}

def src_arxiv(day):
    d = day.replace("-", "")
    q = f"submittedDate:[{d}0000 TO {d}2359]"
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom",
          "os": "http://a9.com/-/spec/opensearch/1.1/"}
    out, start, total, page = [], 0, None, 1000
    while total is None or start < total:
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": q, "start": start, "max_results": page,
             "sortBy": "submittedDate", "sortOrder": "ascending"})
        root = ET.fromstring(get(url, timeout=180))
        total = int(root.findtext("os:totalResults", "0", ns))
        entries = root.findall("a:entry", ns)
        tries = 0
        while not entries and start < total and tries < 5:
            tries += 1  # arXiv sometimes returns an empty page; wait and ask again
            time.sleep(15 * tries)
            root = ET.fromstring(get(url, timeout=180))
            entries = root.findall("a:entry", ns)
        NOTES["arxiv"] = (f"arXiv reports {total} submissions" if total else
                          "nothing announced yet for this day (arXiv lists submissions once announced; re-collected later)")
        if not entries and total:
            raise RuntimeError(f"arXiv stopped returning results at {start} of {total}")
        if not total:
            break
        for e in entries:
            aid = re.sub(r"v\d+$", "", e.findtext("a:id", "", ns).rsplit("/abs/", 1)[-1])
            prim = e.find("x:primary_category", ns)
            cat = prim.get("term") if prim is not None else ""
            grp = ARXIV_GROUPS.get(cat.split(".")[0], "")
            out.append(rec(t=e.findtext("a:title", "", ns), s="arXiv", k="preprint",
                           a=[a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
                           u=f"https://arxiv.org/abs/{aid}", id=f"arXiv:{aid}",
                           doi=norm_doi(e.findtext("x:doi", "", ns)),
                           f=f"{cat} · {grp}" if grp else cat, x=e.findtext("a:summary", "", ns)))
        start += page
        if start > CAP:
            break
        time.sleep(4)
    return out

def src_rxiv(server):
    def run(day):
        out, cursor = [], 0
        while True:
            j = getjson(f"https://api.biorxiv.org/details/{server}/{day}/{day}/{cursor}")
            coll = j.get("collection") or []
            for p in coll:
                if str(p.get("version")) != "1":
                    continue  # new submissions only, not revisions
                out.append(rec(t=p.get("title"), a=(p.get("authors") or "").split(";"),
                               s={"biorxiv": "bioRxiv", "medrxiv": "medRxiv"}[server],
                               k="preprint", doi=norm_doi(p.get("doi")),
                               u=f"https://www.{server}.org/content/{p.get('doi')}v1",
                               f=(p.get("category") or "").capitalize(), x=p.get("abstract")))
            msg = (j.get("messages") or [{}])[0]
            total = int(msg.get("total", 0) or 0)
            cursor += len(coll)
            if not coll or cursor >= total or cursor > CAP:
                return out
            time.sleep(1)
    return run

PHILARCHIVE_OAI = ["https://philarchive.org/oai.pl", "https://philpapers.org/oai.pl",
                   "https://philarchive.org/oai", "https://philpapers.org/oai"]
def src_philarchive(day):
    errs = []
    for base in PHILARCHIVE_OAI:
        try:
            recs = [dc_record(r, "PhilArchive", "paper", "Philosophy") for r in oai_records(base, day, seconds=True)]
            NOTES["philarchive"] = f"via {base}"
            if not recs:  # diagnose: what does the endpoint say about itself and the past week?
                try:
                    ident = get(base + "?verb=Identify").decode("utf-8", "replace")
                    gran = re.search(r"<granularity>(.*?)<", ident)
                    early = re.search(r"<earliestDatestamp>(.*?)<", ident)
                    wk = (dt.date.fromisoformat(day) - dt.timedelta(days=7)).isoformat()
                    li = get(base + "?" + urllib.parse.urlencode({"verb": "ListIdentifiers", "metadataPrefix": "oai_dc", "from": wk})).decode("utf-8", "replace")
                    stamps = re.findall(r"<datestamp>(.*?)<", li)
                    NOTES["philarchive"] += (f"; granularity {gran and gran.group(1)}, earliest {early and early.group(1)}, "
                                             f"{len(stamps)} ids since {wk}, latest {max(stamps) if stamps else 'none'}; "
                                             f"head {li[:160]!r}")
                except Exception as e:
                    NOTES["philarchive"] += f"; diagnose failed: {e}"
            return recs
        except Exception as e:
            errs.append(f"{base}: {str(e)[:100]}")
    raise RuntimeError(" | ".join(errs))

def src_philsci(day):
    return [dc_record(r, "PhilSci-Archive", "preprint", "Philosophy of science")
            for r in oai_records("https://philsci-archive.pitt.edu/cgi/oai2", day)]

def src_iacr(day):
    out = []
    for r in oai_records("https://eprint.iacr.org/oai", day):
        x = dc_record(r, "IACR ePrint", "preprint", "Cryptography")
        out.append(x)
    return out

HAL_KINDS = {"THESE": "thesis", "HDR": "thesis", "MEM": "thesis", "ETABTHESE": "thesis",
             "REPORT": "report", "UNDEFINED": "preprint", "PREPRINT": "preprint", "WORKINGPAPER": "report",
             "OTHER": "paper"}
def src_hal(day):
    out, cursor = [], "*"
    fq = [f"submittedDate_tdate:[{day}T00:00:00Z TO {day}T23:59:59Z]",
          "docType_s:(" + " OR ".join(HAL_KINDS) + ")"]
    while True:
        params = [("q", "*:*"), ("rows", "1000"), ("sort", "docid asc"), ("cursorMark", cursor), ("wt", "json"),
                  ("fl", "halId_s,title_s,authFullName_s,abstract_s,docType_s,uri_s,doiId_s,primaryDomain_s,domain_s")]
        params += [("fq", f) for f in fq]
        j = getjson("https://api.archives-ouvertes.fr/search/?" + urllib.parse.urlencode(params))
        docs = j.get("response", {}).get("docs", [])
        for d in docs:
            dom = d.get("primaryDomain_s") or ""
            out.append(rec(t=d.get("title_s"), a=d.get("authFullName_s"), s="HAL",
                           k=HAL_KINDS.get(d.get("docType_s"), "paper"), u=d.get("uri_s"),
                           doi=norm_doi(d.get("doiId_s")), x=d.get("abstract_s"),
                           f=HAL_DOMAINS.get(dom.split(".")[0], dom)))
        nxt = j.get("nextCursorMark")
        if not docs or not nxt or nxt == cursor or len(out) > CAP:
            return out
        cursor = nxt

HAL_DOMAINS = {"shs": "Humanities & social sciences", "sdv": "Life sciences", "spi": "Engineering",
               "info": "Computer science", "math": "Mathematics", "phys": "Physics", "sdu": "Earth sciences",
               "chim": "Chemistry", "sde": "Environmental sciences", "qfin": "Finance", "stat": "Statistics",
               "nlin": "Nonlinear sciences", "scco": "Cognitive science"}

ZENODO_KINDS = {"thesis": "thesis", "report": "report", "workingpaper": "report", "technicalnote": "report",
                "deliverable": "report", "proposal": "report", "standard": "report", "preprint": "preprint"}
def src_zenodo(day):
    out, page = [], 1
    q = f'metadata.publication_date:"{day}" AND metadata.resource_type.id:publication*'
    while True:
        url = "https://zenodo.org/api/records?" + urllib.parse.urlencode(
            {"q": q, "size": 25, "page": page, "sort": "newest"})
        j = getjson(url)
        hits = j.get("hits", {}).get("hits", [])
        for h in hits:
            m = h.get("metadata", {})
            rt = (m.get("resource_type") or {})
            sub = rt.get("subtype") or (rt.get("type") or "")
            if sub in ("book", "section", "annotationcollection", "datamanagementplan", "patent",
                       "peerreview", "taxonomictreatment", "softwaredocumentation", "journal"):
                continue
            kw = m.get("keywords") or []
            out.append(rec(t=m.get("title"), a=[c.get("name") for c in m.get("creators", [])],
                           s="Zenodo", k=ZENODO_KINDS.get(sub, "paper"),
                           u=h.get("links", {}).get("self_html") or f"https://zenodo.org/records/{h.get('id')}",
                           doi=norm_doi(h.get("doi") or m.get("doi")), x=m.get("description"),
                           f=kw[0] if kw else (rt.get("title") or "")))
        total = j.get("hits", {}).get("total", 0)
        if isinstance(total, dict):
            total = total.get("value", 0)
        if not hits or page * 25 >= min(total, 9975) or len(out) > CAP:
            return out
        page += 1
        time.sleep(1.2)

CROSSREF_PREFIX = {"10.2139": "SSRN", "10.21203": "Research Square", "10.20944": "Preprints.org",
                   "10.26434": "ChemRxiv", "10.31234": "PsyArXiv", "10.31235": "SocArXiv",
                   "10.31219": "OSF Preprints", "10.35542": "EdArXiv", "10.31223": "EarthArXiv",
                   "10.36227": "TechRxiv", "10.22541": "Authorea", "10.1101": "bioRxiv/medRxiv",
                   "10.3386": "NBER", "10.31222": "MetaArXiv", "10.32942": "EcoEvoRxiv",
                   "10.33774": "Cambridge Open Engage", "10.31237": "SportRxiv", "10.31224": "engrXiv",
                   "10.31730": "AfricArXiv", "10.31236": "LawArXiv", "10.31220": "AgriXiv",
                   "10.31232": "NutriXiv", "10.31233": "PaleorXiv", "10.31221": "Frenxiv",
                   "10.31226": "LIS Scholarship Archive", "10.35543": "MindRxiv", "10.31229": "INA-Rxiv"}
def crossref_source(doi, publisher):
    pre = doi.split("/", 1)[0]
    return CROSSREF_PREFIX.get(pre) or (publisher or "Crossref")[:60]

def src_crossref(ctype, kind, datefilter):
    def run(day):
        out, cursor = [], "*"
        filt = f"from-{datefilter}-date:{day},until-{datefilter}-date:{day},type:{ctype}"
        while True:
            url = "https://api.crossref.org/works?" + urllib.parse.urlencode(
                {"filter": filt, "rows": 1000, "cursor": cursor, "mailto": MAILTO,
                 **({} if ctype == "dissertation" else
                    {"select": "DOI,title,author,publisher,subject,abstract,URL,group-title"})})
            j = getjson(url, timeout=180)["message"]
            items = j.get("items", [])
            for it in items:
                doi = norm_doi(it.get("DOI"))
                authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
                           for a in it.get("author", [])]
                inst = it.get("institution") or []
                pub = inst[0].get("name") if inst and kind == "thesis" else ""
                src = crossref_source(doi, it.get("publisher"))
                out.append(rec(t=it.get("title"), a=authors, s=src, k=kind,
                               u=f"https://doi.org/{doi}", doi=doi, x=it.get("abstract"),
                               f=(it.get("subject") or [it.get("group-title") or ""])[0], pub=pub))
            cursor = j.get("next-cursor")
            if not items or not cursor or len(items) < 1000 or len(out) > CAP:
                return out
            time.sleep(1)
    return run

DATACITE_KINDS = {"Dissertation": "thesis", "Report": "report", "Preprint": "preprint"}
def src_datacite(day):
    out, url = [], "https://api.datacite.org/dois?" + urllib.parse.urlencode({
        "query": f"types.resourceTypeGeneral:(Dissertation OR Report OR Preprint) AND "
                 f"created:[{day}T00:00:00Z TO {day}T23:59:59Z]",
        "page[size]": 1000, "page[cursor]": 1,
        "fields[dois]": "doi,titles,creators,types,descriptions,subjects,url,publisher"})
    while url:
        j = getjson(url, timeout=180)
        data = j.get("data", [])
        for d in data:
            a = d.get("attributes", {})
            doi = norm_doi(a.get("doi"))
            if doi.startswith(("10.48550/", "10.5281/")):
                continue  # arXiv and Zenodo are collected directly
            rtg = (a.get("types") or {}).get("resourceTypeGeneral")
            desc = next((x.get("description") for x in a.get("descriptions") or []
                         if x.get("descriptionType") == "Abstract"), "")
            subj = (a.get("subjects") or [{}])
            pub = a.get("publisher")
            if isinstance(pub, dict):
                pub = pub.get("name")
            out.append(rec(t=((a.get("titles") or [{}])[0]).get("title"),
                           a=[c.get("name") for c in a.get("creators") or []],
                           s="DataCite", k=DATACITE_KINDS.get(rtg, "paper"), u=f"https://doi.org/{doi}",
                           doi=doi, x=desc, f=subj[0].get("subject") if subj and subj[0] else "", pub=pub))
        url = (j.get("links") or {}).get("next")
        if not data or len(out) > CAP:
            break
        time.sleep(1)
    return out

def inverted_to_text(inv):
    if not inv:
        return ""
    pos = {}
    for w, ps in inv.items():
        for p in ps:
            pos[p] = w
            if len(pos) > 120:
                break
    return " ".join(pos[k] for k in sorted(pos)[:90])

def src_openalex(otype, kind):
    def run(day):
        out, cursor = [], "*"
        key = os.environ.get("OPENALEX_API_KEY")
        if not key:
            raise RuntimeError("skipped: add a free OpenAlex API key as the OPENALEX_API_KEY repository secret")
        while cursor:
            params = {"filter": f"from_publication_date:{day},to_publication_date:{day},type:{otype}",
                      "per_page": 100, "cursor": cursor, "mailto": MAILTO,
                      "select": "id,doi,display_name,authorships,primary_location,primary_topic,abstract_inverted_index,ids"}
            params["api_key"] = key
            j = getjson("https://api.openalex.org/works?" + urllib.parse.urlencode(params), timeout=120)
            res = j.get("results", [])
            for w in res:
                doi = norm_doi(w.get("doi"))
                loc = w.get("primary_location") or {}
                srcname = ((loc.get("source") or {}).get("display_name")) or ""
                insts = []
                for au in w.get("authorships") or []:
                    insts += [i.get("display_name") for i in au.get("institutions") or []]
                topic = (w.get("primary_topic") or {})
                field = ((topic.get("field") or {}).get("display_name")) or topic.get("display_name") or ""
                aid = arxiv_from_doi(doi)
                out.append(rec(t=w.get("display_name"), s="OpenAlex", k=kind,
                               a=[(au.get("author") or {}).get("display_name") for au in w.get("authorships") or []],
                               u=loc.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else w.get("id")),
                               doi=doi, id=f"arXiv:{aid}" if aid else "", f=field,
                               x=inverted_to_text(w.get("abstract_inverted_index")),
                               pub=(insts[0] if insts and kind == "thesis" else srcname)))
            cursor = (j.get("meta") or {}).get("next_cursor")
            if not res or len(out) > CAP:
                break
            time.sleep(0.3)
        return out
    return run

# Order = priority when the same work turns up in several places.
SOURCES = [
    ("arxiv", "arXiv", src_arxiv),
    ("biorxiv", "bioRxiv", src_rxiv("biorxiv")),
    ("medrxiv", "medRxiv", src_rxiv("medrxiv")),
    ("philarchive", "PhilArchive (PhilPapers)", src_philarchive),
    ("philsci", "PhilSci-Archive", src_philsci),
    ("iacr", "IACR Cryptology ePrint", src_iacr),
    ("hal", "HAL (theses, reports, preprints)", src_hal),
    ("zenodo", "Zenodo publications", src_zenodo),
    ("crossref_preprints", "Crossref posted content (SSRN, ChemRxiv, PsyArXiv, Research Square…)",
     src_crossref("posted-content", "preprint", "posted")),
    ("crossref_reports", "Crossref reports & working papers", src_crossref("report", "report", "pub")),
    ("crossref_theses", "Crossref dissertations", src_crossref("dissertation", "thesis", "pub")),
    ("datacite", "DataCite theses, reports & preprints", src_datacite),
    ("openalex_theses", "OpenAlex dissertations", src_openalex("dissertation", "thesis")),
    ("openalex_reports", "OpenAlex reports", src_openalex("report", "report")),
]

TITLE_KEY = re.compile(r"[^a-z0-9]+")
def dedupe(records):
    seen, out = {}, []
    for r in records:
        keys = []
        if r.get("id"):
            keys.append(r["id"].lower())
        if r.get("doi"):
            keys.append(r["doi"])
            a = arxiv_from_doi(r["doi"])
            if a:
                keys.append(f"arxiv:{a}")
        tk = TITLE_KEY.sub("", (r.get("t") or "").lower())
        if len(tk) > 25:
            keys.append("t:" + tk[:160])
        hit = next((seen[k] for k in keys if k in seen), None)
        if hit is not None:
            also = hit.setdefault("also", [])
            if r["s"] != hit["s"] and r["s"] not in also:
                also.append(r["s"])
            for f in ("x", "doi", "f", "pub"):
                if not hit.get(f) and r.get(f):
                    hit[f] = r[f]
            if not hit.get("a") and r.get("a"):
                hit["a"] = r["a"]
            for k in keys:
                seen.setdefault(k, hit)
            continue
        if not r.get("t"):
            continue
        for k in keys:
            seen[k] = r
        out.append(r)
    return out

def collect(day, only=None):
    status, allrecs = {}, []
    for key, label, fn in SOURCES:
        if only and key not in only:
            continue
        t0 = time.time()
        try:
            recs = fn(day)
            status[key] = {"label": label, "status": "ok", "raw": len(recs), "secs": round(time.time() - t0)}
            if NOTES.get(key):
                status[key]["note"] = NOTES[key]
            allrecs += recs
        except Exception as e:
            traceback.print_exc()
            status[key] = {"label": label, "status": f"error: {type(e).__name__}: {str(e)[:200]}",
                           "raw": 0, "secs": round(time.time() - t0)}
        print(f"{day} {key}: {status[key]['status']} {status[key]['raw']} ({status[key]['secs']}s)", flush=True)
    items = dedupe(allrecs)
    return items, status

def write_day(day, items, status):
    os.makedirs(DATA, exist_ok=True)
    kinds, sources = {}, {}
    for r in items:
        kinds[r["k"]] = kinds.get(r["k"], 0) + 1
        sources[r["s"]] = sources.get(r["s"], 0) + 1
    doc = {"date": day, "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "count": len(items), "kinds": kinds, "status": status, "items": items}
    with open(os.path.join(DATA, f"{day}.json"), "w") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    summary = {"date": day, "count": len(items), "kinds": kinds,
            "sources": dict(sorted(sources.items(), key=lambda x: -x[1])[:40]),
            "errors": [k for k, v in status.items() if not v["status"].startswith("ok")],
            "generated": doc["generated"]}
    os.makedirs(os.path.join(DATA, "sum"), exist_ok=True)
    json.dump(summary, open(os.path.join(DATA, "sum", f"{day}.json"), "w"), ensure_ascii=False)
    return summary

RUN_DATES = os.path.join(ROOT, ".run-dates.json")  # not committed

def reindex():
    """Rebuild index.json from docs/data/sum/*.json, and manifest.json from this run's dates."""
    sumdir = os.path.join(DATA, "sum")
    days = []
    for fn in sorted(os.listdir(sumdir)) if os.path.isdir(sumdir) else []:
        if fn.endswith(".json") and os.path.exists(os.path.join(DATA, fn)):
            days.append(json.load(open(os.path.join(sumdir, fn))))
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    idx = {"updated": now, "sources": [{"key": k, "label": l} for k, l, _ in SOURCES],
           "days": sorted(days, key=lambda d: d["date"], reverse=True)}
    json.dump(idx, open(os.path.join(DATA, "index.json"), "w"), ensure_ascii=False, indent=0)
    run = json.load(open(RUN_DATES)) if os.path.exists(RUN_DATES) else []
    json.dump({"updated": now, "dates": run}, open(os.path.join(DATA, "manifest.json"), "w"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="append", help="YYYY-MM-DD (repeatable)")
    ap.add_argument("--days-ago", default="1", help="comma list of offsets from today UTC, e.g. 1,4")
    ap.add_argument("--only", help="comma list of source keys")
    ap.add_argument("--reindex", action="store_true", help="only rebuild index.json and manifest.json")
    a = ap.parse_args()
    if a.reindex:
        return reindex()
    today = dt.datetime.utcnow().date()
    days = a.date or [(today - dt.timedelta(days=int(n))).isoformat() for n in a.days_ago.split(",") if n.strip()]
    only = set(a.only.split(",")) if a.only else None
    summaries = []
    for day in days:
        items, status = collect(day, only)
        summaries.append(write_day(day, items, status))
        print(f"== {day}: {len(items)} unique records", flush=True)
    json.dump(days, open(RUN_DATES, "w"))
    reindex()

if __name__ == "__main__":
    main()

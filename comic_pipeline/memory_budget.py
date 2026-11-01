"""Bound reference data locally, without an extra Gemini countTokens request.

One UTF-8 byte is charged as one estimated token: deliberately conservative,
not a measurement of Gemini tokens. Current-page text, images and constant
translation instructions are outside this reference-data budget.
"""
from copy import deepcopy
import json
import unicodedata

MEMORY_TOKEN_BUDGET = 12000
FIXED_BUDGET = 5000
CACHED_FIXED_BUDGET = 48000
SPLIT_CACHE_POLICY = "split-v1"
MEMORY_TOKEN_CHOICES = (6000, 12000, 24000, 48000)


def memory_token_limit(value=MEMORY_TOKEN_BUDGET):
    if type(value) is not int or value not in MEMORY_TOKEN_CHOICES:
        raise ValueError("作品上下文上限須為 6,000、12,000、24,000 或 48,000 估算 token")
    return value


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimated_tokens(value):
    return len(packed(value).encode("utf-8"))


def relevance(value, query):
    text = unicodedata.normalize("NFKC", packed(value)).casefold()
    query = unicodedata.normalize("NFKC", query).casefold()
    return sum(query[i:i+2] in text for i in range(max(0, len(query)-1)))


def compact_fact(item):
    if not isinstance(item, dict):
        return None
    result = {k:deepcopy(v) for k,v in item.items() if k in {"page", "page_key", "certainty"}}
    result["text"] = str(item.get("text", ""))[:350]
    result["evidence"] = [dict(id=e.get("id", ""), quote=str(e.get("quote", ""))[:120])
                          for e in item.get("evidence", [])[:2] if isinstance(e, dict)]
    return result


def bounded_context(context, query="", *, stable_fixed=False, token_limit=MEMORY_TOKEN_BUDGET, cache_fixed=False):
    token_limit = memory_token_limit(token_limit)
    fixed_limit = CACHED_FIXED_BUDGET if cache_fixed else FIXED_BUDGET * token_limit // MEMORY_TOKEN_BUDGET
    original = dict(fixed=context.get("fixed", {}), dynamic=context.get("dynamic", {}))
    # Reserve container overhead before filling either allowance.
    fixed = dict(glossary=[],translation_style=[],setting=[],uncertainties=[])
    dynamic = dict(recent_pages=[], grounded_history=[], observed_terms=[], unresolved_questions=[])
    result = dict(context, fixed=fixed, dynamic=dynamic)
    def fits():
        # Leave room for the labels separating preferences, fixed and dynamic.
        if cache_fixed:
            return estimated_tokens(fixed) <= fixed_limit and estimated_tokens(dynamic) <= token_limit-256
        return estimated_tokens(dict(fixed=fixed, dynamic=dynamic)) <= token_limit-256
    def scalar(key, value, max_chars):
        if not isinstance(value, str):
            return
        value = value[:max_chars]
        while value:
            fixed[key] = value
            if estimated_tokens(fixed) <= fixed_limit and fits():
                return
            value = value[:max(0,len(value)-max(1,len(value)//8))]
        fixed.pop(key, None)
    def append(target, item, fixed_part=False):
        target.append(item)
        if not fits() or fixed_part and estimated_tokens(fixed)>fixed_limit:
            target.pop()
            return False
        return True
    source = original["fixed"] if isinstance(original["fixed"], dict) else {}
    for key, limit in [("title_ja",300),("title_zh_tw",300),("user_instructions",900)]:
        scalar(key, source.get(key), limit)
    synopsis = source.get("synopsis")
    if synopsis:
        item = compact_fact(synopsis) if isinstance(synopsis,dict) else str(synopsis)[:350]
        fixed["synopsis"] = item
        if estimated_tokens(fixed)>fixed_limit or not fits():fixed.pop("synopsis")
    terms = {}
    for item in source.get("characters", [])+source.get("glossary", []):
        if isinstance(item, dict):
            terms.setdefault(str(item.get("ja", "")), item)
    ordered = sorted(terms.values(), key=lambda t:(relevance(t,"" if stable_fixed else query),t.get("certainty")=="confirmed",t.get("kind")=="character",str(t.get("ja",""))), reverse=True)
    fixed["glossary"] = []
    for term in ordered:
        item = {k:str(term[k])[:150] for k in ("ja","zh_tw","kind","certainty") if k in term}
        item["aliases"] = [s[:80] for s in term.get("aliases", [])[:4] if isinstance(s,str)]
        if term.get("notes"):item["notes"]=str(term["notes"])[:160]
        if not append(fixed["glossary"],item,True) and "notes" in item:
            item.pop("notes");append(fixed["glossary"],item,True)
    for key in ("translation_style","setting","uncertainties"):
        fixed[key] = []
        for item in source.get(key, [])[:8]:
            item = compact_fact(item) if isinstance(item,dict) else str(item)[:200]
            append(fixed[key],item,True)
    story = source.get("recent_story")
    story = story.get("text", "") if isinstance(story, dict) else story
    if isinstance(story, str) and story:
        # A story snapshot is deliberately allowed to be much longer than the
        # premise synopsis. Explicit mode keeps its full, stable prefix when it fits.
        scalar("recent_story", story, 12000)
    prior = original["dynamic"] if isinstance(original["dynamic"],dict) else {}
    dynamic["continuity"] = str(prior.get("continuity", "Only processed pages; unseen gaps are unknown."))[:200]
    # Give the most recent dialogue first claim on the remaining space.
    for page in reversed(prior.get("recent_pages", [])[-5:]):
        item = {k:page[k] for k in ("page","page_key") if k in page}
        item["dialogue"] = []
        if not append(dynamic["recent_pages"],item):continue
        for block in page.get("dialogue", []):
            append(item["dialogue"], {k:str(block.get(k,""))[:400] for k in ("id","source_text","translation")})
    dynamic["recent_pages"].reverse()
    if stable_fixed:
        selected={t.get("ja") for t in fixed["glossary"]}
        relevant=sorted((t for t in terms.values() if t.get("ja") not in selected and relevance(t,query)>0),key=lambda t:relevance(t,query),reverse=True)
        for term in relevant[:20]:
            append(dynamic["observed_terms"],{k:str(term[k])[:150] for k in ("ja","zh_tw","certainty") if k in term})
    for term in sorted(prior.get("observed_terms", [])[-100:],key=lambda t:relevance(t,query),reverse=True):
        if len(dynamic["observed_terms"])>=100:break
        if any(t.get("ja")==term.get("ja") for t in dynamic["observed_terms"]):continue
        item={k:deepcopy(term[k]) for k in ("page","ja","zh_tw") if k in term}
        append(dynamic["observed_terms"], item)
    for fact in sorted(prior.get("grounded_history", [])[:35],key=lambda f:relevance(f,query),reverse=True):
        append(dynamic["grounded_history"],compact_fact(fact))
    dynamic["grounded_history"].sort(key=lambda f:f.get("page",0))
    for fact in prior.get("unresolved_questions", [])[-10:]:
        append(dynamic["unresolved_questions"],compact_fact(fact))
    size = estimated_tokens(dict(fixed=fixed,dynamic=dynamic))
    result["budget"] = dict(limit_estimated_tokens=token_limit+(fixed_limit if cache_fixed else 0),
        selected_limit_estimated_tokens=token_limit, fixed_limit_estimated_tokens=fixed_limit, estimated_tokens=size,
        fixed_estimated_tokens=estimated_tokens(fixed), dynamic_estimated_tokens=estimated_tokens(dynamic),
        policy="explicit_candidate" if cache_fixed else "combined",
        estimator="utf8-bytes-conservative", truncated=packed(original)!=packed(dict(fixed=fixed,dynamic=dynamic)))
    return result

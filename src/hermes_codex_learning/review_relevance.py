"""Task relevance and quote-backed learning claims, independent of Markdown layout."""
from __future__ import annotations

from copy import deepcopy
import re
import json

from .knowledge import _tokens

CONCEPTS = (
    ("research", {"research","investigation","调研","研究"}),
    ("alternatives", {"alternative","alternatives","option","options","方案","选型","选项"}),
    ("documents", {"document","documents","documentation","source","sources","资料","文档","来源"}),
    ("internal", {"internal","enterprise","内部","内网","企业"}),
    ("external", {"external","public","外部","公开"}),
    ("capabilities", {"capability","capabilities","能力"}),
    ("comparison", {"compare","comparison","comparative","比较","对比"}),
    ("technical", {"technical","technology","技术"}),
)
GENERIC = {"learned","skill","skills","workflow","task","tasks","current","user","only","this","that","with",
           "from","when","then","before","after","should","must","have","will","were","been","also","well",
           "use","uses","using","test","tests","verify","verification","validate","validation","limits","limit",
           "applicability","prerequisites","需要","可以","使用","流程","验证","测试","当前","用户","以后"}
STABLE = re.compile(r"(?i)\b(?:prefer|always|usually|in future|from now on)\b|以后|今后|默认|每次|长期|通常|习惯|偏好")
GENERIC.update({"and","the","for","not","are","was","has","had","our","your","their","they","these","those",
                "into","than","but","can","could","would","now","first","second","from","all","each","its",
                "http","https","home","data00","tools","functions","exec","command","json","query","cmd","cwd","workdir",
                "const","await","return","这个","这里","已经","相关","所有","结果","进行","后续","应该","必须","目前","不要","可能"})


def concepts(text):
    terms = (_tokens(text) | _tokens(text.replace("_"," ").replace("-"," "))) - GENERIC
    for canonical, aliases in CONCEPTS:
        if terms & aliases or any(alias in text for alias in aliases if not alias.isascii()):
            terms.difference_update(aliases)
            terms.add(canonical)
    return terms


def message_text(message):
    text = message.get("content") or ""
    return re.sub(r"^\[(?:Evidence |External execution evidence ).*?\]\n", "", text, count=1) if isinstance(text,str) else ""


def task_evidence(messages):
    evidence, topic = {}, set()
    for i,message in enumerate(messages,1):
        if message.get("review_context"):
            continue
        if message.get("tool_calls"):
            for call in message["tool_calls"]:
                function=call.get("function",{})
                arguments=function.get("arguments", "")
                if isinstance(arguments,str):
                    try: arguments=json.loads(arguments)
                    except ValueError: arguments={}
                if isinstance(arguments,dict):
                    action_text=" ".join(str(arguments[key]) for key in ("cmd","command","name","path","file_path") if key in arguments)
                    topic.update(concepts(str(function.get("name",""))+" "+action_text[:2000]))
            continue
        text = message_text(message)
        if not text:
            continue
        ref = message.get("evidence_ref") or f"M{i}"
        role = message["role"]
        evidence[ref] = {"text":text,"role":role,"scope":message.get("evidence_scope","own"),
                         "kind":message.get("evidence_kind","request" if role == "user" else "result")}
        # Long tool payloads are evidence for a claim, not an invitation to edit
        # every skill whose topic happens to appear in a search result.
        if role != "tool":
            topic.update(concepts(text[:12000]))
    return evidence, topic


def related_skills(catalog, topic, allowed, *, limit=10):
    ranked = []
    for item in catalog:
        name = item["name"]
        if name not in allowed:
            continue
        terms = concepts(name.replace("-"," ") + " " + item.get("description", "") + " " + " ".join(item.get("aliases",[])))
        overlap = sorted(topic & terms)
        if len(overlap) >= 2:
            ranked.append((len(overlap),name,overlap))
    ranked.sort(key=lambda item:(-item[0],item[1]))
    return {name:overlap for _,name,overlap in ranked[:limit]}


EVIDENCE_SCHEMA = {"type":"object", "properties":{
    "classification":{"type":"string","enum":["project_fact","procedure","stable_preference"]},
    "basis":{"type":"array","minItems":1,"maxItems":6,"items":{"type":"object","properties":{
        "ref":{"type":"string"},"quote":{"type":"string"}},"required":["ref","quote"]}},
    "rationale":{"type":"string","description":"Explain the supported lesson, what changed, and why this target fits."}},
    "required":["classification","basis","rationale"]}


def evidence_tools(tools):
    copied = deepcopy(tools)
    for tool in copied:
        function = tool.get("function", {})
        if function.get("name") not in {"skill_manage","memory"}:
            continue
        parameters = function.setdefault("parameters", {"type":"object"})
        props = parameters.setdefault("properties", {})
        props["evidence"] = deepcopy(EVIDENCE_SCHEMA)
        operations = props.setdefault("operations", {"type":"array","minItems":1,"maxItems":5})
        item = operations.setdefault("items", {"type":"object"})
        item.setdefault("properties", {})["evidence"] = deepcopy(EVIDENCE_SCHEMA)
        note = " Each write operation requires evidence with classification, verbatim basis refs/quotes, and rationale. Evidence is checked by the bridge and removed before the native write."
        if note not in function.get("description", ""):
            function["description"] = function.get("description", "") + note
    return copied


def check_claim(scope, tool_name, operation, proof):
    if not isinstance(proof,dict) or set(proof) - {"classification","basis","rationale"}:
        return None, "learning_evidence_required"
    classification, basis, rationale = proof.get("classification"), proof.get("basis"), proof.get("rationale")
    if (not isinstance(classification,str) or classification not in {"project_fact","procedure","stable_preference"}
            or not isinstance(basis,list) or not 1 <= len(basis) <= 6
            or not isinstance(rationale,str) or not 24 <= len(rationale) <= 2000):
        return None, "learning_evidence_required"
    cited = []
    for item in basis:
        if not isinstance(item,dict) or set(item) != {"ref","quote"}:
            return None, "learning_evidence_invalid_reference"
        ref, quote = item.get("ref"), item.get("quote")
        original = scope["evidence"].get(ref) if isinstance(ref,str) else None
        if not original or not isinstance(quote,str) or not 8 <= len(quote) <= 600 or quote not in original["text"]:
            return None, "learning_evidence_invalid_reference"
        cited.append({**original, "quote":quote})
    own = [item for item in cited if item["scope"] == "own"]
    if not own:
        return None, "learning_evidence_parent_only"
    if classification == "stable_preference" and not any(item["role"] == "user" and item["kind"] == "request" and STABLE.search(item["quote"]) for item in own):
        return None, "learning_preference_not_user_supported"
    if tool_name == "skill_manage" and classification == "project_fact":
        return None, "learning_project_fact_requires_memory"
    if tool_name == "memory" and classification == "procedure":
        return None, "learning_procedure_requires_skill"
    if classification == "procedure" and not any(item["role"] in {"assistant","tool"} or item["kind"] == "material" for item in cited):
        return None, "learning_result_evidence_required"
    quoted_terms = concepts(" ".join(item["quote"] for item in basis))
    matched = concepts(rationale) & quoted_terms
    proposed = (operation.get("content") or operation.get("new_string") or operation.get("file_content") or
                operation.get("old_text") or "")
    if not isinstance(proposed,str):
        return None, "learning_claim_not_supported"
    action,name=operation.get("action"),operation.get("name")
    target_terms=scope.get("target_topics",{}).get(name,[]) if isinstance(name,str) else []
    if (tool_name == "skill_manage" and action != "create" and target_terms
            and "admitted_explicit_target" not in target_terms
            and len(concepts(rationale) & set(target_terms)) < 2):
        return None, "learning_target_claim_not_supported"
    micro_revision = (isinstance(action,str) and action in {"patch","edit","write_file"}
                      and isinstance(name,str) and name in scope.get("owned",set())
                      and (operation.get("action") == "write_file" or "new_string" in operation))
    if not matched or (not micro_revision and len(concepts(proposed) & (scope["topic"] | quoted_terms)) < 2):
        return None, "learning_claim_not_supported"
    return {"classification":classification,"basis":basis,"rationale":rationale,"matched_concepts":sorted(matched)}, None

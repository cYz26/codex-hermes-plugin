"""Explicit learning's model-facing contracts, shared with the stdio MCP surface."""

COMMON = {"cwd": {"type":"string", "description":"Absolute current chat workspace."},
          "request_id": {"type":"string", "description":"Stable ID for retries of this request; use a new ID for a new learning request."}}


def tool(name, description, properties=None, required=()):
    return {"name":name, "description":description, "inputSchema":{"type":"object",
        "properties":{**COMMON, **(properties or {})}, "required":["cwd", "request_id", *required], "additionalProperties":False}}


TOOLS = [
    tool("request_learning", "Request learning only when the user asks to learn/refine. Refine queues a snapshot of admitted completed turns; learn opens a collection for source chunks. Does not imply completed learning.", {
        "chat_id":{"type":"string", "description":"Real host chat ID, e.g. CODEX_THREAD_ID; never infer the latest chat from cwd."},
        "mode":{"type":"string","enum":["refine","learn"],"default":"refine"},
        "focus":{"type":"string","description":"User's learning focus and constraints, at most 4000 UTF-8 bytes."},
        "scope":{"type":"string","enum":["both","memory","skills"],"default":"both"},
        "memory_writes":{"type":"string","enum":["add","revise"],"default":"add",
                         "description":"Use revise only when the user requests correction/update of existing project memory."},
        "through_turn":{"type":"string","description":"Optional captured cutoff turn; unfinished turns return waiting_for_turn."},
        "skill_name":{"type":"string","description":"Required for learn: a descriptive learned-<topic> target, reused across batches."},
    }, ("chat_id",)),
    tool("add_learning_material", "Add one user-selected source chunk to a collecting learn request. At most 24 KiB per chunk and 64 chunks; use stable chunk IDs for retries. Existing data admission applies.", {
        "chunk_id":{"type":"string"}, "source":{"type":"string","description":"Source URL or path, at most 512 UTF-8 bytes."},
        "title":{"type":"string","description":"Topic title, at most 160 UTF-8 bytes."},
        "content":{"type":"string","description":"Material actually read or supplied by the user, at most 24 KiB UTF-8."},
    }, ("chunk_id","source","content")),
    tool("start_learning", "Seal collected materials and queue learning batches followed by reference-index verification. Idempotent; no more materials may be added after sealing."),
    tool("learning_request_status", "Read this project's request progress, native outcomes and artifact publication decisions. Does not start learning."),
    tool("cancel_learning", "Cancel queued work and future material batches. A running batch may finish; already applied writes are retained."),
]

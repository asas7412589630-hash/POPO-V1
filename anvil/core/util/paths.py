import os

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# layout
POLICY_SOURCE = os.path.join(BASE, "policy", "source")
POLICY_COMPILED = os.path.join(BASE, "policy", "compiled")
RUNTIME_DIR = os.path.join(BASE, "runtime")
RUNTIME_STATE = os.path.join(RUNTIME_DIR, "state")
RUNTIME_REQ = os.path.join(RUNTIME_DIR, "req")
LOG_DIR = os.path.join(BASE, "log")
ARTIFACTS_DIR = os.path.join(BASE, "artifacts")

# env overrides (keep compatibility with old envs)
PROMPT_FILE = os.environ.get("ARMOR_PROMPT_FILE") or os.path.join(POLICY_SOURCE, "armor", "gpt-5.2_critique.txt")
MAP_FILE = os.environ.get("ARMOR_MAP_FILE") or os.path.join(POLICY_SOURCE, "armor_map_active.json")
ALIAS_FILE = os.environ.get("ARMOR_ALIAS_FILE") or os.path.join(POLICY_SOURCE, "armor_alias.json")
EFFORT_FILE = os.environ.get("ARMOR_EFFORT_FILE") or os.path.join(POLICY_SOURCE, "armor_effort.json")
FAMILY_FILE = os.environ.get("ARMOR_FAMILY_FILE") or os.path.join(POLICY_SOURCE, "armor_family.json")

STATE_FILE = os.environ.get("ARMOR_STATE_FILE") or os.path.join(RUNTIME_STATE, "session_state.json")
ARCHIVE_DIR = os.environ.get("ARMOR_ARCHIVE_DIR") or RUNTIME_STATE

LAST_REQUEST_FILE = os.environ.get("ARMOR_LAST_REQUEST_FILE") or os.path.join(ARTIFACTS_DIR, "last_request.json")
LAST_BODY_FILE = os.environ.get("ARMOR_DUMP_BODY") or os.path.join(ARTIFACTS_DIR, "last_body.json")
INCOMING_DUMP = os.environ.get("ARMOR_INCOMING_DUMP") or os.path.join(ARTIFACTS_DIR, "last_incoming.json")

SCHEMA_CACHE_FILE = os.environ.get("ARMOR_SCHEMA_CACHE_FILE") or os.path.join(RUNTIME_STATE, "schema_cache.json")

# ensure base dirs exist
for d in (POLICY_SOURCE, POLICY_COMPILED, RUNTIME_STATE, RUNTIME_REQ, LOG_DIR, ARTIFACTS_DIR):
    os.makedirs(d, exist_ok=True)

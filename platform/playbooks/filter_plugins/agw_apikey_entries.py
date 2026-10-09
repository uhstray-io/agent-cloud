"""The apiKey enrolments of one rendered agentgateway config (rollback-inference-route.yml).

The rollback compares the key enrolments of the kept config against the live one. Those files
hold key hashes (or, under local-dev's agw_plaintext_keys, the keys), so the parse happens in
this filter, inside a `no_log` task, and what comes back is native data: no JSON text is built
and re-read in Jinja, which is what made the extraction depend on the ansible-core version
(2.16 turns the macro's text into a Python tuple before `from_json` sees it).

The filter takes the base64 `content` that `slurp` returns and answers a mapping:

    entries   the `keys` entries of every `apiKey` mapping, at any depth, in document order
              (anchors, aliases and merge keys are already resolved by the YAML loader).
              A `keys` that is not a list is ignored; an entry that is not a mapping, and an
              empty one, enrols nobody nameable and is dropped.
    problem   "" when the file parsed, else the CLASS of the failure and nothing else:
              "not YAML", "not a mapping", "nested too deeply" or
              "unreadable structure". An empty document is a
              file that enrols nobody (problem "").

Names only, never content: a parser error quotes the line it stopped at, which in these files
is a key, so no exception text leaves this module. The visible refusal in the playbook gets
the class and the file name.
"""

import base64
import json

import yaml


def _entries(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "apiKey" and isinstance(value, dict) and isinstance(value.get("keys"), list):
                out.extend(e for e in value["keys"] if isinstance(e, dict) and e)
            _entries(value, out)
    elif isinstance(node, list):
        for value in node:
            _entries(value, out)


def agw_apikey_entries(content):
    """See the module docstring. `content` is a slurp result's base64 text."""
    try:
        text = base64.b64decode(content, validate=True).decode("utf-8")
        doc = yaml.safe_load(text)
    except Exception:
        return {"entries": [], "problem": "not YAML"}
    if doc is None:
        return {"entries": [], "problem": ""}
    if not isinstance(doc, dict):
        return {"entries": [], "problem": "not a mapping"}
    found = []
    try:
        _entries(doc, found)
        # Plain JSON-safe data (a YAML date becomes its text), so a fact built from it
        # serialises the same on every ansible-core.
        entries = json.loads(json.dumps(found, default=str))
    except RecursionError:
        return {"entries": [], "problem": "nested too deeply"}
    except Exception:
        return {"entries": [], "problem": "unreadable structure"}
    return {"entries": entries, "problem": ""}


class FilterModule:
    def filters(self):
        return {"agw_apikey_entries": agw_apikey_entries}

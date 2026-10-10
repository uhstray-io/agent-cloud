#!/usr/bin/env python3
"""Survey or narrowly repair the root of the journal collector state volume."""

import json
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime

IMAGE = "docker.io/grafana/alloy:v1.9.2"
RECEIVER = "o11y-alloy"
COLLECTOR = "o11y-journal-collector"
VOLUME_KEY = "journal-collector-state"
DATA_PATH = "/alloy-state"
MAX_VOLUMES = 256
MAX_CONTAINERS = 1024
MAX_CHILDREN = 32
MAX_JSON_BYTES = 1_048_576
MIN_FREE_BYTES = 16 * 1024 * 1024
MIN_FREE_INODES = 128
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]{1,32})?\Z")
_CREATED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)

_SPACE_SCRIPT = r'''import json,os,stat,sys
path=sys.argv[1]
try:
    st=os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or os.path.realpath(path)!=path: raise ValueError
    space=os.statvfs(path)
    print(json.dumps({"status":"observed","free_bytes":space.f_bavail*space.f_frsize,"free_inodes":space.f_favail}))
except (OSError,ValueError):
    print(json.dumps({"status":"unavailable"}))
'''


def _call(argv, timeout=15, run=subprocess.run):
    try:
        result = run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    if not isinstance(result.stdout, str) or len(result.stdout) > MAX_JSON_BYTES:
        return None
    return result


def _json(result):
    if result is None or result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    return value


def _rows(value):
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) and all(isinstance(row, dict) for row in value) else None


def _container_names(value):
    rows = _rows(value)
    if rows is None:
        return None
    names = []
    for row in rows:
        name = row.get("Names", row.get("Name"))
        if isinstance(name, list) and len(name) == 1:
            name = name[0]
        if not isinstance(name, str) or not name:
            return None
        names.append(name.lstrip("/"))
    return names


_METADATA_SCRIPT = r'''import hashlib,json,os,re,stat,sys,time
root=sys.argv[1]
read_cursor=sys.argv[2]=="true"
def mount_id(path):
    best=(-1,None)
    with open("/proc/self/mountinfo",encoding="utf-8") as stream:
        for line in stream:
            left=line.split(" - ",1)[0].split()
            raw=left[4]
            mount=re.sub(r"\\([0-7]{3})",lambda m:chr(int(m.group(1),8)),raw)
            if path==mount or path.startswith(mount.rstrip("/")+"/"):
                if len(mount)>best[0]: best=(len(mount),(left[0],mount))
    if best[1] is None: raise ValueError("mount unavailable")
    return best[1]
def acl(path):
    names=os.listxattr(path,follow_symlinks=False)
    return any("acl" in name.lower() for name in names)
def sig(name, st):
    kind = "other"
    if stat.S_ISREG(st.st_mode): kind="file"
    elif stat.S_ISDIR(st.st_mode): kind="dir"
    elif stat.S_ISLNK(st.st_mode): kind="symlink"
    return {
        "name":hashlib.sha256(os.fsencode(name)).hexdigest(), "kind":kind,
        "uid":st.st_uid, "gid":st.st_gid, "mode":stat.S_IMODE(st.st_mode),
        "nlink":st.st_nlink, "dev":st.st_dev, "ino":st.st_ino,
    }
def scalar(value):
    if len(value)>=2 and value[0]==value[-1]=="'": return value[1:-1].replace("''", "'")
    if len(value)>=2 and value[0]==value[-1]=='"': return json.loads(value)
    return value
def journal_cursor(data):
    try: content=data.decode("utf-8")
    except UnicodeDecodeError: return False
    match=re.fullmatch(
        r"positions:\n  \? path: ([^\n]+)\n    labels: ([^\n]+)\n  : ([^\n]+)\n?",
        content)
    if not match: return False
    path,labels,cursor=(scalar(value) for value in match.groups())
    cursor_format=r"s=[0-9a-f]+;i=[0-9a-f]+;b=[0-9a-f]+;m=[0-9a-f]+;t=[0-9a-f]+;x=[0-9a-f]+"
    return (path=="cursor-loki.source.journal.o11y_alloy" and labels==""
        and re.fullmatch(cursor_format, cursor) is not None)
class RetryObservation(Exception): pass
for observation_attempt in range(2):
 try:
    st=os.lstat(root)
    if not stat.S_ISDIR(st.st_mode) or os.path.realpath(root)!=root: raise ValueError("root ambiguous")
    root_mount=mount_id(root)
    mount_exact=False
    with open("/proc/self/mountinfo",encoding="utf-8") as stream:
        for line in stream:
            left=line.split(" - ",1)[0].split()
            mount=re.sub(r"\\([0-7]{3})",lambda m:chr(int(m.group(1),8)),left[4])
            mount_exact=mount_exact or mount==root
    names=os.listdir(root)
    if len(names)>32: raise OverflowError("children unbounded")
    children=[]
    acl_found=acl(root)
    child_mount=False
    component_layout=st.st_mode & (0o022|0o7000) == 0
    cursor_valid=False
    cursor_presence="absent"
    free=os.statvfs(root)
    free_bytes=free.f_bavail*free.f_frsize
    free_inodes=free.f_favail
    for name in sorted(names):
        path=os.path.join(root,name)
        item=os.lstat(path)
        item_mount=mount_id(path)
        child_mount=child_mount or item_mount[0]!=root_mount[0]
        acl_found=acl_found or acl(path)
        if stat.S_ISREG(item.st_mode):
            component_layout=(component_layout and name=="alloy_seed.json"
                and item.st_uid==0 and item.st_gid==0 and item.st_nlink==1
                and item.st_mode & 0o077 == 0 and item.st_mode & 0o7133 == 0)
        elif stat.S_ISDIR(item.st_mode) and name=="loki.source.journal.o11y_alloy":
            component_layout=(component_layout and item.st_uid==0 and item.st_gid==0
                and item.st_nlink==2 and item.st_mode & 0o700 == 0o700
                and item.st_mode & (0o022|0o7000) == 0)
            nested=os.listdir(path)
            if len(nested)>2:
                component_layout=False
            temp_names=[]
            directory_fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            for nested_name in nested:
                nested_path=os.path.join(path,nested_name)
                try:
                    nested_stat=os.lstat(nested_path)
                except FileNotFoundError:
                    if re.fullmatch(r"\.positions\.yml[0-9]{1,19}",nested_name):
                        raise RetryObservation
                    raise
                nested_mount=mount_id(nested_path)
                nested_acl=acl(nested_path)
                acl_found=acl_found or nested_acl
                child_mount=child_mount or nested_mount[0]!=root_mount[0]
                safe_file=(stat.S_ISREG(nested_stat.st_mode) and nested_stat.st_uid==0
                    and nested_stat.st_gid==0 and nested_stat.st_nlink==1 and not nested_acl
                    and nested_stat.st_mode & 0o600 == 0o600
                    and nested_stat.st_mode & 0o077 == 0
                    and nested_stat.st_mode & 0o7133 == 0)
                if nested_name=="positions.yml":
                    cursor_presence="present"
                    component_layout=(component_layout and safe_file)
                    cursor_valid=False
                    if read_cursor and safe_file and nested_stat.st_size<=65536:
                        try:
                            fd=os.open(nested_name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=directory_fd)
                            try:
                                opened=os.fstat(fd)
                                data=os.read(fd,65537)
                                after=os.fstat(fd)
                                cursor_valid=((opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)==
                                    (nested_stat.st_dev,nested_stat.st_ino,nested_stat.st_size,nested_stat.st_mtime_ns)
                                    and (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)==
                                    (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)
                                    and len(data)==opened.st_size and journal_cursor(data))
                            finally: os.close(fd)
                        except OSError:
                            cursor_valid=False
                elif re.fullmatch(r"\.positions\.yml[0-9]{1,19}",nested_name):
                    temp_names.append(nested_name)
                    component_layout=(component_layout and safe_file)
                else:
                    component_layout=False
            if len(temp_names)>1:
                component_layout=False
            if temp_names:
                time.sleep(0.1)
                after=os.listdir(path)
                if len(after)==1 and after[0]=="positions.yml":
                    os.close(directory_fd)
                    raise RetryObservation
                component_layout=False
            os.close(directory_fd)
        else:
            component_layout=False
        children.append(sig(name,item))
    report = {
        "status":"observed",
        "root":{"uid":st.st_uid, "gid":st.st_gid, "mode":stat.S_IMODE(st.st_mode),
               "dev":st.st_dev, "ino":st.st_ino},
        "children":children, "child_count":len(children), "acl":acl_found,
        "root_is_mount":mount_exact, "child_mount":child_mount,
        "component_layout":component_layout,
        "journal_cursor_presence":cursor_presence,
        "journal_cursor_valid":cursor_valid,
        "free_bytes":free_bytes,
        "free_inodes":free_inodes,
    }
    print(json.dumps(report,sort_keys=True))
    break
 except RetryObservation:
    if observation_attempt == 0:
        continue
    print(json.dumps({"status":"unavailable"}))
    break
 except OverflowError:
    print(json.dumps({"status":"unbounded"}))
    break
 except (OSError,ValueError,IndexError):
    print(json.dumps({"status":"unavailable"}))
    break
'''

_CHOWN_SCRIPT = r'''import hashlib,json,os,re,stat,sys
root=sys.argv[1]
expected=json.loads(sys.argv[2])
target_uid=int(sys.argv[3])
target_gid=int(sys.argv[4])
def fd_mount_id(fd):
    with open(f"/proc/self/fdinfo/{fd}",encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("mnt_id:"): return int(line.split()[1])
    raise ValueError
def mount_ids():
    found=set()
    with open("/proc/self/mountinfo",encoding="utf-8") as stream:
        for line in stream:
            left=line.split(" - ",1)[0].split()
            mount=re.sub(r"\\([0-7]{3})",lambda m:chr(int(m.group(1),8)),left[4])
            found.add(mount)
    return found
root_fd=None
try:
    before=os.lstat(root)
    if not stat.S_ISDIR(before.st_mode) or os.path.realpath(root)!=root: raise ValueError
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    st=os.fstat(root_fd)
    if (st.st_dev,st.st_ino)!=(before.st_dev,before.st_ino): raise ValueError
    root_state = {
        "uid":st.st_uid, "gid":st.st_gid, "mode":stat.S_IMODE(st.st_mode),
        "dev":st.st_dev, "ino":st.st_ino,
    }
    if root_state != expected["root"]: raise ValueError
    root_mount=fd_mount_id(root_fd)
    mounts=mount_ids()
    if root in mounts: raise ValueError
    names=os.listdir(root_fd)
    if len(names)>32 or len(names)!=len(expected["children"]): raise ValueError
    for name,want in zip(sorted(names),expected["children"]):
        child_fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=root_fd)
        try:
            item=os.fstat(child_fd)
            child_mount=fd_mount_id(child_fd)
            child_acl=any("acl" in x.lower() for x in os.listxattr(child_fd))
        finally:
            os.close(child_fd)
        kind="other"
        if stat.S_ISREG(item.st_mode): kind="file"
        elif stat.S_ISDIR(item.st_mode): kind="dir"
        elif stat.S_ISLNK(item.st_mode): kind="symlink"
        got={"name":hashlib.sha256(os.fsencode(name)).hexdigest(),"kind":kind,
             "uid":item.st_uid,"gid":item.st_gid,"mode":stat.S_IMODE(item.st_mode),
             "nlink":item.st_nlink,"dev":item.st_dev,"ino":item.st_ino}
        if (got!=want or not stat.S_ISREG(item.st_mode) or item.st_nlink!=1
                or child_mount!=root_mount or child_acl): raise ValueError
    if any("acl" in x.lower() for x in os.listxattr(root_fd)): raise ValueError
    path_state=os.lstat(root)
    pinned=os.fstat(root_fd)
    expected_root=expected["root"]
    path_root={"uid":path_state.st_uid,"gid":path_state.st_gid,"mode":stat.S_IMODE(path_state.st_mode),
               "dev":path_state.st_dev,"ino":path_state.st_ino}
    pinned_root={"uid":pinned.st_uid,"gid":pinned.st_gid,"mode":stat.S_IMODE(pinned.st_mode),
                 "dev":pinned.st_dev,"ino":pinned.st_ino}
    if path_root!=expected_root or pinned_root!=expected_root: raise ValueError
    if os.path.realpath(root)!=root: raise ValueError
    os.chown(root_fd,target_uid,target_gid)
    after=os.fstat(root_fd)
    after_state=(after.st_uid,after.st_gid,stat.S_IMODE(after.st_mode),after.st_dev,after.st_ino)
    expected_after=(target_uid,target_gid,expected["root"]["mode"],expected["root"]["dev"],expected["root"]["ino"])
    if after_state!=expected_after: raise ValueError
    print(json.dumps({"status":"changed"}))
except (OSError,ValueError,IndexError):
    print(json.dumps({"status":"refused"}))
finally:
    if root_fd is not None: os.close(root_fd)
'''

_CHMOD_PENDING_SCRIPT = r'''import json,os,re,stat,sys
root=sys.argv[1]
expected=json.loads(sys.argv[2])
target_mode=int(sys.argv[3])
root_fd=None
mutation_started=False
try:
    before=os.lstat(root)
    if not stat.S_ISDIR(before.st_mode) or os.path.realpath(root)!=root: raise ValueError
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    st=os.fstat(root_fd)
    if (st.st_dev,st.st_ino)!=(before.st_dev,before.st_ino): raise ValueError
    state={"uid":st.st_uid,"gid":st.st_gid,"mode":stat.S_IMODE(st.st_mode),
           "dev":st.st_dev,"ino":st.st_ino}
    if state!=expected["root"] or os.listdir(root_fd): raise ValueError
    if target_mode != (state["mode"] & ~0o022) or target_mode == state["mode"]: raise ValueError
    if state["mode"] & 0o700 != 0o700 or state["mode"] & 0o7000: raise ValueError
    if any("acl" in name.lower() for name in os.listxattr(root_fd)): raise ValueError
    mounts=set()
    with open("/proc/self/mountinfo",encoding="utf-8") as stream:
        for line in stream:
            left=line.split(" - ",1)[0].split()
            mount=re.sub(r"\\([0-7]{3})",lambda m:chr(int(m.group(1),8)),left[4])
            mounts.add(mount)
    if root in mounts: raise ValueError
    path_state=os.lstat(root)
    pinned=os.fstat(root_fd)
    path_state={"uid":path_state.st_uid,"gid":path_state.st_gid,"mode":stat.S_IMODE(path_state.st_mode),
                "dev":path_state.st_dev,"ino":path_state.st_ino}
    pinned_state={"uid":pinned.st_uid,"gid":pinned.st_gid,"mode":stat.S_IMODE(pinned.st_mode),
                  "dev":pinned.st_dev,"ino":pinned.st_ino}
    if path_state!=expected["root"] or pinned_state!=expected["root"]: raise ValueError
    if os.path.realpath(root)!=root: raise ValueError
    mutation_started=True
    os.fchmod(root_fd,target_mode)
    after=os.fstat(root_fd)
    expected_after=(state["uid"],state["gid"],target_mode,state["dev"],state["ino"])
    actual_after=(after.st_uid,after.st_gid,stat.S_IMODE(after.st_mode),after.st_dev,after.st_ino)
    if actual_after!=expected_after: raise ValueError
    print(json.dumps({"status":"changed"}))
except Exception:
    print(json.dumps({"status":"uncertain" if mutation_started else "refused"}))
finally:
    if root_fd is not None: os.close(root_fd)
'''


def _inspect_metadata(path, read_cursor=False, run=subprocess.run):
    result = _call(["podman", "unshare", "python3", "-c", _METADATA_SCRIPT, path,
                    "true" if read_cursor else "false"], run=run)
    value = _json(result)
    if result is None or result.returncode != 0 or not isinstance(value, dict):
        return None
    if value.get("status") not in {"observed", "unavailable", "unbounded"}:
        return None
    return value


def _store_space(run=subprocess.run):
    result = _call(["podman", "info", "--format={{.Store.GraphRoot}}"], run=run)
    if result is None or result.returncode != 0:
        return None
    path = result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout
    if (
        not path.startswith("/")
        or len(path) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        return None
    result = _call(["podman", "unshare", "python3", "-c", _SPACE_SCRIPT, path], run=run)
    value = _json(result)
    if result is None or result.returncode != 0 or not isinstance(value, dict) or value.get("status") != "observed":
        return None
    if type(value.get("free_bytes")) is not int or type(value.get("free_inodes")) is not int:
        return None
    return value


def _field_diagnostics(value):
    fields = {}
    for name in ("NeedsChown", "NeedsCopyUp"):
        if not isinstance(value, dict) or name not in value:
            fields[name] = {"presence": "missing", "type": "missing", "value": "unavailable"}
            continue
        item = value[name]
        if item is None:
            kind = "null"
        elif type(item) is bool:
            kind = "boolean"
        elif isinstance(item, str):
            kind = "string"
        elif isinstance(item, (int, float)):
            kind = "number"
        elif isinstance(item, list):
            kind = "array"
        elif isinstance(item, dict):
            kind = "object"
        else:
            kind = "other"
        fields[name] = {
            "presence": "present",
            "type": kind,
            "value": item if type(item) is bool else "unavailable",
        }
    return fields


def _unverified_fields():
    return {
        name: {"presence": "unverified", "type": "unverified", "value": "unavailable"}
        for name in ("NeedsChown", "NeedsCopyUp")
    }


def _unverified_diagnostics():
    fields = _unverified_fields()
    return {
        "inventory_fields": fields,
        "named_volume": {
            "outcome": "unverified",
            "identity": "unverified",
            "inventory_identity": "unverified",
            "fields": fields.copy(),
            "template_fields": fields.copy(),
            "template_identity": "unverified",
        },
        "podman_version": {"client": "unavailable", "server": "unavailable"},
    }


def _volume_identity(value, name, project):
    if not isinstance(value, dict):
        return "unverified"
    if "Name" in value and value["Name"] != name:
        return "mismatch"
    labels = value.get("Labels")
    if not isinstance(labels, dict) or not isinstance(value.get("Name"), str):
        return "unverified"
    if "com.docker.compose.project" in labels and labels["com.docker.compose.project"] != project:
        return "mismatch"
    if "com.docker.compose.volume" in labels and labels["com.docker.compose.volume"] != VOLUME_KEY:
        return "mismatch"
    if (
        value["Name"] != name
        or labels.get("com.docker.compose.project") != project
    ):
        return "unverified"
    return "match"


def _volume_identity_tuple(value):
    if not isinstance(value, dict):
        return None
    labels = value.get("Labels")
    if not isinstance(labels, dict):
        return None
    return tuple(value.get(field) for field in (
        "Name", "Driver", "Scope", "Options", "Mountpoint", "CreatedAt"
    )) + (
        tuple(sorted(labels.items())),
    )


def _valid_created_at(value):
    if not isinstance(value, str) or not _CREATED_AT_RE.fullmatch(value):
        return False
    try:
        created_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    fraction = re.search(r"\.([0-9]{1,9})", value)
    return (
        created_at != datetime.min.replace(tzinfo=UTC)
        or (fraction is not None and any(digit != "0" for digit in fraction.group(1)))
    )


def _named_volume_flags(name, project, inventory_volume, run):
    template = (
        '{{.Name}}\n{{index .Labels "com.docker.compose.project"}}\n'
        '{{with index .Labels "com.docker.compose.volume"}}{{.}}{{end}}\n'
        "{{.NeedsChown}}\n{{.NeedsCopyUp}}"
    )

    def inspect():
        result = _call(["podman", "volume", "inspect", name], run=run)
        rows = _rows(_json(result))
        if result is None or result.returncode != 0 or rows is None or len(rows) != 1:
            return None
        return rows[0]

    before = inspect()
    template_result = _call(["podman", "volume", "inspect", "--format", template, name], run=run)
    after = inspect()
    fields = _field_diagnostics(before) if isinstance(before, dict) else _unverified_fields()
    base = {
        "outcome": "unavailable",
        "identity": "unverified",
        "inventory_identity": "unverified",
        "fields": fields,
        "template_fields": _unverified_fields(),
        "template_identity": "unverified",
    }
    if before is None or after is None or template_result is None or template_result.returncode != 0:
        return {**base, "outcome": "unavailable", "flags": None}
    before_identity = _volume_identity(before, name, project)
    after_identity = _volume_identity(after, name, project)
    inventory_identity = _volume_identity(inventory_volume, name, project)
    template_lines = template_result.stdout.splitlines()
    template_identity = "match"
    if len(template_lines) != 5:
        template_identity = "unverified"
    elif (
        template_lines[0] != name
        or template_lines[1] != project
        or template_lines[2] not in ("", VOLUME_KEY)
    ):
        template_identity = "mismatch"
    if "mismatch" in (before_identity, after_identity, inventory_identity, template_identity):
        return {
            **base, "outcome": "identity_mismatch", "identity": "mismatch",
            "inventory_identity": "mismatch", "template_identity": template_identity,
            "flags": None,
        }
    if not all(item == "match" for item in (before_identity, after_identity, inventory_identity, template_identity)):
        return {**base, "outcome": "identity_unverified", "flags": None}
    if _volume_identity_tuple(before) != _volume_identity_tuple(after) or (
        _volume_identity_tuple(inventory_volume) != _volume_identity_tuple(before)
    ):
        return {**base, "outcome": "identity_changed", "identity": "mismatch", "flags": None}
    if before != after:
        return {**base, "outcome": "identity_changed", "identity": "mismatch", "flags": None}

    raw_flags = {}
    effective_flags = {}
    for index, field in enumerate(("NeedsChown", "NeedsCopyUp"), start=3):
        raw = template_lines[index]
        if raw not in ("false", "true"):
            return {**base, "outcome": "flag_unverified", "identity": "match", "inventory_identity": "match",
                    "template_identity": "match", "flags": None}
        effective = raw == "true"
        for row in (inventory_volume, before, after):
            if field in row and (type(row[field]) is not bool or row[field] is not effective):
                return {**base, "outcome": "flag_mismatch", "identity": "match", "inventory_identity": "match",
                        "template_identity": "match", "flags": None}
        effective_flags[field] = effective
        raw_flags[field] = {"presence": "present", "type": "boolean", "value": effective}
    return {
        "outcome": "observed", "identity": "match", "inventory_identity": "match",
        "fields": fields,
        "template_fields": raw_flags,
        "template_identity": "match",
        "flags": effective_flags,
    }


def _named_volume_diagnostics(readback):
    return {key: value for key, value in readback.items() if key != "flags"}


def _version_diagnostics(run):
    result = _call(["podman", "version", "--format", "json"], run=run)
    value = _json(result)
    if result is None or result.returncode != 0 or not isinstance(value, dict):
        return {"client": "unavailable", "server": "unavailable"}

    def version(section):
        section_value = value.get(section)
        raw = section_value.get("Version") if isinstance(section_value, dict) else None
        if isinstance(raw, str) and len(raw) <= 64 and _VERSION_RE.fullmatch(raw):
            return raw
        return "unavailable"

    return {"client": version("Client"), "server": version("Server")}


def discover(run=subprocess.run, diagnostics=False, read_cursor=False):
    rootless = _call(["podman", "info", "--format={{.Host.Security.Rootless}}"], run=run)
    if rootless is None or rootless.returncode != 0 or rootless.stdout.strip().lower() != "true":
        return {"status": "not_rootless"}

    receiver = _json(_call(["podman", "inspect", "--type", "container", "--format", "json", RECEIVER], run=run))
    receiver_rows = _rows(receiver)
    if receiver_rows is None or len(receiver_rows) != 1:
        return {"status": "receiver_unavailable"}
    labels = receiver_rows[0].get("Config", {}).get("Labels")
    project = labels.get("com.docker.compose.project") if isinstance(labels, dict) else None
    service = labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
    if not isinstance(project, str) or not project or service != "alloy":
        return {"status": "receiver_identity_unverified"}

    volume_rows = _rows(_json(_call(["podman", "volume", "inspect", "--all"], run=run)))
    if volume_rows is None or len(volume_rows) > MAX_VOLUMES:
        return {"status": "volume_inventory_unavailable"}
    all_containers = _rows(_json(_call(["podman", "ps", "--all", "--format", "json"], run=run)))
    container_names = _container_names(all_containers)
    if container_names is None or len(container_names) > MAX_CONTAINERS:
        return {"status": "container_inventory_unavailable"}
    collector_present = COLLECTOR in container_names
    expected_name = f"{project}_{VOLUME_KEY}"
    named = []
    labelled = []
    for volume in volume_rows:
        if volume.get("Name") == expected_name:
            named.append(volume)
        volume_labels = volume.get("Labels")
        if (
            isinstance(volume_labels, dict)
            and volume_labels.get("com.docker.compose.project") == project
            and volume_labels.get("com.docker.compose.volume") == VOLUME_KEY
        ):
            labelled.append(volume)
    if len(named) > 1 or len(labelled) > 1:
        return {"status": "volume_ambiguous"}
    if not named and not labelled:
        return {
            "status": "volume_missing",
            "project": project,
            "volume_name": expected_name,
            "volume_inventory_complete": True,
            "name_collision": False,
            "collector_present": collector_present,
            "storage_space": _store_space(run),
        }
    volume_labels = named[0].get("Labels") if len(named) == 1 else None
    if (
        len(named) != 1
        or not isinstance(volume_labels, dict)
        or volume_labels.get("com.docker.compose.project") != project
        or (
            "com.docker.compose.volume" in volume_labels
            and volume_labels.get("com.docker.compose.volume") != VOLUME_KEY
        )
        or (labelled and labelled[0] is not named[0])
    ):
        return {"status": "volume_identity_unsupported"}
    volume = named[0]
    name = volume.get("Name")
    mountpoint = volume.get("Mountpoint")
    if (
        not isinstance(name, str)
        or not name
        or volume.get("Driver") != "local"
        or volume.get("Scope") != "local"
        or volume.get("Options") not in ({}, None)
        or not isinstance(mountpoint, str)
        or not mountpoint.startswith("/")
        or volume.get("Anonymous", False)
    ):
        return {"status": "volume_identity_unsupported"}

    named_flags = _named_volume_flags(name, project, volume, run)
    survey_diagnostics = None
    if diagnostics:
        survey_diagnostics = {
            "inventory_fields": _field_diagnostics(volume),
            "named_volume": _named_volume_diagnostics(named_flags),
            "podman_version": _version_diagnostics(run),
        }

    def finish(found):
        if survey_diagnostics is not None:
            found["survey_diagnostics"] = survey_diagnostics
        return found

    if named_flags.get("flags") is None:
        return finish({
            "status": "volume_initialization_unverified",
            "volume_use_count": 0,
            "collector_present": collector_present,
        })

    users = _rows(_json(_call(["podman", "ps", "--all", "--filter", f"volume={name}", "--format", "json"], run=run)))
    if users is None:
        return finish({"status": "volume_users_unavailable"})
    names = _container_names(users)
    if names is None or len(names) > 32 or any(item != COLLECTOR for item in names) or len(names) > 1:
        return finish({
            "status": "volume_shared",
            "volume_use_count": min(len(users), 33),
            "collector_present": COLLECTOR in (names or []),
        })
    mount_count = volume.get("MountCount")
    if not isinstance(mount_count, int) or mount_count not in (0, 1) or (not names and mount_count != 0):
        return finish({
            "status": "mount_count_unverified",
            "volume_use_count": len(names),
            "collector_present": bool(names),
        })
    collector_mount_verified = False
    if names:
        inspect_collector = ["podman", "inspect", "--type", "container", "--format", "json", COLLECTOR]
        collector = _rows(_json(_call(inspect_collector, run=run)))
        if collector is None or len(collector) != 1:
            return finish({"status": "collector_mount_unverified", "volume_use_count": 1, "collector_present": True})
        mounts = collector[0].get("Mounts")
        exact = (
            [item for item in mounts if isinstance(item, dict) and item.get("Destination") == DATA_PATH]
            if isinstance(mounts, list) else []
        )
        if (
            len(exact) != 1
            or exact[0].get("Name") != name
            or exact[0].get("Type") != "volume"
            or exact[0].get("Source") != mountpoint
            or exact[0].get("RW") is not True
        ):
            return finish({"status": "collector_mount_unverified", "volume_use_count": 1, "collector_present": True})
        config = collector[0].get("Config")
        if not isinstance(config, dict) or config.get("User") != "0:0":
            return finish({"status": "collector_identity_unverified", "volume_use_count": 1, "collector_present": True})
        collector_mount_verified = True
    elif collector_present:
        return finish({"status": "collector_mount_unverified", "volume_use_count": 0, "collector_present": True})

    metadata = _inspect_metadata(mountpoint, read_cursor=read_cursor, run=run)
    if metadata is None or metadata.get("status") != "observed":
        return finish({
            "status": "metadata_unavailable",
            "volume_use_count": len(names),
            "collector_present": bool(names),
        })
    needs_chown = named_flags["flags"]["NeedsChown"]
    needs_copy_up = named_flags["flags"]["NeedsCopyUp"]
    found = {
        "status": "volume_initialization_pending" if needs_chown or needs_copy_up else "observed",
        "volume_name": name,
        "mountpoint": mountpoint,
        "volume_use_count": len(names),
        "mount_count": mount_count,
        "collector_present": collector_present,
        "collector_mount_verified": collector_mount_verified,
        "project": project,
        "volume": volume,
        "needs_chown": needs_chown,
        "needs_copy_up": needs_copy_up,
        "volume_inventory_complete": True,
        "name_collision": False,
        "metadata": metadata,
        "storage_space": _store_space(run),
    }
    return finish(found)


def _access(metadata, uid=0, gid=0):
    root = metadata["root"]
    mode = root["mode"]
    if root["uid"] == uid:
        allowed = (mode >> 6) & 7
    elif root["gid"] == gid:
        allowed = (mode >> 3) & 7
    else:
        allowed = mode & 7
    return allowed & 7 == 7


def _safe_metadata(found):
    metadata = found.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("status") != "observed":
        return False
    root = metadata.get("root")
    children = metadata.get("children")
    if not isinstance(root, dict) or not isinstance(children, list) or len(children) > MAX_CHILDREN:
        return False
    if (
        metadata.get("acl") is not False
        or metadata.get("root_is_mount") is not False
        or metadata.get("child_mount") is not False
    ):
        return False
    return not any(
        not isinstance(item, dict)
        or item.get("kind") != "file"
        or item.get("uid") != 0
        or item.get("gid") != 0
        or item.get("nlink") != 1
        for item in children
    )


def _live_safe_metadata(found, require_cursor=True):
    metadata = found.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("status") != "observed":
        return False
    root = metadata.get("root")
    children = metadata.get("children")
    return (
        isinstance(root, dict)
        and root.get("uid") == 0
        and root.get("gid") == 0
        and isinstance(children, list)
        and len(children) <= MAX_CHILDREN
        and metadata.get("component_layout") is True
        and (
            not require_cursor
            or (
                metadata.get("journal_cursor_presence") == "present"
                and metadata.get("journal_cursor_valid") is True
            )
        )
        and metadata.get("acl") is False
        and metadata.get("root_is_mount") is False
        and metadata.get("child_mount") is False
    )


def _summary(found):
    metadata = found.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("status") != "observed":
        return {
            "status": found.get("status", "unavailable"),
            "volume_use_count": found.get("volume_use_count", 0),
            "collector_present": found.get("collector_present", False),
            "entry_count": 0,
            "root_owner": "unavailable",
            "root_mode_access": "unavailable",
            "acl": "unavailable",
            "mount": "unavailable",
            "children": "unavailable",
            "journal_cursor": "unavailable",
            "free_space": "unavailable",
        }
    owner_matches = metadata["root"]["uid"] == 0 and metadata["root"]["gid"] == 0
    return {
        "status": found.get("status", "unavailable"),
        "volume_use_count": found["volume_use_count"],
        "collector_present": found["collector_present"],
        "entry_count": metadata["child_count"],
        "root_owner": "matches_collector" if owner_matches else "mismatch",
        "root_mode_access": "read_write_execute" if _access(metadata) else "blocked",
        "acl": "present" if metadata["acl"] else "absent",
        "mount": "ambiguous" if metadata["root_is_mount"] or metadata["child_mount"] else "clear",
        "children": "safe_regular_files" if _safe_metadata(found) else "ambiguous",
        "journal_cursor": "valid" if metadata.get("journal_cursor_valid") is True else "unavailable",
        "free_space": "sufficient" if (
            type(metadata.get("free_bytes")) is int and metadata["free_bytes"] >= MIN_FREE_BYTES
            and type(metadata.get("free_inodes")) is int and metadata["free_inodes"] >= MIN_FREE_INODES
        ) else "low_or_unverified",
    }


def survey(discover_fn=None):
    default_discovery = discover_fn is None
    found = discover(diagnostics=True) if default_discovery else discover_fn()
    summary = _summary(found)
    if isinstance(found.get("survey_diagnostics"), dict):
        summary["survey_diagnostics"] = found["survey_diagnostics"]
    elif default_discovery:
        summary["survey_diagnostics"] = _unverified_diagnostics()
    summary["pending_repair_diagnostic"] = _pending_diagnostic(found)
    return summary


def _change_root(found, target_uid=0, target_gid=0, run=subprocess.run):
    expected = found["metadata"]
    argv = ["podman", "unshare", "python3", "-c", _CHOWN_SCRIPT,
            found["mountpoint"], json.dumps(expected, sort_keys=True), str(target_uid), str(target_gid)]
    result = _call(
        argv,
        timeout=20,
        run=run,
    )
    value = _json(result)
    return (
        result is not None
        and result.returncode == 0
        and isinstance(value, dict)
        and value.get("status") == "changed"
    )


def _reduce_pending_root_mode(found, target_mode, run=subprocess.run):
    expected = found["metadata"]
    result = _call(
        ["podman", "unshare", "python3", "-c", _CHMOD_PENDING_SCRIPT,
         found["mountpoint"], json.dumps(expected, sort_keys=True), str(target_mode)],
        timeout=20,
        run=run,
    )
    value = _json(result)
    if (
        result is not None
        and result.returncode == 0
        and isinstance(value, dict)
        and value.get("status") in {"changed", "refused", "uncertain"}
    ):
        return value["status"]
    return "uncertain"


def _image_is_available(run=subprocess.run):
    result = _call(["podman", "image", "exists", IMAGE], run=run)
    return result is not None and result.returncode == 0


def _access_test(volume_name, run=subprocess.run):
    nonce = uuid.uuid4().hex
    container = f"o11y-journal-positions-check-{nonce}"
    path = f"{DATA_PATH}/.positions-gate-{nonce}"
    renamed = f"{path}.renamed"
    script = (
        "set -eu; umask 077; "
        f"a={path!r}; b={renamed!r}; "
        "moved=0; trap 'if [ \"$moved\" -eq 1 ]; then rm -f -- \"$b\"; else rm -f -- \"$a\"; fi' EXIT; "
        "(set -C; : > \"$a\"); printf x >> \"$a\"; "
        "test ! -e \"$b\"; mv -n -- \"$a\" \"$b\"; test ! -e \"$a\"; moved=1; "
        "test -f \"$b\"; rm -- \"$b\"; trap - EXIT"
    )
    def test_argv(script, test_container):
        return [
        "podman", "run", "--pull=never", "--name", test_container, "--rm", "--network", "none",
        "--read-only", "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", f"type=volume,source={volume_name},destination={DATA_PATH},rw",
        "--entrypoint", "/bin/bash", IMAGE, "-c", script,
        ]
    result = _call(test_argv(script, container), timeout=30, run=run)
    leftover = _json(_call(["podman", "ps", "--all", "--filter", f"name=^{container}$", "--format", "json"], run=run))
    clean = _rows(leftover) == []
    if not clean:
        _call(["podman", "rm", "--force", "--time", "1", container], timeout=10, run=run)
        list_argv = ["podman", "ps", "--all", "--filter", f"name=^{container}$", "--format", "json"]
        leftover = _json(_call(list_argv, run=run))
        clean = _rows(leftover) == []
    passed = result is not None and result.returncode == 0 and clean
    if not passed and clean:
        cleanup = f"rm -f -- {path!r} {renamed!r}"
        cleanup_name = f"{container}-cleanup"
        cleanup_result = _call(test_argv(cleanup, cleanup_name), timeout=30, run=run)
        cleanup_leftover = _json(_call(
            ["podman", "ps", "--all", "--filter", f"name=^{cleanup_name}$", "--format", "json"],
            run=run,
        ))
        clean = cleanup_result is not None and cleanup_result.returncode == 0 and _rows(cleanup_leftover) == []
    return passed and clean


def _space_is_sufficient(found, require_storage=False):
    metadata = found.get("metadata")
    storage_space = found.get("storage_space")
    checks = []
    if require_storage and storage_space is not None:
        checks.append(storage_space)
    elif require_storage:
        return False
    if isinstance(metadata, dict):
        checks.append(metadata)
    return bool(checks) and all(
        isinstance(space, dict)
        and type(space.get("free_bytes")) is int
        and space["free_bytes"] >= MIN_FREE_BYTES
        and type(space.get("free_inodes")) is int
        and space["free_inodes"] >= MIN_FREE_INODES
        for space in checks
    )


_PENDING_CHECK_NAMES = (
    "status_pending", "volume_inventory_complete", "name_collision_clear", "project_valid",
    "volume_object", "volume_name_matches", "mountpoint_absolute", "labels_object",
    "project_label_matches", "volume_label_matches", "needs_chown_false", "needs_copy_up_true",
    "volume_unused", "mount_count_zero", "collector_absent", "metadata_object",
    "metadata_observed", "children_empty", "child_count_zero", "acl_absent",
    "root_not_mount", "child_not_mount", "root_object", "owner_rwx",
    "group_other_write_or_special_absent", "owner_mismatch", "collector_access_blocked",
    "volume_free_bytes", "volume_free_inodes", "graphroot_free_bytes", "graphroot_free_inodes",
)


def _pending_checks(found):
    metadata = found.get("metadata")
    root = metadata.get("root") if isinstance(metadata, dict) else None
    volume = found.get("volume")
    labels = volume.get("Labels") if isinstance(volume, dict) else None
    project = found.get("project")
    mode = root.get("mode") if isinstance(root, dict) else None
    observed_metadata = isinstance(metadata, dict) and metadata.get("status") == "observed"
    root_mode_known = isinstance(root, dict) and isinstance(mode, int)
    storage_space = found.get("storage_space")
    return {
        "status_pending": found.get("status") == "volume_initialization_pending",
        "volume_inventory_complete": found.get("volume_inventory_complete") is True,
        "name_collision_clear": found.get("name_collision") is False,
        "project_valid": isinstance(project, str),
        "volume_object": isinstance(volume, dict),
        "volume_name_matches": isinstance(project, str) and isinstance(volume, dict)
        and volume.get("Name") == f"{project}_{VOLUME_KEY}",
        "mountpoint_absolute": isinstance(found.get("mountpoint"), str)
        and found["mountpoint"].startswith("/"),
        "labels_object": isinstance(labels, dict),
        "project_label_matches": isinstance(labels, dict)
        and labels.get("com.docker.compose.project") == project,
        "volume_label_matches": isinstance(labels, dict)
        and labels.get("com.docker.compose.volume", VOLUME_KEY) == VOLUME_KEY,
        "needs_chown_false": found.get("needs_chown") is False,
        "needs_copy_up_true": found.get("needs_copy_up") is True,
        "volume_unused": found.get("volume_use_count") == 0,
        "mount_count_zero": found.get("mount_count") == 0,
        "collector_absent": found.get("collector_present") is False,
        "metadata_object": isinstance(metadata, dict),
        "metadata_observed": observed_metadata,
        "children_empty": observed_metadata and metadata.get("children") == [],
        "child_count_zero": observed_metadata and metadata.get("child_count") == 0,
        "acl_absent": observed_metadata and metadata.get("acl") is False,
        "root_not_mount": observed_metadata and metadata.get("root_is_mount") is False,
        "child_not_mount": observed_metadata and metadata.get("child_mount") is False,
        "root_object": isinstance(root, dict),
        "owner_rwx": root_mode_known and mode & 0o700 == 0o700,
        "group_other_write_or_special_absent": root_mode_known and mode & (0o022 | 0o7000) == 0,
        "owner_mismatch": isinstance(root, dict) and (root.get("uid"), root.get("gid")) != (0, 0),
        "collector_access_blocked": observed_metadata and root_mode_known
        and isinstance(root, dict) and "uid" in root and "gid" in root and not _access(metadata),
        "volume_free_bytes": isinstance(metadata, dict)
        and type(metadata.get("free_bytes")) is int
        and metadata["free_bytes"] >= MIN_FREE_BYTES,
        "volume_free_inodes": isinstance(metadata, dict)
        and type(metadata.get("free_inodes")) is int
        and metadata["free_inodes"] >= MIN_FREE_INODES,
        "graphroot_free_bytes": isinstance(storage_space, dict)
        and type(storage_space.get("free_bytes")) is int
        and storage_space["free_bytes"] >= MIN_FREE_BYTES,
        "graphroot_free_inodes": isinstance(storage_space, dict)
        and type(storage_space.get("free_inodes")) is int
        and storage_space["free_inodes"] >= MIN_FREE_INODES,
    }


def _capacity_category(space, field, threshold):
    value = space.get(field) if isinstance(space, dict) else None
    if type(value) is not int:
        return "unverified"
    return "sufficient" if value >= threshold else "low"


def _mode_bit_category(mode, mask, verified):
    if not verified or type(mode) is not int or not 0 <= mode <= 0o7777:
        return "unverified"
    return "present" if mode & mask else "absent"


def _pending_diagnostic(found):
    checks = _pending_checks(found)
    metadata = found.get("metadata")
    root = metadata.get("root") if isinstance(metadata, dict) else None
    mode = root.get("mode") if isinstance(root, dict) else None
    mode_verified = isinstance(metadata, dict) and metadata.get("status") == "observed" and isinstance(mode, int)
    return {
        "owner_rwx": "unverified" if not mode_verified else (
            "complete" if checks["owner_rwx"] else "incomplete"
        ),
        "group_other_write_or_special_bits": "unverified" if not mode_verified else (
            "absent" if checks["group_other_write_or_special_absent"] else "present"
        ),
        "group_write": _mode_bit_category(mode, 0o020, mode_verified),
        "other_write": _mode_bit_category(mode, 0o002, mode_verified),
        "setuid": _mode_bit_category(mode, 0o4000, mode_verified),
        "setgid": _mode_bit_category(mode, 0o2000, mode_verified),
        "sticky": _mode_bit_category(mode, 0o1000, mode_verified),
        "volume_free_bytes": _capacity_category(
            metadata if isinstance(metadata, dict) and metadata.get("status") == "observed" else None,
            "free_bytes", MIN_FREE_BYTES,
        ),
        "volume_free_inodes": _capacity_category(
            metadata if isinstance(metadata, dict) and metadata.get("status") == "observed" else None,
            "free_inodes", MIN_FREE_INODES,
        ),
        "graphroot_free_bytes": _capacity_category(
            found.get("storage_space"), "free_bytes", MIN_FREE_BYTES
        ),
        "graphroot_free_inodes": _capacity_category(
            found.get("storage_space"), "free_inodes", MIN_FREE_INODES
        ),
        "failed_checks": [name for name in _PENDING_CHECK_NAMES if not checks[name]],
    }


def _cursorless_prestart_safe(found):
    metadata = found.get("metadata")
    volume = found.get("volume")
    labels = volume.get("Labels") if isinstance(volume, dict) else None
    project = found.get("project")
    expected_name = f"{project}_{VOLUME_KEY}" if isinstance(project, str) else None
    return (
        found.get("status") == "observed"
        and isinstance(volume, dict)
        and isinstance(labels, dict)
        and project
        and found.get("volume_name") == expected_name
        and volume.get("Name") == expected_name
        and labels.get("com.docker.compose.project") == project
        and labels.get("com.docker.compose.volume") in (None, VOLUME_KEY)
        and volume.get("Driver") == "local"
        and volume.get("Scope") == "local"
        and volume.get("Options") in ({}, None)
        and volume.get("Mountpoint") == found.get("mountpoint")
        and type(volume.get("MountCount")) is int
        and volume["MountCount"] == 0
        and volume.get("Anonymous", False) is False
        and found.get("needs_chown") is False
        and found.get("needs_copy_up") is False
        and found.get("collector_present") is False
        and type(found.get("volume_use_count")) is int
        and found["volume_use_count"] == 0
        and type(found.get("mount_count")) is int
        and found["mount_count"] == 0
        and _live_safe_metadata(found, require_cursor=False)
        and metadata.get("journal_cursor_presence") == "absent"
        and metadata.get("journal_cursor_valid") is False
        and isinstance(metadata.get("root", {}).get("mode"), int)
        and metadata["root"]["mode"] & 0o700 == 0o700
        and metadata["root"]["mode"] & (0o022 | 0o7000) == 0
        and _access(metadata)
        and _space_is_sufficient(found, require_storage=True)
    )


def _stable_metadata(metadata):
    return {key: value for key, value in metadata.items() if key not in {"free_bytes", "free_inodes"}}


def _pending_empty(found, owner_mismatch=False):
    checks = _pending_checks(found)
    required = _PENDING_CHECK_NAMES if owner_mismatch else tuple(
        name for name in _PENDING_CHECK_NAMES
        if name not in {"owner_mismatch", "collector_access_blocked"}
    )
    return all(checks[name] for name in required)


def _repair_pending_positions(first, discover_fn, change_fn, mode_change_fn):
    first_metadata = first.get("metadata") if isinstance(first, dict) else None
    if not isinstance(first_metadata, dict):
        return {"status": "refused", "reason": "evidence_changed"}
    first_root = first_metadata.get("root")
    first_root = first_root if isinstance(first_root, dict) else {}
    if (
        (first_root.get("uid"), first_root.get("gid")) == (0, 0)
        and _pending_empty(first)
        and _space_is_sufficient(first, require_storage=True)
    ):
        return {"status": "already_correct", "reason": "owner_matches"}

    checks = _pending_checks(first)
    mode = first_root.get("mode")
    mode_exception = (
        type(mode) is int
        and 0 <= mode <= 0o7777
        and not checks["group_other_write_or_special_absent"]
        and mode & 0o700 == 0o700
        and mode & 0o7000 == 0
        and mode & 0o022 != 0
        and all(value for name, value in checks.items()
                if name != "group_other_write_or_special_absent")
        and _space_is_sufficient(first, require_storage=True)
    )
    if (
        (not _pending_empty(first, owner_mismatch=True) and not mode_exception)
        or not _space_is_sufficient(first, require_storage=True)
    ):
        return {
            "status": "refused",
            "reason": "pending_volume_unsupported",
            "pending_repair_diagnostic": _pending_diagnostic(first),
        }
    original = first["metadata"]["root"]
    original_volume = _volume_identity_tuple(first.get("volume"))
    if original_volume is None or not _valid_created_at(first["volume"].get("CreatedAt")):
        return {"status": "refused", "reason": "volume_identity_unverified"}

    def fresh_matches(current, owner, expected_mode=original["mode"], allow_mode_exception=False):
        metadata = current.get("metadata") if isinstance(current, dict) else None
        if not isinstance(metadata, dict) or metadata.get("status") != "observed":
            return False
        root = metadata.get("root")
        if not isinstance(root, dict):
            return False
        current_checks = _pending_checks(current)
        pending_safe = _pending_empty(current, owner_mismatch=owner != (0, 0))
        if allow_mode_exception:
            pending_safe = (
                not current_checks["group_other_write_or_special_absent"]
                and all(value for name, value in current_checks.items()
                        if name != "group_other_write_or_special_absent")
            )
        return (
            pending_safe
            and _space_is_sufficient(current, require_storage=True)
            and _volume_identity_tuple(current.get("volume")) == original_volume
            and current.get("volume_name") == first.get("volume_name")
            and current.get("mountpoint") == first.get("mountpoint")
            and current.get("project") == first.get("project")
            and root.get("mode") == expected_mode
            and root.get("dev") == original["dev"]
            and root.get("ino") == original["ino"]
            and (root.get("uid"), root.get("gid")) == owner
        )

    fresh = discover_fn()
    owner = (original["uid"], original["gid"])
    if (
        not fresh_matches(fresh, owner, allow_mode_exception=mode_exception)
            or _stable_metadata(fresh["metadata"]) != _stable_metadata(first_metadata)
    ):
        return {"status": "refused", "reason": "evidence_changed"}

    def recover():
        return {"status": "uncertain", "reason": "first_mount_history_unproven"}

    try:
        owner_change_input = fresh
        expected_mode = original["mode"]
        if mode_exception:
            target_mode = original["mode"] & ~0o022
            mode_result = mode_change_fn(fresh, target_mode)
            if mode_result == "refused":
                return {"status": "refused", "reason": "evidence_changed"}
            if mode_result != "changed":
                return recover()
            mode_verified = discover_fn()
            expected_metadata = {
                **fresh["metadata"],
                "root": {**fresh["metadata"]["root"], "mode": target_mode},
                "component_layout": target_mode & (0o022 | 0o7000) == 0,
            }
            if (
                not fresh_matches(mode_verified, owner, expected_mode=target_mode)
                or _stable_metadata(mode_verified["metadata"]) != _stable_metadata(expected_metadata)
                or mode_verified["metadata"]["children"] != []
            ):
                return recover()
            owner_change_input = mode_verified
            expected_mode = target_mode

        if not change_fn(owner_change_input):
            return recover()
        verified = discover_fn()
        if (
            not fresh_matches(verified, (0, 0), expected_mode=expected_mode)
            or verified["metadata"]["root"]["mode"] != expected_mode
            or verified["metadata"]["children"] != []
        ):
            return recover()
        mount_gate = discover_fn()
        if (
            not fresh_matches(mount_gate, (0, 0), expected_mode=expected_mode)
            or _stable_metadata(mount_gate["metadata"]) != _stable_metadata(verified["metadata"])
        ):
            return recover()
    except Exception:
        return recover()
    return {"status": "repaired", "reason": "pending_initialization_owner_verified"}


def repair_positions(
    discover_fn=discover,
    change_fn=_change_root,
    access_test_fn=_access_test,
    restore_fn=lambda found, uid, gid: _change_root(found, uid, gid),
    image_available_fn=_image_is_available,
    mode_change_fn=_reduce_pending_root_mode,
):
    first = discover_fn()
    if first.get("status") == "volume_initialization_pending":
        return _repair_pending_positions(first, discover_fn, change_fn, mode_change_fn)
    if first.get("status") != "observed":
        return {"status": "refused", "reason": first.get("status", "unavailable")}
    if not _space_is_sufficient(first):
        return {"status": "refused", "reason": "insufficient_space"}
    if first["volume_use_count"] != 0 or first["collector_present"] or first.get("mount_count") != 0:
        return {"status": "refused", "reason": "volume_in_use"}
    if not _safe_metadata(first):
        return {"status": "refused", "reason": "metadata_ambiguous"}
    root = first["metadata"]["root"]
    if root["uid"] == 0 and root["gid"] == 0:
        return {"status": "already_correct", "reason": "owner_matches"}
    if root["uid"] == 0 or root["mode"] & 0o700 != 0o700 or _access(first["metadata"]):
        return {"status": "refused", "reason": "unsupported_ownership_state"}
    if any(item["mode"] & 0o400 == 0 for item in first["metadata"]["children"]):
        return {"status": "refused", "reason": "child_access_ambiguous"}
    if not image_available_fn():
        return {"status": "refused", "reason": "probe_unavailable"}

    fresh = discover_fn()
    if (
        fresh.get("status") != "observed"
        or fresh["volume_name"] != first["volume_name"]
        or fresh["project"] != first["project"]
        or fresh["volume_use_count"] != 0
        or fresh.get("mount_count") != 0
        or fresh["collector_present"]
        or _stable_metadata(fresh["metadata"]) != _stable_metadata(first["metadata"])
        or not _space_is_sufficient(fresh)
        or not _safe_metadata(fresh)
    ):
        return {"status": "refused", "reason": "evidence_changed"}
    def recover(reason):
        try:
            current = discover_fn()
            if (
                current.get("status") != "observed"
                or current.get("volume_name") != first["volume_name"]
                or current.get("project") != first["project"]
                or current.get("mountpoint") != first["mountpoint"]
                or current.get("volume_use_count") != 0
                or current.get("mount_count") != 0
                or current.get("collector_present") is not False
                or not _safe_metadata(current)
            ):
                return {"status": "uncertain", "reason": "rollback_identity_unverified"}
            current_root = current["metadata"]["root"]
            original_root = root
            if (current_root["uid"], current_root["gid"]) not in {
                (original_root["uid"], original_root["gid"]), (0, 0)
            }:
                return {"status": "uncertain", "reason": "rollback_owner_unexpected"}
            if (
                current_root["dev"] != original_root["dev"]
                or current_root["ino"] != original_root["ino"]
                or current_root["mode"] != original_root["mode"]
                or current["metadata"]["children"] != first["metadata"]["children"]
            ):
                return {"status": "uncertain", "reason": "rollback_metadata_unverified"}
            restored = _stable_metadata(current["metadata"]) == _stable_metadata(first["metadata"])
            if not restored:
                restore_fn(current, original_root["uid"], original_root["gid"])
                restored_state = discover_fn()
                restored = (
                    restored_state.get("status") == "observed"
                    and restored_state.get("volume_name") == first["volume_name"]
                    and restored_state.get("project") == first["project"]
                    and restored_state.get("mountpoint") == first["mountpoint"]
                    and restored_state.get("volume_use_count") == 0
                    and restored_state.get("mount_count") == 0
                    and restored_state.get("collector_present") is False
                    and _stable_metadata(restored_state["metadata"]) == _stable_metadata(first["metadata"])
                )
            if restored:
                return {"status": "refused", "reason": reason}
        except Exception:
            pass
        return {"status": "uncertain", "reason": "rollback_unverified"}

    try:
        if not change_fn(fresh):
            return recover("root_change_not_verified")
        verified = discover_fn()
        if (
            verified.get("status") != "observed"
            or verified.get("volume_name") != first["volume_name"]
            or verified.get("project") != first["project"]
            or verified.get("mountpoint") != first["mountpoint"]
            or verified.get("volume_use_count") != 0
            or verified.get("mount_count") != 0
            or verified.get("collector_present") is not False
            or verified["metadata"]["root"]["uid"] != 0
            or verified["metadata"]["root"]["gid"] != 0
            or verified["metadata"]["root"]["mode"] != root["mode"]
            or verified["metadata"]["root"]["dev"] != root["dev"]
            or verified["metadata"]["root"]["ino"] != root["ino"]
            or verified["metadata"]["children"] != first["metadata"]["children"]
            or not _space_is_sufficient(verified)
        ):
            return recover("readback_failed")
        mount_gate = discover_fn()
        if (
            mount_gate.get("status") != "observed"
            or mount_gate.get("volume_name") != first["volume_name"]
            or mount_gate.get("project") != first["project"]
            or mount_gate.get("mountpoint") != first["mountpoint"]
            or mount_gate.get("volume_use_count") != 0
            or mount_gate.get("mount_count") != 0
            or mount_gate.get("collector_present") is not False
            or _stable_metadata(mount_gate["metadata"]) != _stable_metadata(verified["metadata"])
            or not _space_is_sufficient(mount_gate)
        ):
            return recover("pre_mount_recheck_failed")
        if not access_test_fn(first["volume_name"]):
            return recover("access_test_failed")
    except Exception:
        return recover("post_mutation_check_failed")
    return {"status": "repaired", "reason": "verified"}


def verify(discover_fn=None):
    found = discover(read_cursor=True) if discover_fn is None else discover_fn()
    summary = _summary(found)
    if found.get("status") in {"volume_missing", "volume_initialization_pending"}:
        volume = found.get("volume")
        metadata = found.get("metadata")
        root = metadata.get("root") if isinstance(metadata, dict) else None
        bootstrap_safe = (
            found.get("volume_inventory_complete") is True
            and found.get("collector_present") is False
            and found.get("name_collision") is False
        )
        if found.get("status") == "volume_initialization_pending":
            mode = root.get("mode") if isinstance(root, dict) else None
            bootstrap_safe = bootstrap_safe and (
                isinstance(volume, dict)
                and found.get("needs_chown", volume.get("NeedsChown")) is False
                and found.get("needs_copy_up", volume.get("NeedsCopyUp")) is True
                and found.get("volume_use_count") == 0
                and found.get("mount_count") == 0
                and isinstance(metadata, dict)
                and metadata.get("status") == "observed"
                and metadata.get("child_count") == 0
                and metadata.get("children") == []
                and metadata.get("acl") is False
                and metadata.get("root_is_mount") is False
                and metadata.get("child_mount") is False
                and isinstance(root, dict)
                and root.get("uid") == 0
                and root.get("gid") == 0
                and isinstance(mode, int)
                and mode & 0o700 == 0o700
                and mode & (0o022 | 0o7000) == 0
                and _space_is_sufficient(found, require_storage=True)
            )
        elif found.get("status") == "volume_missing":
            bootstrap_safe = bootstrap_safe and _space_is_sufficient(found, require_storage=True)
        if bootstrap_safe:
            return {
                **summary,
                "status": "bootstrap_allowed",
                "reason": "collector_absent_and_volume_safe_to_initialize",
            }
        if (
            found.get("status") == "volume_initialization_pending"
            and isinstance(volume, dict)
            and found.get("needs_chown", volume.get("NeedsChown")) is False
            and found.get("needs_copy_up", volume.get("NeedsCopyUp")) is True
            and found.get("collector_present") is True
            and found.get("volume_use_count") == 1
            and found.get("mount_count") == 1
        ):
            found = {**found, "status": "observed"}
            summary = _summary(found)
    if found.get("status") != "observed":
        return {"status": "refused", "reason": found.get("status", "unavailable")}
    if not _live_safe_metadata(found):
        if _cursorless_prestart_safe(found):
            return {
                **summary,
                "children": "alloy_component_layout",
                "status": "ready",
                "reason": "positions_cursor_absent_prestart_retry",
            }
        return {"status": "refused", "reason": "metadata_ambiguous"}
    if not _access(found["metadata"]):
        return {"status": "refused", "reason": "positions_access_blocked"}
    return {
        **summary,
        "children": "alloy_component_layout",
        "status": "ready",
        "reason": "positions_identity_verified",
    }


def verify_live(discover_fn=None):
    found = discover(read_cursor=True) if discover_fn is None else discover_fn()
    if (
        found.get("status") == "volume_initialization_pending"
        and found.get("needs_chown", found.get("volume", {}).get("NeedsChown")) is False
        and found.get("needs_copy_up", found.get("volume", {}).get("NeedsCopyUp")) is True
        and found.get("collector_present") is True
        and found.get("volume_use_count") == 1
        and found.get("mount_count") == 1
    ):
        found = {**found, "status": "observed"}
    if found.get("status") != "observed":
        return {"status": "refused", "reason": found.get("status", "unavailable")}
    if (
        found.get("collector_present") is not True
        or type(found.get("volume_use_count")) is not int
        or found["volume_use_count"] != 1
        or type(found.get("mount_count")) is not int
        or found["mount_count"] != 1
        or found.get("collector_mount_verified") is not True
    ):
        return {"status": "refused", "reason": "collector_mount_unverified"}
    if not _live_safe_metadata(found):
        return {"status": "refused", "reason": "metadata_ambiguous"}
    if not _access(found["metadata"]):
        return {"status": "refused", "reason": "positions_access_blocked"}
    return {
        **_summary(found),
        "children": "alloy_component_layout",
        "status": "ready",
        "reason": "positions_identity_verified",
    }


def main():
    actions = {
        "survey": survey,
        "repair-positions": repair_positions,
        "verify": verify,
        "verify-live": verify_live,
    }
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        result = actions[action]() if action in actions else {"status": "refused", "reason": "invalid_action"}
    except Exception:
        result = {
            **_summary({"status": "unavailable"}),
            "reason": "survey_failed",
            "survey_diagnostics": _unverified_diagnostics(),
            "pending_repair_diagnostic": _pending_diagnostic({}),
        } if action == "survey" else {
            "status": "unavailable",
            "reason": "survey_failed",
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

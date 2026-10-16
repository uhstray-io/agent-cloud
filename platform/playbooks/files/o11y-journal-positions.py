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
VOLUME_KEY = "journal-collector-state-v3"
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
def role_observation(root_path, root_stat, root_mount, name, observed_mount, expected_stat):
    result={"owner":"unverified","access":"unverified","acl":"unverified",
            "mount":"unverified","children":"unverified"}
    root_fd=None
    role_fd=None
    try:
        root_fd=os.open(root_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        pinned_root=os.fstat(root_fd)
        identity=lambda value:(value.st_dev,value.st_ino,value.st_uid,value.st_gid,
            stat.S_IMODE(value.st_mode),value.st_nlink,value.st_mtime_ns)
        if identity(pinned_root)!=identity(root_stat): return result
        before=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or identity(before)!=identity(expected_stat): return result
        role_fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=root_fd)
        opened=os.fstat(role_fd)
        if identity(before)!=identity(opened): return result
        result["owner"]="matches_collector" if (opened.st_uid,opened.st_gid)==(0,0) else "mismatch"
        if opened.st_uid==0: allowed=(opened.st_mode>>6)&7
        elif opened.st_gid==0: allowed=(opened.st_mode>>3)&7
        else: allowed=opened.st_mode&7
        result["access"]="read_write_execute" if allowed&7==7 else "blocked"
        acl_present=any("acl" in value.lower() for value in os.listxattr(role_fd))
        result["acl"]="present" if acl_present else "absent"
        role_path=os.path.join(root_path,name)
        def bounded_names(directory_fd):
            scan_fd=os.open(".",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,
                            dir_fd=directory_fd)
            try:
                if identity(os.fstat(scan_fd))!=identity(os.fstat(directory_fd)):
                    return None
                names=[]
                with os.scandir(scan_fd) as entries:
                    for entry in entries:
                        names.append(entry.name)
                        if len(names)>32: return None
                return names
            finally:
                os.close(scan_fd)
        names=bounded_names(role_fd)
        if names is None:
            return {"owner":"unverified","access":"unverified","acl":"unverified",
                    "mount":"unverified","children":"unverified"}
        names_after=bounded_names(role_fd)
        after=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
        pinned_after=os.fstat(role_fd)
        path_root=os.lstat(root_path)
        pinned_root_after=os.fstat(root_fd)
        acl_after=any("acl" in value.lower() for value in os.listxattr(role_fd))
        mount_after=mount_id(role_path)
        if (names_after is None or identity(before)!=identity(after) or identity(opened)!=identity(pinned_after)
                or identity(path_root)!=identity(root_stat)
                or identity(pinned_root_after)!=identity(root_stat)
                or acl_after!=acl_present or mount_after!=observed_mount or names_after!=names):
            return {"owner":"unverified","access":"unverified","acl":"unverified",
                    "mount":"unverified","children":"unverified"}
        result["mount"]="ambiguous" if observed_mount[0]!=root_mount[0] else "clear"
        result["children"]="nonempty" if names else "empty"
        return result
    except (OSError,ValueError,IndexError):
        return {"owner":"unverified","access":"unverified","acl":"unverified",
                "mount":"unverified","children":"unverified"}
    finally:
        if role_fd is not None: os.close(role_fd)
        if root_fd is not None: os.close(root_fd)
def unverified_journal_metadata():
    component={"presence":"unverified","type":"unverified","owner":"unverified",
        "owner_rwx":"unverified","unsafe_mode_bits":"unverified","link_count":"unverified",
        "acl":"unverified","mount":"unverified","children":"unverified"}
    positions={"presence":"unverified","type":"unverified","owner":"unverified",
        "owner_rw":"unverified","unsafe_mode_bits":"unverified","link_count":"unverified",
        "acl":"unverified","mount":"unverified"}
    return {"component":component,"positions":positions,
        "child_count":"unverified","temporary_file_metadata":"unverified",
        "temporary_files":"unverified",
        "unknown_children":{"files":"unverified","directories":"unverified",
            "symlinks":"unverified","other":"unverified"}}
def unverified_seed_metadata():
    return {"presence":"unverified","type":"unverified","owner":"unverified",
        "link_count":"unverified","owner_execute":"unverified",
        "group_other_permissions":"unverified","special_bits":"unverified",
        "acl":"unverified","mount":"unverified"}
def seed_observation(root_path, root_stat, root_mount):
    result=unverified_seed_metadata()
    root_fd=None
    seed_fd=None
    name="alloy_seed.json"
    try:
        identity=lambda value:(value.st_dev,value.st_ino,value.st_uid,value.st_gid,
            stat.S_IMODE(value.st_mode),value.st_nlink,value.st_mtime_ns)
        root_fd=os.open(root_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        if identity(os.fstat(root_fd))!=identity(root_stat): return result
        try: before=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
        except FileNotFoundError:
            result.update(presence="absent",type="absent")
            return result if identity(os.fstat(root_fd))==identity(root_stat) else unverified_seed_metadata()
        result["presence"]="present"
        result["type"]=("regular_file" if stat.S_ISREG(before.st_mode) else
            "directory" if stat.S_ISDIR(before.st_mode) else
            "symlink" if stat.S_ISLNK(before.st_mode) else "other")
        if not stat.S_ISREG(before.st_mode):
            after=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
            stable = identity(after) == identity(before) and identity(os.fstat(root_fd)) == identity(root_stat)
            return result if stable else unverified_seed_metadata()
        seed_fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=root_fd)
        opened=os.fstat(seed_fd)
        if identity(opened)!=identity(before): return unverified_seed_metadata()
        result.update(
            owner="matches_collector" if (opened.st_uid,opened.st_gid)==(0,0) else "mismatch",
            link_count="match" if opened.st_nlink==1 else "mismatch",
            owner_execute="present" if opened.st_mode & 0o100 else "absent",
            group_other_permissions="present" if opened.st_mode & 0o077 else "absent",
            special_bits="present" if opened.st_mode & 0o7000 else "absent",
        )
        seed_path=os.path.join(root_path,name)
        acl_found=any("acl" in value.lower() for value in os.listxattr(seed_fd))
        def checked_mount():
            root_before=os.lstat(root_path)
            seed_before=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
            path_before=os.stat(seed_path,follow_symlinks=False)
            if (identity(root_before)!=identity(root_stat) or identity(seed_before)!=identity(opened)
                    or identity(path_before)!=identity(opened)):
                return None
            observed=mount_id(seed_path)
            root_after=os.lstat(root_path)
            seed_after=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
            path_after=os.stat(seed_path,follow_symlinks=False)
            if (identity(root_after)!=identity(root_stat) or identity(seed_after)!=identity(opened)
                    or identity(path_after)!=identity(opened)):
                return None
            return observed
        observed_mount=checked_mount()
        if observed_mount is None: return unverified_seed_metadata()
        mount_after=checked_mount()
        acl_after=any("acl" in value.lower() for value in os.listxattr(seed_fd))
        if (observed_mount is None or mount_after is None or identity(opened)!=identity(before)
                or identity(os.fstat(root_fd))!=identity(root_stat) or acl_after!=acl_found
                or mount_after!=observed_mount):
            return unverified_seed_metadata()
        result["acl"]="present" if acl_found else "absent"
        result["mount"]="ambiguous" if observed_mount[0]!=root_mount[0] else "clear"
        return result
    except (OSError,ValueError,IndexError,AttributeError):
        return unverified_seed_metadata()
    finally:
        if seed_fd is not None: os.close(seed_fd)
        if root_fd is not None: os.close(root_fd)
def bounded_names(directory_fd):
    names=[]
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names)>32: return None
    return names
def journal_observation(root_path, root_stat, root_mount):
    result=unverified_journal_metadata()
    root_fd=None
    component_fd=None
    positions_fd=None
    try:
        identity=lambda value:(value.st_dev,value.st_ino,value.st_uid,value.st_gid,
            stat.S_IMODE(value.st_mode),value.st_nlink,value.st_mtime_ns)
        root_fd=os.open(root_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        if identity(os.fstat(root_fd))!=identity(root_stat): return result
        name="loki.source.journal.o11y_alloy"
        try: before=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
        except FileNotFoundError:
            result["component"].update(presence="absent",type="absent")
            result["positions"].update(presence="absent",type="absent")
            result["child_count"]=0
            result["temporary_file_metadata"]="not_applicable"
            result["temporary_files"]=0
            result["unknown_children"]={"files":0,"directories":0,"symlinks":0,"other":0}
            return result if identity(os.fstat(root_fd))==identity(root_stat) else unverified_journal_metadata()
        result["component"]["presence"]="present"
        if not stat.S_ISDIR(before.st_mode):
            result["component"]["type"]=("symlink" if stat.S_ISLNK(before.st_mode) else
                "file" if stat.S_ISREG(before.st_mode) else "other")
            after=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
            if identity(after)!=identity(before) or identity(os.fstat(root_fd))!=identity(root_stat):
                return unverified_journal_metadata()
            return result
        result["component"]["type"]="directory"
        component_path=os.path.join(root_path,name)
        component_mount=mount_id(component_path)
        component_fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=root_fd)
        opened=os.fstat(component_fd)
        if identity(before)!=identity(opened): return unverified_journal_metadata()
        result["component"]["owner"]="matches_collector" if (opened.st_uid,opened.st_gid)==(0,0) else "mismatch"
        result["component"]["owner_rwx"]="complete" if opened.st_mode & 0o700 == 0o700 else "incomplete"
        result["component"]["unsafe_mode_bits"]="present" if opened.st_mode & (0o022|0o7000) else "absent"
        result["component"]["link_count"]="match" if opened.st_nlink==2 else "mismatch"
        component_acl=any("acl" in value.lower() for value in os.listxattr(component_fd))
        result["component"]["acl"]="present" if component_acl else "absent"
        result["component"]["mount"]="ambiguous" if component_mount[0]!=root_mount[0] else "clear"
        names=bounded_names(component_fd)
        if names is None: return unverified_journal_metadata()
        result["child_count"]=len(names)
        result["component"]["children"]="nonempty" if names else "empty"
        temporary=0
        temporary_safety=[]
        unknown={"files":0,"directories":0,"symlinks":0,"other":0}
        positions_stat=None
        positions_mount=None
        positions_acl=None
        child_snapshot=[]
        for child_name in names:
            try: child_stat=os.stat(child_name,dir_fd=component_fd,follow_symlinks=False)
            except OSError: return unverified_journal_metadata()
            child_path=os.path.join(component_path,child_name)
            child_mount=mount_id(child_path)
            nested_acl=acl(child_path)
            child_snapshot.append((child_name,identity(child_stat),child_mount,nested_acl))
            if child_name=="positions.yml":
                positions_stat=child_stat
                positions_mount=child_mount
                if stat.S_ISREG(child_stat.st_mode):
                    positions_fd=os.open(
                        child_name,
                        os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC,
                        dir_fd=component_fd,
                    )
                    if identity(os.fstat(positions_fd))!=identity(child_stat): return unverified_journal_metadata()
                    positions_acl=any("acl" in value.lower() for value in os.listxattr(positions_fd))
                else:
                    positions_acl=None
            elif (re.fullmatch(r"\.positions\.yml[0-9]{1,19}",child_name)
                    and stat.S_ISREG(child_stat.st_mode)):
                temporary+=1
                temporary_safety.append(
                    child_stat.st_uid==0 and child_stat.st_gid==0 and child_stat.st_nlink==1
                    and not nested_acl and child_stat.st_mode & 0o600 == 0o600
                    and child_stat.st_mode & 0o077 == 0 and child_stat.st_mode & 0o7133 == 0
                    and child_mount[0]==root_mount[0]
                )
            else:
                kind=("files" if stat.S_ISREG(child_stat.st_mode) else
                    "directories" if stat.S_ISDIR(child_stat.st_mode) else
                    "symlinks" if stat.S_ISLNK(child_stat.st_mode) else "other")
                unknown[kind]+=1
        if positions_stat is None:
            result["positions"].update(presence="absent",type="absent")
        elif not stat.S_ISREG(positions_stat.st_mode):
            positions_type="symlink" if stat.S_ISLNK(positions_stat.st_mode) else "other"
            result["positions"].update(presence="present",type=positions_type)
        else:
            result["positions"].update(
                presence="present",type="regular_file",
                owner="matches_collector" if (positions_stat.st_uid,positions_stat.st_gid)==(0,0) else "mismatch",
                owner_rw="complete" if positions_stat.st_mode & 0o600 == 0o600 else "incomplete",
                unsafe_mode_bits="present" if positions_stat.st_mode & (0o077|0o7133) else "absent",
                link_count="match" if positions_stat.st_nlink==1 else "mismatch",
                acl="present" if positions_acl else "absent",
                mount="ambiguous" if positions_mount[0]!=root_mount[0] else "clear")
        result["temporary_files"]=temporary
        result["temporary_file_metadata"]=(
            "not_applicable" if not temporary else "safe" if all(temporary_safety) else "unsafe"
        )
        result["unknown_children"]=unknown
        names_after=bounded_names(component_fd)
        component_after=os.fstat(component_fd)
        root_after=os.fstat(root_fd)
        path_after=os.lstat(component_path)
        acl_after=any("acl" in value.lower() for value in os.listxattr(component_fd))
        mount_after=mount_id(component_path)
        if (names_after is None or sorted(names_after)!=sorted(names) or identity(component_after)!=identity(opened)
                or identity(path_after)!=identity(before) or identity(root_after)!=identity(root_stat)
                or acl_after!=component_acl or mount_after!=component_mount):
            return unverified_journal_metadata()
        if positions_stat is not None:
            after=os.stat("positions.yml",dir_fd=component_fd,follow_symlinks=False)
            if identity(after)!=identity(positions_stat): return unverified_journal_metadata()
            if stat.S_ISREG(after.st_mode):
                if positions_fd is None or identity(os.fstat(positions_fd))!=identity(positions_stat):
                    return unverified_journal_metadata()
                if any("acl" in value.lower() for value in os.listxattr(positions_fd))!=positions_acl:
                    return unverified_journal_metadata()
        for child_name, expected_identity, expected_mount, expected_acl in child_snapshot:
            child_after=os.stat(child_name,dir_fd=component_fd,follow_symlinks=False)
            child_mount_after=mount_id(os.path.join(component_path,child_name))
            child_acl_after=acl(os.path.join(component_path,child_name))
            if (identity(child_after)!=expected_identity or child_mount_after!=expected_mount
                    or child_acl_after!=expected_acl):
                return unverified_journal_metadata()
        return result
    except (OSError,ValueError,IndexError,AttributeError):
        return unverified_journal_metadata()
    finally:
        if positions_fd is not None: os.close(positions_fd)
        if component_fd is not None: os.close(component_fd)
        if root_fd is not None: os.close(root_fd)
def empty_unmatched():
    return {
        "count":0,
        "owner":{"matches_collector":0,"mismatch":0,"unverified":0},
        "access":{"read_write_execute":0,"blocked":0,"unverified":0},
        "acl":{"absent":0,"present":0,"unverified":0},
        "mount":{"clear":0,"ambiguous":0,"unverified":0},
        "children":{"empty":0,"nonempty":0,"unverified":0},
    }
def record_unmatched(summary, observation):
    summary["count"]+=1
    if observation["acl"]!="absent" or observation["mount"]!="clear":
        observation={key:"unverified" for key in ("owner","access","acl","mount","children")}
    categories={
        "owner":{"matches_collector":"matches_collector","mismatch":"mismatch"},
        "access":{"read_write_execute":"read_write_execute","blocked":"blocked"},
        "acl":{"absent":"absent","present":"present"},
        "mount":{"clear":"clear","ambiguous":"ambiguous"},
        "children":{"empty":"empty","nonempty":"nonempty"},
    }
    for field, values in categories.items():
        category=observation[field]
        summary[field][category if category in values else "unverified"]+=1
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
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        pinned=os.fstat(root_fd)
        if (pinned.st_dev,pinned.st_ino)!=(st.st_dev,st.st_ino): raise ValueError("root changed")
        names=bounded_names(root_fd)
        path_after=os.lstat(root)
        pinned_after=os.fstat(root_fd)
        if ((path_after.st_dev,path_after.st_ino)!=(st.st_dev,st.st_ino)
                or (pinned_after.st_dev,pinned_after.st_ino)!=(st.st_dev,st.st_ino)):
            raise ValueError("root changed")
    finally:
        os.close(root_fd)
    if names is None: raise OverflowError("children unbounded")
    children=[]
    top_level={"seed_file":0,"journal_component":0,"remotecfg_component":0,"other_files":0,
               "other_directories":0,"other_symlinks":0,"other_kinds":0,
               "receiver_component":0,"exporter_component":0,
               "attributes_processor_component":0}
    role_metadata={"receiver":{"owner":"unverified","access":"unverified","acl":"unverified",
                                 "mount":"unverified","children":"unverified"},
                   "exporter":{"owner":"unverified","access":"unverified","acl":"unverified",
                                 "mount":"unverified","children":"unverified"},
                   "attributes_processor":{"owner":"unverified","access":"unverified","acl":"unverified",
                                           "mount":"unverified","children":"unverified"},
                   "remotecfg":{"owner":"unverified","access":"unverified","acl":"unverified",
                                "mount":"unverified","children":"unverified"}}
    journal_metadata=unverified_journal_metadata()
    seed_metadata=unverified_seed_metadata()
    remotecfg_state="absent"
    unmatched_directories=empty_unmatched()
    journal_metadata=journal_observation(root,st,root_mount)
    seed_metadata=seed_observation(root,st,root_mount)
    acl_found=acl(root)
    child_mount=False
    component_layout=st.st_mode & (0o022|0o7000) == 0
    cursor_valid=False
    cursor_presence="absent"
    cursor_checked=False
    free=os.statvfs(root)
    free_bytes=free.f_bavail*free.f_frsize
    free_inodes=free.f_favail
    for name in sorted(names):
        path=os.path.join(root,name)
        item=os.lstat(path)
        item_mount=mount_id(path)
        child_mount=child_mount or item_mount[0]!=root_mount[0]
        acl_found=acl_found or acl(path)
        if name=="remotecfg":
            top_level["remotecfg_component"]+=1
            if stat.S_ISDIR(item.st_mode):
                observation=role_observation(root,st,root_mount,name,item_mount,item)
                role_metadata["remotecfg"]=observation
                values=tuple(observation.values())
                mode_safe=item.st_mode & (0o022|0o7000) == 0
                if "unverified" in values:
                    remotecfg_state="unverified"
                elif (observation["owner"]=="matches_collector"
                      and observation["access"]=="read_write_execute"
                      and observation["acl"]=="absent" and observation["mount"]=="clear"
                      and observation["children"]=="empty" and mode_safe):
                    remotecfg_state="safe"
                else:
                    remotecfg_state="unsafe"
            else:
                remotecfg_state="unsafe"
            component_layout=component_layout and remotecfg_state=="safe"
        elif stat.S_ISREG(item.st_mode):
            if name=="alloy_seed.json": top_level["seed_file"]+=1
            else: top_level["other_files"]+=1
            component_layout=(component_layout and name=="alloy_seed.json"
                and item.st_uid==0 and item.st_gid==0 and item.st_nlink==1
                and item.st_mode & 0o077 == 0 and item.st_mode & 0o7133 == 0)
        elif stat.S_ISDIR(item.st_mode) and name=="loki.source.journal.o11y_alloy":
            top_level["journal_component"]+=1
            component_layout=(component_layout and item.st_uid==0 and item.st_gid==0
                and item.st_nlink==2 and item.st_mode & 0o700 == 0o700
                and item.st_mode & (0o022|0o7000) == 0)
            temp_names=[]
            directory_fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            nested=bounded_names(directory_fd)
            if nested is None: raise OverflowError("nested entries unbounded")
            if len(nested)>2:
                component_layout=False
            for nested_name in nested:
                nested_path=os.path.join(path,nested_name)
                try:
                    nested_stat=os.stat(nested_name,dir_fd=directory_fd,follow_symlinks=False)
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
                                stable_read=((opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)==
                                    (nested_stat.st_dev,nested_stat.st_ino,nested_stat.st_size,nested_stat.st_mtime_ns)
                                    and (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)==
                                    (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)
                                    and len(data)==opened.st_size)
                                if stable_read:
                                    cursor_valid=journal_cursor(data)
                                    cursor_checked=True
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
                after=bounded_names(directory_fd)
                if after is None: raise OverflowError("nested entries unbounded")
                if len(after)==1 and after[0]=="positions.yml":
                    os.close(directory_fd)
                    raise RetryObservation
                component_layout=False
            os.close(directory_fd)
        else:
            component_layout=False
            if stat.S_ISDIR(item.st_mode):
                top_level["other_directories"]+=1
                if name=="otelcol.receiver.loki.journal":
                    top_level["receiver_component"]+=1
                    role_metadata["receiver"]=role_observation(root,st,root_mount,name,item_mount,item)
                elif name=="otelcol.exporter.otlp.journal":
                    top_level["exporter_component"]+=1
                    role_metadata["exporter"]=role_observation(root,st,root_mount,name,item_mount,item)
                else:
                    observation=role_observation(root,st,root_mount,name,item_mount,item)
                    if name=="otelcol.processor.attributes.journal_labels":
                        top_level["attributes_processor_component"]+=1
                        role_metadata["attributes_processor"]=observation
                    record_unmatched(unmatched_directories,observation)
            elif stat.S_ISLNK(item.st_mode): top_level["other_symlinks"]+=1
            else: top_level["other_kinds"]+=1
        children.append(sig(name,item))
    report = {
        "status":"observed",
        "root":{"uid":st.st_uid, "gid":st.st_gid, "mode":stat.S_IMODE(st.st_mode),
               "dev":st.st_dev, "ino":st.st_ino},
        "children":children, "child_count":len(children), "acl":acl_found,
        "top_level":top_level,
        "role_metadata":role_metadata,
        "journal_metadata":journal_metadata,
        "seed_metadata":seed_metadata,
        "layout_diagnostic":{"root_mode":"safe" if st.st_mode & (0o022|0o7000)==0 else "unsafe"},
        "remotecfg_state":remotecfg_state,
        "unmatched_directories":unmatched_directories,
        "root_is_mount":mount_exact, "child_mount":child_mount,
        "component_layout":component_layout,
        "journal_cursor_presence":cursor_presence,
        "cursor_checked":cursor_checked,
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


def _volume_names(result):
    if result is None or result.returncode != 0 or not isinstance(result.stdout, str):
        return None
    names = result.stdout.splitlines()
    if len(names) > MAX_VOLUMES or any(
        not name or name.strip() != name for name in names
    ) or len(set(names)) != len(names):
        return None
    return names


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

    all_containers = _rows(_json(_call(["podman", "ps", "--all", "--format", "json"], run=run)))
    container_names = _container_names(all_containers)
    if container_names is None or len(container_names) > MAX_CONTAINERS:
        return {"status": "container_inventory_unavailable"}
    collector_present = COLLECTOR in container_names
    expected_name = f"{project}_{VOLUME_KEY}"
    named_names = _volume_names(_call([
        "podman", "volume", "ls", "--quiet", "--filter",
        f"name=^{re.escape(expected_name)}$",
    ], run=run))
    if named_names is None:
        return {"status": "volume_inventory_unavailable"}
    label_names = _volume_names(_call([
        "podman", "volume", "ls", "--quiet",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", f"label=com.docker.compose.volume={VOLUME_KEY}",
    ], run=run))
    if label_names is None:
        return {"status": "volume_inventory_unavailable"}
    if len(label_names) > 1:
        return {"status": "volume_ambiguous"}
    if label_names and label_names[0] != expected_name:
        return {"status": "volume_identity_unsupported"}
    if not named_names and not label_names:
        return {
            "status": "volume_missing",
            "project": project,
            "volume_name": expected_name,
            "volume_inventory_complete": True,
            "name_collision": False,
            "collector_present": collector_present,
            "storage_space": _store_space(run),
        }
    if named_names != [expected_name] or (label_names and label_names != named_names):
        return {"status": "volume_identity_unsupported"}
    inspected = _rows(_json(_call(
        ["podman", "volume", "inspect", expected_name], run=run
    )))
    if inspected is None or len(inspected) != 1:
        return {"status": "volume_inventory_unavailable"}
    volume = inspected[0]
    volume_labels = volume.get("Labels")
    if (
        not isinstance(volume_labels, dict)
        or volume_labels.get("com.docker.compose.project") != project
        or (
            "com.docker.compose.volume" in volume_labels
            and volume_labels.get("com.docker.compose.volume") != VOLUME_KEY
        )
    ):
        return {"status": "volume_identity_unsupported"}
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
    live_mount = dict.fromkeys(
        ("destination", "volume_name", "source", "type", "rw", "user"), "unverified"
    )

    def finish(found):
        if survey_diagnostics is not None:
            found["survey_diagnostics"] = survey_diagnostics
        found["live_mount"] = live_mount
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
        if isinstance(mounts, list) and all(isinstance(item, dict) for item in mounts):
            exact = [item for item in mounts if isinstance(item, dict) and item.get("Destination") == DATA_PATH]
            live_mount["destination"] = "match" if len(exact) == 1 else "mismatch"
            if len(exact) == 1:
                item = exact[0]
                for key, field, expected in (
                    ("volume_name", "Name", name), ("source", "Source", mountpoint),
                    ("type", "Type", "volume"),
                ):
                    value = item.get(field)
                    if isinstance(value, str):
                        live_mount[key] = "match" if value == expected else "mismatch"
                rw = item.get("RW")
                if type(rw) is bool:
                    live_mount["rw"] = "match" if rw is True else "mismatch"
        config = collector[0].get("Config")
        if isinstance(config, dict) and isinstance(config.get("User"), str):
            live_mount["user"] = "match" if config["User"] == "0:0" else "mismatch"
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


def _live_failed_checks(found):
    metadata=found.get("metadata")
    if not isinstance(metadata,dict) or metadata.get("status")!="observed":
        return ["metadata_unavailable"]
    root=metadata.get("root")
    checks=[]
    if not isinstance(root,dict) or root.get("uid")!=0 or root.get("gid")!=0:
        checks.append("owner")
    if metadata.get("component_layout") is not True:
        checks.append("component_layout")
    if metadata.get("remotecfg_state") == "unsafe":
        checks.append("remotecfg_layout")
    if metadata.get("acl") is not False:
        checks.append("acl")
    if metadata.get("root_is_mount") is not False or metadata.get("child_mount") is not False:
        checks.append("mount_boundary")
    if metadata.get("journal_cursor_presence")!="present":
        checks.append("cursor_presence")
    if metadata.get("cursor_checked") is True and metadata.get("journal_cursor_valid") is not True:
        checks.append("cursor_validity")
    if isinstance(root,dict) and not _access(metadata):
        checks.append("positions_access")
    return checks


def _live_mount_diagnostic(found):
    source=found.get("live_mount")
    fields=(source if isinstance(source,dict) else {})
    allowed={"match","mismatch","unverified"}
    return {
        key: value if value in allowed else "unverified"
        for key,value in {
            "destination":fields.get("destination"),
            "volume_name":fields.get("volume_name"),
            "source":fields.get("source"),
            "type":fields.get("type"),
            "rw":fields.get("rw"),
            "user":fields.get("user"),
        }.items()
    }


def _unmatched_directory_diagnostic(metadata):
    categories = {
        "owner": ("matches_collector", "mismatch", "unverified"),
        "access": ("read_write_execute", "blocked", "unverified"),
        "acl": ("absent", "present", "unverified"),
        "mount": ("clear", "ambiguous", "unverified"),
        "children": ("empty", "nonempty", "unverified"),
    }
    source = metadata.get("unmatched_directories") if isinstance(metadata, dict) else None
    count = source.get("count") if isinstance(source, dict) else None
    valid_count = type(count) is int and 0 <= count <= MAX_CHILDREN
    valid_source = isinstance(source, dict) and set(source) == {"count", *categories}
    result = {"count": count if valid_count else "unverified"}
    for field, values in categories.items():
        observed = source.get(field) if isinstance(source, dict) else None
        valid = (
            valid_count
            and valid_source
            and isinstance(observed, dict)
            and set(observed) == set(values)
            and all(type(observed[key]) is int and 0 <= observed[key] <= MAX_CHILDREN for key in values)
            and sum(observed.values()) == count
        )
        result[field] = (
            {key: observed[key] for key in values}
            if valid
            else dict.fromkeys(values, "unverified")
        )
    return result


def _journal_metadata_diagnostic(metadata):
    values = {
        "component": {
            "presence": ("absent", "present", "unverified"),
            "type": ("absent", "directory", "file", "symlink", "other", "unverified"),
            "owner": ("matches_collector", "mismatch", "unverified"),
            "owner_rwx": ("complete", "incomplete", "unverified"),
            "unsafe_mode_bits": ("absent", "present", "unverified"),
            "link_count": ("match", "mismatch", "unverified"),
            "acl": ("absent", "present", "unverified"),
            "mount": ("clear", "ambiguous", "unverified"),
            "children": ("empty", "nonempty", "unverified"),
        },
        "positions": {
            "presence": ("absent", "present", "unverified"),
            "type": ("absent", "regular_file", "symlink", "other", "unverified"),
            "owner": ("matches_collector", "mismatch", "unverified"),
            "owner_rw": ("complete", "incomplete", "unverified"),
            "unsafe_mode_bits": ("absent", "present", "unverified"),
            "link_count": ("match", "mismatch", "unverified"),
            "acl": ("absent", "present", "unverified"),
            "mount": ("clear", "ambiguous", "unverified"),
        },
    }
    source = metadata.get("journal_metadata") if isinstance(metadata, dict) else None
    if not isinstance(source, dict) or set(source) != {
        "component", "positions", "child_count", "temporary_file_metadata", "temporary_files", "unknown_children"
    }:
        source = {}
    result = {}
    for role, fields in values.items():
        observed = source.get(role)
        if not isinstance(observed, dict) or set(observed) != set(fields):
            observed = {}
        result[role] = {
            key: observed.get(key) if observed.get(key) in allowed else "unverified"
            for key, allowed in fields.items()
        }
    child_count = source.get("child_count")
    result["child_count"] = (
        child_count if type(child_count) is int and 0 <= child_count <= MAX_CHILDREN else "unverified"
    )
    temporary_metadata = source.get("temporary_file_metadata")
    result["temporary_file_metadata"] = (
        temporary_metadata if temporary_metadata in ("not_applicable", "safe", "unsafe", "unverified")
        else "unverified"
    )
    count = source.get("temporary_files")
    result["temporary_files"] = count if type(count) is int and 0 <= count <= MAX_CHILDREN else "unverified"
    unknown = source.get("unknown_children")
    kinds = ("files", "directories", "symlinks", "other")
    valid_unknown = (
        isinstance(unknown, dict) and set(unknown) == set(kinds)
        and all(type(unknown[key]) is int and 0 <= unknown[key] <= MAX_CHILDREN for key in kinds)
        and sum(unknown.values()) <= MAX_CHILDREN
    )
    result["unknown_children"] = (
        {key: unknown[key] for key in kinds}
        if valid_unknown
        else dict.fromkeys(kinds, "unverified")
    )
    return result


def _seed_metadata_diagnostic(metadata):
    fields = {
        "presence": ("absent", "present", "unverified"),
        "type": ("absent", "regular_file", "directory", "symlink", "other", "unverified"),
        "owner": ("matches_collector", "mismatch", "unverified"),
        "link_count": ("match", "mismatch", "unverified"),
        "owner_execute": ("absent", "present", "unverified"),
        "group_other_permissions": ("absent", "present", "unverified"),
        "special_bits": ("absent", "present", "unverified"),
        "acl": ("absent", "present", "unverified"),
        "mount": ("clear", "ambiguous", "unverified"),
    }
    observed = metadata.get("seed_metadata") if isinstance(metadata, dict) else None
    if not isinstance(observed, dict) or set(observed) != set(fields):
        observed = {}
    return {
        key: observed.get(key) if observed.get(key) in allowed else "unverified"
        for key, allowed in fields.items()
    }


def _live_diagnostic(found):
    metadata=found.get("metadata")
    top_level=metadata.get("top_level") if isinstance(metadata,dict) else None
    if not isinstance(top_level,dict):
        top_level={
            "seed_file":"unverified","journal_component":"unverified","other_files":"unverified",
            "other_directories":"unverified","other_symlinks":"unverified","other_kinds":"unverified",
            "receiver_component":"unverified","exporter_component":"unverified",
            "attributes_processor_component":"unverified","remotecfg_component":"unverified",
        }
    role_metadata=metadata.get("role_metadata") if isinstance(metadata,dict) else None
    if not isinstance(role_metadata,dict):
        role_metadata={}
    role_values={}
    for role in ("receiver","exporter","attributes_processor","remotecfg"):
        observation=role_metadata.get(role)
        if not isinstance(observation,dict):
            observation={}
        role_values[role]={
            "owner": observation.get("owner") if observation.get("owner") in (
                "matches_collector", "mismatch", "unverified"
            ) else "unverified",
            "access": observation.get("access") if observation.get("access") in (
                "read_write_execute", "blocked", "unverified"
            ) else "unverified",
            "acl": observation.get("acl") if observation.get("acl") in (
                "present", "absent", "unverified"
            ) else "unverified",
            "mount": observation.get("mount") if observation.get("mount") in (
                "clear", "ambiguous", "unverified"
            ) else "unverified",
            "children": observation.get("children") if observation.get("children") in (
                "empty", "nonempty", "unverified"
            ) else "unverified",
        }
    failed_checks=_live_failed_checks(found)
    unverified_checks=[]
    mount=_live_mount_diagnostic(found)
    for key, check in (
        ("destination", "mount_destination"),
        ("volume_name", "mount_volume_name"),
        ("source", "mount_source"),
        ("type", "mount_type"),
        ("rw", "mount_rw"),
        ("user", "container_user"),
    ):
        if mount[key] == "mismatch":
            failed_checks.append(check)
        elif mount[key] == "unverified":
            unverified_checks.append(check)
    if isinstance(metadata, dict) and metadata.get("cursor_checked") is not True:
        unverified_checks.append("cursor_validity")
    remotecfg_state = metadata.get("remotecfg_state") if isinstance(metadata, dict) else None
    if remotecfg_state not in ("absent", "safe", "unsafe", "unverified"):
        remotecfg_state = "unverified"
    if remotecfg_state == "unverified":
        unverified_checks.append("remotecfg_layout")
    root = metadata.get("root") if isinstance(metadata, dict) else None
    root_mode = root.get("mode") if isinstance(root, dict) else None
    layout_diagnostic = {
        "root_mode": (
            "safe" if type(root_mode) is int and 0 <= root_mode <= 0o7777 and root_mode & (0o022 | 0o7000) == 0
            else "unsafe" if type(root_mode) is int and 0 <= root_mode <= 0o7777
            else "unverified"
        ),
        "journal_child_count": _journal_metadata_diagnostic(metadata)["child_count"],
    }
    seed_diagnostic = _seed_metadata_diagnostic(metadata)
    seed_count = top_level.get("seed_file") if isinstance(top_level, dict) else None
    expected_seed_count = 1 if (
        seed_diagnostic["presence"] == "present" and seed_diagnostic["type"] == "regular_file"
    ) else 0 if seed_diagnostic["presence"] == "absent" or (
        seed_diagnostic["presence"] == "present" and seed_diagnostic["type"] != "regular_file"
    ) else None
    if type(seed_count) is not int or expected_seed_count is None or seed_count != expected_seed_count:
        seed_diagnostic = dict.fromkeys(seed_diagnostic, "unverified")
    return {
        "helper_status":found.get("status") if found.get("status") in {
            "observed","volume_initialization_pending","volume_missing","volume_shared",
            "mount_count_unverified","collector_mount_unverified","collector_identity_unverified",
            "metadata_unavailable","volume_initialization_unverified","volume_identity_unsupported",
            "volume_users_unavailable","volume_ambiguous","not_rootless","receiver_unavailable",
            "receiver_identity_unverified","volume_inventory_unavailable","container_inventory_unavailable",
        } else "unavailable",
        "helper_reason":"survey_only",
        "failed_checks":failed_checks,
        "unverified_checks":unverified_checks,
        "mount":mount,
        "top_level": {
            key: top_level.get(key)
            if type(top_level.get(key)) is int and 0 <= top_level.get(key) <= MAX_CHILDREN
            else "unverified"
            for key in (
                "seed_file", "journal_component", "other_files", "other_directories",
                "other_symlinks", "other_kinds", "receiver_component", "exporter_component",
                "attributes_processor_component", "remotecfg_component",
            )
        },
        "role_metadata":role_values,
        "journal_metadata":_journal_metadata_diagnostic(metadata),
        "seed_metadata":seed_diagnostic,
        "layout_diagnostic":layout_diagnostic,
        "remotecfg_state":remotecfg_state,
        "unmatched_directories":_unmatched_directory_diagnostic(metadata),
    }


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
            "remotecfg": "unverified",
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
        "remotecfg": metadata.get("remotecfg_state") if metadata.get("remotecfg_state") in (
            "absent", "safe", "unsafe", "unverified"
        ) else "unverified",
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
    summary["live_diagnostic"] = {
        **_live_diagnostic(found),
        "host_uid_map": "not_run",
        "collector_uid_map": "not_run",
        "uid_maps": "not_run",
        "namespace_probe": "not_run",
    }
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
    if found.get("status") == "volume_missing":
        bootstrap_safe = (
            found.get("volume_inventory_complete") is True
            and found.get("collector_present") is False
            and found.get("name_collision") is False
        )
        bootstrap_safe = bootstrap_safe and _space_is_sufficient(found, require_storage=True)
        if bootstrap_safe:
            return {
                **summary,
                "status": "bootstrap_allowed",
                "reason": "collector_absent_and_volume_safe_to_initialize",
            }
    if (
        found.get("status") == "volume_initialization_pending"
        and found.get("needs_chown", found.get("volume", {}).get("NeedsChown")) is False
        and found.get("needs_copy_up", found.get("volume", {}).get("NeedsCopyUp")) is True
        and found.get("collector_present") is True
        and found.get("volume_use_count") == 1
        and found.get("mount_count") == 1
    ):
        found = {**found, "status": "observed"}
        summary = _summary(found)
    if found.get("status") != "observed":
        return {"status": "refused", "reason": found.get("status", "unavailable")}
    if not _live_safe_metadata(found):
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
    def refused(reason):
        diagnostic=_live_diagnostic(found)
        diagnostic["helper_reason"]=reason if reason in {
            "not_rootless","receiver_unavailable","receiver_identity_unverified",
            "volume_inventory_unavailable","container_inventory_unavailable","volume_ambiguous",
            "volume_missing","volume_identity_unsupported","volume_initialization_unverified",
            "volume_users_unavailable","volume_shared","mount_count_unverified",
            "collector_mount_unverified","collector_identity_unverified","metadata_unavailable",
            "volume_initialization_pending","metadata_ambiguous","positions_access_blocked",
        } else "verification_refused"
        return {"status":"refused","reason":reason,"live_diagnostic":diagnostic}
    if found.get("status") != "observed":
        return refused(found.get("status", "unavailable"))
    if (
        found.get("collector_present") is not True
        or type(found.get("volume_use_count")) is not int
        or found["volume_use_count"] != 1
        or type(found.get("mount_count")) is not int
        or found["mount_count"] != 1
        or found.get("collector_mount_verified") is not True
    ):
        return refused("collector_mount_unverified")
    if not _live_safe_metadata(found):
        return refused("metadata_ambiguous")
    if not _access(found["metadata"]):
        return refused("positions_access_blocked")
    return {
        **_summary(found),
        "children": "alloy_component_layout",
        "status": "ready",
        "reason": "positions_identity_verified",
        "live_diagnostic":{**_live_diagnostic(found),"helper_reason":"positions_identity_verified"},
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
            "live_diagnostic": {
                **_live_diagnostic({"status": "unavailable"}),
                "helper_reason": "survey_failed",
                "host_uid_map": "not_run",
                "collector_uid_map": "not_run",
                "uid_maps": "not_run",
                "namespace_probe": "not_run",
            },
        } if action == "survey" else ({
            "status": "unavailable",
            "reason": "survey_failed",
            "live_diagnostic": {
                **_live_diagnostic({"status": "unavailable"}),
                "helper_reason": "survey_failed",
            },
        } if action == "verify-live" else {
            "status": "unavailable",
            "reason": "survey_failed",
        })
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Survey or narrowly repair the root of the journal collector state volume."""

import json
import subprocess
import sys
import uuid

IMAGE = "docker.io/grafana/alloy:v1.9.2"
RECEIVER = "o11y-alloy"
COLLECTOR = "o11y-journal-collector"
VOLUME_KEY = "journal-collector-state"
DATA_PATH = "/var/lib/alloy/data"
MAX_VOLUMES = 256
MAX_CONTAINERS = 1024
MAX_CHILDREN = 32
MAX_JSON_BYTES = 1_048_576


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
    component_layout=st.st_mode & 0o022 == 0
    for name in sorted(names):
        path=os.path.join(root,name)
        item=os.lstat(path)
        item_mount=mount_id(path)
        child_mount=child_mount or item_mount[0]!=root_mount[0]
        acl_found=acl_found or acl(path)
        if stat.S_ISREG(item.st_mode):
            component_layout=(component_layout and name=="alloy_seed.json"
                and item.st_uid==0 and item.st_gid==0 and item.st_nlink==1)
        elif stat.S_ISDIR(item.st_mode) and name=="loki.source.journal.o11y_alloy":
            component_layout=(component_layout and item.st_uid==0 and item.st_gid==0
                and item.st_nlink==2 and item.st_mode & 0o700 == 0o700
                and item.st_mode & 0o022 == 0)
            nested=os.listdir(path)
            if len(nested)>2:
                component_layout=False
            temp_names=[]
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
                    component_layout=(component_layout and safe_file)
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
                component_layout=(component_layout and len(after)==1 and after[0]=="positions.yml")
                if component_layout:
                    current=os.lstat(os.path.join(path,"positions.yml"))
                    current_mount=mount_id(os.path.join(path,"positions.yml"))
                    current_acl=acl(os.path.join(path,"positions.yml"))
                    acl_found=acl_found or current_acl
                    child_mount=child_mount or current_mount[0]!=root_mount[0]
                    component_layout=(stat.S_ISREG(current.st_mode) and current.st_uid==0
                        and current.st_gid==0 and current.st_nlink==1 and not current_acl
                        and current.st_mode & 0o600 == 0o600
                        and current.st_mode & 0o077 == 0
                        and current.st_mode & 0o7133 == 0)
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
    found={}
    with open("/proc/self/mountinfo",encoding="utf-8") as stream:
        for line in stream:
            left=line.split(" - ",1)[0].split()
            mount=re.sub(r"\\([0-7]{3})",lambda m:chr(int(m.group(1),8)),left[4])
            found[mount]=int(left[0])
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
    if mounts.get(root)==root_mount: raise ValueError
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


def _inspect_metadata(path, run=subprocess.run):
    result = _call(["podman", "unshare", "python3", "-c", _METADATA_SCRIPT, path], run=run)
    value = _json(result)
    if result is None or result.returncode != 0 or not isinstance(value, dict):
        return None
    if value.get("status") not in {"observed", "unavailable", "unbounded"}:
        return None
    return value


def discover(run=subprocess.run):
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

    users = _rows(_json(_call(["podman", "ps", "--all", "--filter", f"volume={name}", "--format", "json"], run=run)))
    if users is None:
        return {"status": "volume_users_unavailable"}
    names = _container_names(users)
    if names is None or len(names) > 32 or any(item != COLLECTOR for item in names) or len(names) > 1:
        return {
            "status": "volume_shared",
            "volume_use_count": min(len(users), 33),
            "collector_present": COLLECTOR in (names or []),
        }
    mount_count = volume.get("MountCount")
    if not isinstance(mount_count, int) or mount_count not in (0, 1) or (not names and mount_count != 0):
        return {"status": "mount_count_unverified", "volume_use_count": len(names), "collector_present": bool(names)}
    if names:
        inspect_collector = ["podman", "inspect", "--type", "container", "--format", "json", COLLECTOR]
        collector = _rows(_json(_call(inspect_collector, run=run)))
        if collector is None or len(collector) != 1:
            return {"status": "collector_mount_unverified", "volume_use_count": 1, "collector_present": True}
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
            return {"status": "collector_mount_unverified", "volume_use_count": 1, "collector_present": True}
        config = collector[0].get("Config")
        if not isinstance(config, dict) or config.get("User") != "0:0":
            return {"status": "collector_identity_unverified", "volume_use_count": 1, "collector_present": True}
    elif collector_present:
        return {"status": "collector_mount_unverified", "volume_use_count": 0, "collector_present": True}

    metadata = _inspect_metadata(mountpoint, run=run)
    if metadata is None or metadata.get("status") != "observed":
        return {"status": "metadata_unavailable", "volume_use_count": len(names), "collector_present": bool(names)}
    needs_chown = volume.get("NeedsChown")
    needs_copy_up = volume.get("NeedsCopyUp")
    if type(needs_chown) is not bool or type(needs_copy_up) is not bool:
        return {
            "status": "volume_initialization_unverified",
            "volume_use_count": len(names),
            "collector_present": collector_present,
            "metadata": metadata,
        }
    found = {
        "status": "volume_initialization_pending" if needs_chown or needs_copy_up else "observed",
        "volume_name": name,
        "mountpoint": mountpoint,
        "volume_use_count": len(names),
        "mount_count": mount_count,
        "collector_present": collector_present,
        "project": project,
        "volume": volume,
        "volume_inventory_complete": True,
        "name_collision": False,
        "metadata": metadata,
    }
    return found


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


def _live_safe_metadata(found):
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
    }


def survey(discover_fn=discover):
    return _summary(discover_fn())


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
        "trap 'rm -f -- \"$a\" \"$b\"' EXIT; "
        "(set -C; : > \"$a\"); printf x >> \"$a\"; "
        "mv -- \"$a\" \"$b\"; test -f \"$b\"; test ! -e \"$a\"; "
        "rm -- \"$b\"; trap - EXIT"
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


def repair_positions(
    discover_fn=discover,
    change_fn=_change_root,
    access_test_fn=_access_test,
    restore_fn=lambda found, uid, gid: _change_root(found, uid, gid),
    image_available_fn=_image_is_available,
):
    first = discover_fn()
    if first.get("status") != "observed":
        return {"status": "refused", "reason": first.get("status", "unavailable")}
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
        or fresh["metadata"] != first["metadata"]
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
            restored = current["metadata"] == first["metadata"]
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
                    and restored_state.get("metadata") == first["metadata"]
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
            or mount_gate["metadata"] != verified["metadata"]
        ):
            return recover("pre_mount_recheck_failed")
        if not access_test_fn(first["volume_name"]):
            return recover("access_test_failed")
    except Exception:
        return recover("post_mutation_check_failed")
    return {"status": "repaired", "reason": "verified"}


def verify(discover_fn=discover):
    found = discover_fn()
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
                and type(volume.get("NeedsChown")) is bool
                and type(volume.get("NeedsCopyUp")) is bool
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
            )
        if bootstrap_safe:
            return {
                **summary,
                "status": "bootstrap_allowed",
                "reason": "collector_absent_and_volume_safe_to_initialize",
            }
        if (
            found.get("status") == "volume_initialization_pending"
            and isinstance(volume, dict)
            and type(volume.get("NeedsChown")) is bool
            and type(volume.get("NeedsCopyUp")) is bool
            and volume["NeedsChown"] is False
            and volume["NeedsCopyUp"] is False
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


def main():
    actions = {"survey": survey, "repair-positions": repair_positions, "verify": verify}
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        result = actions[action]() if action in actions else {"status": "refused", "reason": "invalid_action"}
    except Exception:
        result = {**_summary({"status": "unavailable"}), "reason": "survey_failed"} if action == "survey" else {
            "status": "unavailable",
            "reason": "survey_failed",
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

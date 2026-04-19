import os
from collections import defaultdict


def _normalize_fbx_entries(fbx_records):
    entries = []
    for path_str, file_hash in sorted(fbx_records.items()):
        entries.append(
            {
                "path": path_str,
                "basename": os.path.basename(path_str),
                "hash": file_hash,
            }
        )
    return entries


def _remove_exact_name_hash_matches(previous_entries, current_entries):
    previous_by_key = defaultdict(list)
    current_by_key = defaultdict(list)

    for entry in previous_entries:
        previous_by_key[(entry["basename"], entry["hash"])].append(entry)
    for entry in current_entries:
        current_by_key[(entry["basename"], entry["hash"])].append(entry)

    remaining_previous = []
    remaining_current = []

    for key in sorted(set(previous_by_key) | set(current_by_key)):
        previous_list = previous_by_key.get(key, [])
        current_list = current_by_key.get(key, [])
        shared_count = min(len(previous_list), len(current_list))

        remaining_previous.extend(previous_list[shared_count:])
        remaining_current.extend(current_list[shared_count:])

    return remaining_previous, remaining_current


def calculate_fbx_diff(previous_fbx, current_fbx):
    previous_entries = _normalize_fbx_entries(previous_fbx)
    current_entries = _normalize_fbx_entries(current_fbx)

    remaining_previous, remaining_current = _remove_exact_name_hash_matches(
        previous_entries, current_entries
    )

    previous_by_name = defaultdict(list)
    current_by_name = defaultdict(list)

    for entry in remaining_previous:
        previous_by_name[entry["basename"]].append(entry)
    for entry in remaining_current:
        current_by_name[entry["basename"]].append(entry)

    added = []
    changed = []
    deleted = []

    for name in sorted(set(previous_by_name) | set(current_by_name)):
        previous_list = sorted(
            previous_by_name.get(name, []),
            key=lambda entry: (entry["hash"], entry["path"]),
        )
        current_list = sorted(
            current_by_name.get(name, []),
            key=lambda entry: (entry["hash"], entry["path"]),
        )

        shared_count = min(len(previous_list), len(current_list))
        for index in range(shared_count):
            changed.append(
                {
                    "basename": name,
                    "previous_hash": previous_list[index]["hash"],
                    "current_hash": current_list[index]["hash"],
                    "previous_path": previous_list[index]["path"],
                    "current_path": current_list[index]["path"],
                }
            )

        deleted.extend(previous_list[shared_count:])
        added.extend(current_list[shared_count:])

    return added, changed, deleted


def _short_hash(file_hash):
    return file_hash[:8]


def _group_status(statuses):
    unique_statuses = {status for status in statuses if status != 0}
    if not unique_statuses:
        return 0
    if len(unique_statuses) == 1:
        return unique_statuses.pop()
    return 3


def _format_added_detail(entry):
    return f"{entry['path']} [new {_short_hash(entry['hash'])}]"


def _format_deleted_detail(entry):
    return f"{entry['path']} [old {_short_hash(entry['hash'])}]"


def _format_changed_detail(entry):
    if entry["previous_path"] == entry["current_path"]:
        return (
            f"{entry['current_path']} "
            f"[{_short_hash(entry['previous_hash'])} -> {_short_hash(entry['current_hash'])}]"
        )

    return (
        f"{entry['previous_path']} [old {_short_hash(entry['previous_hash'])}] -> "
        f"{entry['current_path']} [new {_short_hash(entry['current_hash'])}]"
    )


def build_fbx_path_list(added_entries, changed_entries, deleted_entries):
    groups = defaultdict(list)

    for entry in sorted(
        changed_entries,
        key=lambda item: (item["basename"], item["current_path"], item["previous_path"]),
    ):
        groups[entry["basename"]].append(
            {
                "line_str": f"    {_format_changed_detail(entry)}",
                "status": 3,
            }
        )

    for entry in sorted(added_entries, key=lambda item: (item["basename"], item["path"])):
        groups[entry["basename"]].append(
            {
                "line_str": f"    {_format_added_detail(entry)}",
                "status": 1,
            }
        )

    for entry in sorted(deleted_entries, key=lambda item: (item["basename"], item["path"])):
        groups[entry["basename"]].append(
            {
                "line_str": f"    {_format_deleted_detail(entry)}",
                "status": 2,
            }
        )

    path_list = []
    for basename in sorted(groups):
        child_items = groups[basename]
        path_list.append(
            {
                "line_str": basename,
                "status": _group_status(item["status"] for item in child_items),
            }
        )
        path_list.extend(child_items)

    return path_list

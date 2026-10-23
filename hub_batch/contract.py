"""Private Provider Profile + Batch HTTP contract shared by both entries."""
from .jobs import committed_files
from .provider import profile_output_contract

CONTRACT_VERSION = "2026-10-23.v1"


def submission(payload):
    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object")
    allowed = {"profile_id", "profile", "profile_version", "implementation_version", "recipe",
               "inputs", "input_paths", "output_dir", "options"}
    if set(payload) - allowed:
        raise ValueError("unsupported request fields: " + ", ".join(sorted(set(payload) - allowed)))
    if not isinstance(payload.get("options", {}), dict) or payload.get("options"):
        raise ValueError("this provider accepts only empty options")
    for first, second in (("profile_id", "profile"), ("inputs", "input_paths")):
        if first in payload and second in payload and payload[first] != payload[second]:
            raise ValueError(f"{first} and {second} must agree")
    inputs = payload.get("inputs", payload.get("input_paths"))
    if not isinstance(inputs, list) or not inputs or any(not isinstance(item, str) or not item for item in inputs):
        raise ValueError("inputs must be a non-empty list of paths")
    profile = payload.get("profile_id", payload.get("profile"))
    recipe = payload.get("recipe")
    if bool(profile) == bool(recipe):
        raise ValueError("exactly one of profile_id/profile or recipe is required")
    for key in ("profile_id", "profile", "profile_version", "implementation_version", "recipe", "output_dir"):
        if key in payload and payload[key] is not None and (not isinstance(payload[key], str) or not payload[key]):
            raise ValueError(f"{key} must be a non-empty string")
    if profile and not payload.get("profile_version"):
        raise ValueError("profile_version is required")
    return dict(recipe=recipe, paths=inputs, output_dir=payload.get("output_dir"), profile=profile,
                profile_version=payload.get("profile_version"), options=payload.get("options", {}),
                implementation_version=payload.get("implementation_version"))


def discovery(registry):
    profiles = []
    for manifest in registry.list():
        roles, media = profile_output_contract(manifest)
        profiles.append({"profile_id": manifest.name, "profile_version": manifest.version,
                         "implementation_version": manifest.digest, "output_roles": roles,
                         "output_media": media, "supports_single": True, "supports_batch": True,
                         "options": dict(manifest.options), "manifest": manifest.to_dict()})
    return {"contract_version": CONTRACT_VERSION, "profiles": profiles}


def receipt(task):
    """Keep old task fields while making semantic identity and state explicit."""
    files = []
    for item in task.get("files", []):
        outputs = item.get("outputs", [])
        state = item.get("status", "failed")
        files.append({**item, "state": "succeeded" if state == "success" else state,
                      "status": "success" if state == "succeeded" else state,
                      "output_files": [str(output["path"]) for output in outputs if output.get("path")]})
    profile = task.get("profile") or {}
    status = task.get("status", "unknown")
    return {**task, "task_id": task["id"], "status": status,
            "state": "cancel_requested" if status == "cancelling" else status,
            "profile_id": profile.get("name"), "profile_version": profile.get("version"),
            "implementation_version": profile.get("digest"), "stage": task.get("stage", status),
            "progress": task.get("progress"), "created_at": task.get("created_at"),
            "started_at": task.get("started_at"), "updated_at": task.get("updated_at"),
            "finished_at": task.get("finished_at"), "cancel_requested": task.get("cancel_requested", False),
            "files": files, "error": task.get("error")}


def result_task(task):
    """Read committed results even if the progress observer or process was lost."""
    saved = committed_files(task)
    return {**task, "files": saved, "processed_files": len(saved)} if saved is not None else task

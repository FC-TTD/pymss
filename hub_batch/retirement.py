"""Creation policy, deliberately separate from immutable historical manifests."""

RETIRED_PROFILE_VERSIONS = frozenset({"2026-09-20.v1", "2026-10-23.v1"})


class ProfileRetired(ValueError):
    code = "profile_retired"


def require_active_version(version):
    if isinstance(version, str) and version in RETIRED_PROFILE_VERSIONS:
        raise ProfileRetired(f"profile_retired: {version} is unavailable for new tasks")


def validate_compat_identity(payload):
    """Expert model requests stay supported; profile identities cannot be ignored.

    The compatibility engine cannot execute a full versioned profile, so callers
    must use /v1/separations instead of silently selecting its first model.
    """
    require_active_version(payload.get("profile_version"))
    if any(key in payload for key in ("profile", "profile_id", "profile_version", "implementation_version")):
        raise ValueError("versioned profiles require /v1/separations")

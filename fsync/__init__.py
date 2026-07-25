"""fsync package initializer."""

__all__ = ["fileindex"]

# Version + capability set for meta-synchronization: the two boxes must run
# fsync builds whose FEATURES satisfy what a synced config declares it `requires`.
# Bump FEATURES when adding a capability a config can depend on; the config's
# `requires:` list is checked against this set at load time (see homesync.load_config)
# so a box never acts on config its code doesn't yet understand (the skew window).
__version__ = "0.5.0"
FEATURES = frozenset({
    "home-sync",   # P1-P4: profile-driven file sync
    "git-sync",    # P5: full-fidelity git working-tree sync (kind: git)
    "meta-sync",   # config include + requires gate + `fsync meta`
    "folder-sync", # P8: ad-hoc VCS-aware `fsync sync folder` (SVN-first)
    "git-auto-apply",  # ISSUE-001 fix C: kind:git `apply: auto` (file-skip + guarded receiver apply)
})

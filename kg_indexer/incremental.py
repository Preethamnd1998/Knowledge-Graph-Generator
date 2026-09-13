"""Change detection for incremental re-indexing.

mtime+size is the fast path (trust it when unchanged, skip hashing). A
mismatch falls back to a sha256 content hash before deciding a file
actually changed, so a touch-only change (checkout, build noise) doesn't
trigger a needless re-parse — the concrete failure mode a naive
timestamp-only approach would produce.
"""
import hashlib
import os


def _file_fingerprint(path):
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def classify_files(db, service, root_path, all_files):
    """Given every source file discovered on disk for this target (absolute
    paths, as walk_source_files yields), decide which need re-parsing.
    Returns (changed, unchanged) as the SAME absolute paths passed in —
    parsers filter their own walk against these, so the shape must match
    what they iterate over. Internally tracks fingerprints keyed by the
    relative path (relative to `root_path`), matching the file_path
    convention every parser stores on its nodes (see e.g.
    java_parser.py:301 `rel_file = os.path.relpath(file_path, service_path)`)
    — using the absolute path as the tracking key would silently never
    match, since nothing is ever stored that way.

    Deletes stale nodes for changed files here (once, before any parser
    touches them) and updates each file's stored fingerprint. Does NOT
    insert anything — that's still each parser's job for `changed` files.
    """
    changed = []
    unchanged = []
    for file_path in all_files:
        rel_file = os.path.relpath(file_path, root_path)
        mtime_ns, size = _file_fingerprint(file_path)
        stored = db.get_file_fingerprint(rel_file, service)
        if stored and stored["mtime_ns"] == mtime_ns and stored["size"] == size:
            unchanged.append(file_path)
            continue
        digest = _file_hash(file_path)
        if stored and stored["sha256"] == digest:
            # Touched but byte-identical — update the cheap fingerprint so
            # next run's mtime/size check hits the fast path again, but
            # don't touch its nodes.
            db.set_file_fingerprint(rel_file, service, mtime_ns, size, digest)
            unchanged.append(file_path)
            continue
        # Always clear whatever's currently stored for this file before it's
        # reparsed — including when `stored` is None (e.g. a .db written
        # before file_fingerprints existed: every file looks "new" the first
        # time round, but old nodes from that prior full run are still
        # sitting in the DB and must not be left stale). delete_nodes_for_file
        # is a no-op if there's genuinely nothing there yet.
        db.delete_nodes_for_file(rel_file, service)
        db.set_file_fingerprint(rel_file, service, mtime_ns, size, digest)
        changed.append(file_path)
    return changed, unchanged


def sweep_deleted(db, service, root_path, files_seen_this_run):
    """Remove nodes/fingerprints for any file this service used to track
    that no longer appears on disk. Returns the count removed."""
    tracked = db.list_tracked_files(service)
    seen = {os.path.relpath(p, root_path) for p in files_seen_this_run}
    removed = 0
    for rel_file in tracked:
        if rel_file not in seen:
            db.delete_nodes_for_file(rel_file, service)
            db.delete_file_fingerprint(rel_file, service)
            removed += 1
    return removed

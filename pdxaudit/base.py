"""What a run compares the mod against: vanilla alone, or vanilla and the mod's
foundations flattened into one stack.

A base lists its points newest first as (id, message) pairs, as the tracker lists
its commits, and gives each point's parsed indexes in the shapes the audits read.
A vanilla base's points are the tracker's commits and its indexes are the tracker's.

A stack's points are vanilla's versions in tracker order, with each patched version
of a foundation inserted after the vanilla version matching its patch; several
foundation versions on one patch follow the stored foundation order, then their own
history order. A point's tag is the version tag for a vanilla point and
`<source id> <version>` for a foundation point. A foundation version without a patch
is never used. Each point's flattened GUI and block indexes are cached as an overlay
on vanilla's, under <data>/stacks/<stack hash>/cache/, named by the point, whose id
covers the member commits, and so the patch assignments and foundation order."""
import hashlib
import json
import re
from collections import namedtuple
from pathlib import Path

from . import flatten, session
from .safety import RefusedRemoval, remove_file
from .store import data_root
from .tracker import CACHE_FILE_RE, Message, get_commits, read_blobs, tag_of, tree_files

Point = namedtuple("Point", "id msg vanilla vanilla_tag members layer version")
Point.__doc__ = """One entry of a stack's history. members: (commit, tag) or None per
foundation, in load order. layer: 'vanilla' or the source id that made this point."""

STACK_CACHE_VERSION = 1
VANILLA = "vanilla"


def as_base(base):
    """A base for `base`, which is a base already or the vanilla tracker's path."""
    return base if hasattr(base, "gui_index") else VanillaBase(base)


class VanillaBase:
    """Vanilla alone: the tracker's commits and indexes."""
    stacked = False

    def __init__(self, repo):
        self.repo = str(repo)

    def commits(self):
        return session.memo(("base.commits", self.repo), lambda: get_commits(self.repo))

    def layer_of(self, point):
        return VANILLA

    def vanilla_commit(self, point):
        return point

    def gui_index(self, point, modules, label=""):
        from .gui import build_gui_vanilla_cached
        return build_gui_vanilla_cached(self.repo, point, modules, label)

    def gui_owners(self, point, modules):
        """({definition key: owner}, {file path: owner}) for units a foundation owns."""
        return {}, {}

    def block_index(self, point, categories, label=""):
        from .overrides import build_index_cached
        return build_index_cached(self.repo, point, categories, label)

    def block_owners(self, point, categories):
        return {}

    def names_defined(self, point, categories, names):
        from .overrides import names_defined_in_vanilla
        return names_defined_in_vanilla(self.repo, point, categories, names)

    def scalar_values(self, point, categories, names):
        from .overrides import vanilla_scalar_values
        return vanilla_scalar_values(self.repo, point, categories, names)

    def vocab(self, point, label=""):
        from .overrides import build_vocab
        return build_vocab(self.repo, point, label)

    def loc(self, point, wanted, label=""):
        from .loc import build_loc_vanilla
        return build_loc_vanilla(self.repo, point, wanted, label)

    def loc_owners(self, point, wanted):
        return {}

    def definitions(self, point):
        from .dupes import vanilla_definitions
        return vanilla_definitions(self.repo, point)

    def definition_owners(self, point):
        return {}

    def merge_types(self, point):
        from .dupes import merge_types, vanilla_definitions
        return merge_types(vanilla_definitions(self.repo, self.vanilla_commit(point)))

    def prune(self):
        pass


def _point_id(vanilla, members):
    payload = json.dumps([vanilla, [list(m) if m else None for m in members]])
    return hashlib.sha1(payload.encode()).hexdigest()


def stack_points(repo, foundations, limits=None):
    """The stack's points, oldest first. `limits` maps a source id to the newest of its
    versions to use."""
    from .sources import SourceError
    limits = limits or {}
    versions = []
    for src in foundations:
        vs = src.versions()
        if src.id in limits:
            tags = [t for _c, t, _p in vs]
            if limits[src.id] not in tags:
                raise SourceError(f"{src.id} has no version {limits[src.id]}; its versions: "
                                  f"{', '.join(tags) or 'none'}")
            vs = vs[:tags.index(limits[src.id]) + 1]
        versions.append(vs)
    state, points = [None] * len(foundations), []
    for h, msg in reversed(get_commits(repo)):
        vt = tag_of(msg)
        points.append(Point(_point_id(h, state), Message(msg, vt), h, vt, tuple(state), VANILLA, vt))
        for n, src in enumerate(foundations):
            for commit, tag, patch in versions[n]:
                if patch == vt:
                    state[n] = (commit, tag)
                    label = f"{src.id} {tag}"
                    points.append(Point(_point_id(h, state), Message(f"{msg} + {label}", label), h, vt, tuple(state),
                                        src.id, tag))
    return points


class StackBase(VanillaBase):
    """Vanilla and the mod's foundations, flattened at every point."""
    stacked = True

    def __init__(self, repo, foundations, limits=None):
        super().__init__(repo)
        self.foundations = list(foundations)
        self.points = stack_points(self.repo, self.foundations, limits)
        self.by_id = {p.id: p for p in self.points}
        self.stack_hash = hashlib.sha1(",".join(s.key for s in self.foundations).encode()).hexdigest()[:12]

    @property
    def cache_dir(self):
        return data_root() / "stacks" / self.stack_hash / "cache"

    def commits(self):
        return [(p.id, p.msg) for p in reversed(self.points)]

    def layer_of(self, point):
        return self.by_id[point].layer

    def vanilla_commit(self, point):
        return self.by_id[point].vanilla

    def layers(self, point):
        """[(source, version, commit)] of the foundations present at a point, in load order."""
        p = self.by_id[point]
        return [(src, m[1], m[0]) for src, m in zip(self.foundations, p.members) if m]

    def _cache(self, kind, point, parts):
        extra = hashlib.sha1(",".join(sorted(parts)).encode()).hexdigest()[:12]
        return self.cache_dir / f"{kind}-v{STACK_CACHE_VERSION}-{point}-{extra}.json"

    def _overlay(self, path, build):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        overlay = build()
        from .sources import _write_json
        try:
            _write_json(path, overlay)
        except OSError:
            pass
        return overlay

    # --- GUI ---

    def _gui(self, point, modules, label=""):
        p = self.by_id[point]

        def build():
            from .gui import build_gui_vanilla_cached
            van = build_gui_vanilla_cached(self.repo, p.vanilla, modules, label)
            layers = self.layers(point)
            if not layers:
                return (*van, {}, {})

            def make():
                defs, files, bad, owners, file_owners = flatten.flatten_gui(van, p.vanilla_tag, layers, modules)
                return {"defs": [[list(k), list(v)] for k, v in defs.items() if van[0].get(k) != v],
                        "removed": [list(k) for k in van[0] if k not in defs],
                        "files": {f: t for f, t in files.items() if van[1].get(f) != t},
                        "bad": bad, "owners": [[list(k), o] for k, o in owners.items()],
                        "file_owners": file_owners}
            overlay = self._overlay(self._cache("gui", point, modules), make)
            defs = dict(van[0])
            for k in overlay["removed"]:
                defs.pop(tuple(k), None)
            defs.update({tuple(k): tuple(v) for k, v in overlay["defs"]})
            files = dict(van[1])
            files.update(overlay["files"])
            return (defs, files, overlay["bad"], {tuple(k): o for k, o in overlay["owners"]},
                    overlay["file_owners"])
        return session.memo(("stack.gui", self.stack_hash, point, tuple(sorted(modules))), build)

    def gui_index(self, point, modules, label=""):
        return self._gui(point, modules, label)[:3]

    def gui_owners(self, point, modules):
        return self._gui(point, modules)[3:]

    # --- script blocks ---

    def _merging(self):
        from .dupes import merge_types, vanilla_definitions
        return session.memo(("stack.merging", self.repo),
                            lambda: merge_types(vanilla_definitions(self.repo, self.points[-1].vanilla)))

    def _blocks(self, point, categories, label=""):
        p = self.by_id[point]

        def build():
            from .overrides import build_index_cached
            van = build_index_cached(self.repo, p.vanilla, categories, label)
            layers = self.layers(point)
            if not layers:
                return van, {}

            def make():
                idx, owners = flatten.flatten_blocks(van, p.vanilla_tag, layers, categories, self._merging())
                return {"idx": [[list(k), list(v)] for k, v in idx.items() if van.get(k) != v],
                        "removed": [list(k) for k in van if k not in idx],
                        "owners": [[list(k), o] for k, o in owners.items()]}
            overlay = self._overlay(self._cache("blocks", point, categories), make)
            idx = dict(van)
            for k in overlay["removed"]:
                idx.pop(tuple(k), None)
            idx.update({tuple(k): tuple(v) for k, v in overlay["idx"]})
            return idx, {tuple(k): o for k, o in overlay["owners"]}
        return session.memo(("stack.blocks", self.stack_hash, point, tuple(sorted(set(categories)))), build)

    def block_index(self, point, categories, label=""):
        return self._blocks(point, categories, label)[0]

    def block_owners(self, point, categories):
        return self._blocks(point, categories)[1]

    def names_defined(self, point, categories, names):
        found = super().names_defined(self.vanilla_commit(point), categories, names)
        return flatten.flatten_names(found, self.layers(point), categories, set(names))

    def scalar_values(self, point, categories, names):
        values = super().scalar_values(self.vanilla_commit(point), categories, names)
        return flatten.flatten_scalars(values, self.layers(point), categories, set(names))

    # --- vocabulary, localization, definitions ---

    def vocab(self, point, label=""):
        van = super().vocab(self.vanilla_commit(point), label)
        layers = self.layers(point)
        if not layers:
            return van
        replaced = flatten.layer_paths(layers, flatten._any_txt)
        blobs = [b for path, b in tree_files(self.repo, self.vanilla_commit(point)) if path in replaced]
        texts = [raw.decode("utf-8-sig", errors="replace") for raw in read_blobs(self.repo, blobs).values()]
        return flatten.flatten_vocab(van, texts, layers)

    def _loc(self, point, wanted, label=""):
        def build():
            from .loc import build_loc_vanilla
            layers = self.layers(point)
            exclude = flatten.layer_paths(layers, flatten._loc_file)
            van = build_loc_vanilla(self.repo, self.vanilla_commit(point), wanted, label, exclude=exclude)
            return flatten.flatten_loc(van, layers, wanted)
        return session.memo(("stack.loc", self.stack_hash, point, frozenset(wanted)), build)

    def loc(self, point, wanted, label=""):
        return self._loc(point, wanted, label)[0]

    def loc_owners(self, point, wanted):
        return self._loc(point, wanted)[1]

    def _definitions(self, point):
        return session.memo(("stack.definitions", self.stack_hash, point), lambda: flatten.flatten_definitions(
            super(StackBase, self).definitions(self.vanilla_commit(point)), self.by_id[point].vanilla_tag,
            self.layers(point)))

    def definitions(self, point):
        return self._definitions(point)[0]

    def definition_owners(self, point):
        return self._definitions(point)[1]

    # --- caches ---

    def prune(self):
        """Delete cached overlays of points this stack no longer has, and every overlay
        of a stack no mod's foundations make up any more."""
        prune_stacks({self.stack_hash: set(self.by_id)})


_POINT_RE = re.compile(r"-v\d+-([0-9a-f]{40})-")


def prune_stacks(current):
    """Delete stack overlay files whose point is not among `current` {stack hash: point
    ids}, and those of stacks no mod's stored foundations make up."""
    from .sources import _read_json
    root = data_root()
    used = set(current)
    for f in root.glob("*/sources.json") if root.is_dir() else []:
        keys = [e.get("key", "") for e in _read_json(f, {}).get("foundations") or [] if isinstance(e, dict)]
        if keys:
            used.add(hashlib.sha1(",".join(keys).encode()).hexdigest()[:12])
    stacks = root / "stacks"
    if not stacks.is_dir():
        return
    for folder in stacks.iterdir():
        cache = folder / "cache"
        if not cache.is_dir():
            continue
        for fp in cache.glob("*.json"):
            m = _POINT_RE.search(fp.name)
            if not re.fullmatch(CACHE_FILE_RE, fp.name) or not m:
                continue
            keep = folder.name in current and m.group(1) in current[folder.name]
            if folder.name not in used or (folder.name in current and not keep):
                try:
                    remove_file(fp, cache, CACHE_FILE_RE)
                except (RefusedRemoval, OSError):
                    pass

"""The vanilla versions that a run compares the mod with.

A base gives the commits of the tracker as (id, message) pairs. The newest commit
is first. For each commit, a base also gives the parsed indexes that the audits
read."""
from . import session
from .tracker import get_commits


def as_base(base):
    """Returns a base. The `base` argument is a base, or the path to the tracker."""
    return base if hasattr(base, "gui_index") else VanillaBase(base)


class VanillaBase:
    """Vanilla only. The commits and the indexes come from the tracker."""

    def __init__(self, repo):
        self.repo = str(repo)

    def commits(self):
        return session.memo(("base.commits", self.repo), lambda: get_commits(self.repo))

    def gui_index(self, point, modules, label=""):
        from .gui import build_gui_vanilla_cached
        return build_gui_vanilla_cached(self.repo, point, modules, label)

    def block_index(self, point, categories, label=""):
        from .overrides import build_index_cached
        return build_index_cached(self.repo, point, categories, label)

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

    def definitions(self, point):
        from .dupes import vanilla_definitions
        return vanilla_definitions(self.repo, point)

    def merge_types(self, point):
        from .dupes import merge_types, vanilla_definitions
        return merge_types(vanilla_definitions(self.repo, point))

# pdx-audit

Diff mod overrides and referenced tokens against vanilla patch changes.

Two audits, both driven by the vanilla-tracker bare git repo:

  Override audit (default): finds every INJECT:/REPLACE:/TRY_INJECT: directive
  in the mod, locates the target block in vanilla at two commits (old, new), and
  reports what vanilla changed. For changed REPLACE blocks it also checks the
  mod's replacement text and reports whether it already contains vanilla's new
  lines (reconciled) or is missing them (stale). REPLACE blocks vanilla changed
  are highest priority — the mod may be silently suppressing those improvements.

  Dependency audit (--deps): extracts every `token =` identifier the mod's
  scripts assign, then flags any that vanilla used at the old commit but no
  longer uses at the new commit (present-then-gone = likely renamed/removed).
  Catches silent breakage the override audit cannot see, e.g. a modifier key
  the mod writes that vanilla dropped. Suggests rename candidates. Findings are
  suspects to verify with pdx-syntax, not confirmed breakage.

  GUI audit (--gui): finds implicit GUI overrides — mod .gui template/type
  definitions that shadow a same-name vanilla definition (first-loaded file
  wins, hence the aaa_ prefix convention), plus mod .gui files that replace a
  vanilla file at the same relative path — and reports which shadowed
  definitions vanilla changed between the two commits. For changed definitions
  it also checks whether the mod's copy already contains vanilla's new lines.
  Load order is approximated by case-insensitive path sort; findings are
  suspects to verify in game.

Requires a vanilla-tracker bare git repo. Searches for it at:
  1. --vanilla-repo <path> argument
  2. <mod-parent>/vanilla-tracker/repo.git
  3. PDX_VANILLA_REPO environment variable

The --deps vanilla vocabulary is cached per tracker commit under
<vanilla-tracker>/cache/ — keyed by commit hash, so entries never go stale.
On every run a sample of live game files ($PDX_GAME_ROOT or the default
install) is hashed against the newest tracked commit; a mismatch means the
game patched but the tracker was not updated, and a warning is printed.

Usage:
    pdx-audit                              # changed override blocks only
    pdx-audit --all                        # include unchanged
    pdx-audit --diff                       # show unified diffs
    pdx-audit --deps                       # dependency audit (dropped tokens)
    pdx-audit --gui                        # GUI override audit (implicit shadowing)
    pdx-audit --full                       # widen window to oldest tracked commit
    pdx-audit --block farming_village
    pdx-audit --old abc1234 --new def5678
    pdx-audit --mod-root /path/to/mod

## Install

Self-contained Python 3.9+ script, no dependencies. Symlink or copy it onto your PATH:

```bash
ln -s "$(pwd)/pdx-audit" ~/.local/bin/pdx-audit
```

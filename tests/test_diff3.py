"""Copy against vanilla's current text, each difference attributed with vanilla's
history: vanilla's changes the copy lacks, conflicts, and the copy's own edits."""
from pdx_utilities.script_parser import parse, tokenize
from pdxaudit.diff3 import align, baseline, compare, nodes


def _changes(mod, *versions, unwrap=False):
    out = []
    for c in compare(mod, list(versions), unwrap=unwrap):
        node = c.mod or c.new
        out.append((c.kind, c.priority, node.label, c.since))
    return sorted(out)


def test_a_conflict_holds_vanillas_statement_before_the_change():
    old, new = "w = { price = 8 x = 1 }", "w = { price = 6 x = 1 }"
    [c] = compare("w = { price = 4 x = 1 }", [old, new])
    assert c.kind == "both_changed" and old[c.old.start:c.old.end] == "price = 8"


def test_a_statement_you_commented_out_is_your_deletion():
    mod = "w = {\n\ta = 1\n\t#b = 2   # not wanted\n}"
    assert _changes(mod, "w = { a = 1 }", "w = { a = 1 b = 2 }") == [("mod_removed", "info", "b", None)]


def test_a_prose_comment_is_not_a_commented_statement():
    mod = "w = {\n\ta = 1\n\t# b = 2 is too much here\n}"
    assert _changes(mod, "w = { a = 1 }", "w = { a = 1 b = 2 }") == [("vanilla_added", "mid", "b", 1)]


def test_a_comment_holding_an_older_value_is_still_your_deletion():
    # The comment records what you took out; vanilla changing it afterwards does not
    # put it back, so there is nothing for you to do.
    mod = "w = {\n\ta = 1\n\t#b = 1\n}"
    assert _changes(mod, "w = { a = 1 b = 1 }", "w = { a = 1 b = 2 }") == [("removed_changed", "info", "b", 1)]


def test_layout_comments_and_number_spelling_are_not_differences():
    mod = "w = {\n\ta = 1\n\tb = {\n\t\tc = 2\n\t}\n}"
    assert _changes(mod, "w = { a = 1.0 b = {c=2} } # note") == []


def test_vanilla_change_to_an_untouched_block_is_mid():
    assert _changes("w = { a = 1 b = 2 }", "w = { a = 1 b = 2 }", "w = { a = 5 b = 2 }") == [
        ("vanilla_changed", "mid", "a", 1)]


def test_vanilla_change_beside_your_edit_is_high():
    assert _changes("w = { a = 1 b = 9 }", "w = { a = 1 b = 2 }", "w = { a = 5 b = 2 }") == [
        ("mod_changed", "info", "b", None), ("vanilla_changed", "high", "a", 1)]


def test_your_edit_in_a_nested_block_does_not_raise_its_parent():
    assert _changes("w = { a = 1 x = { k = 9 } }",
                    "w = { a = 1 x = { k = 1 } }", "w = { a = 2 x = { k = 1 } }") == [
        ("mod_changed", "info", "k", None), ("vanilla_changed", "mid", "a", 1)]


def test_both_changed_is_mid():
    assert _changes("w = { a = 7 }", "w = { a = 1 }", "w = { a = 5 }") == [
        ("both_changed", "mid", "a", 1)]


def test_a_conflict_raises_the_vanilla_changes_beside_it():
    assert _changes("w = { a = 7 b = 1 }", "w = { a = 1 b = 1 }", "w = { a = 5 b = 2 }") == [
        ("both_changed", "mid", "a", 1), ("vanilla_changed", "high", "b", 1)]


def test_vanilla_deleting_a_statement_you_changed_is_both_changed():
    assert _changes("w = { a = 1 b = 7 }", "w = { a = 1 b = 1 }", "w = { a = 1 }") == [
        ("both_changed", "mid", "b", 1)]


def test_vanilla_addition_you_lack():
    assert _changes("w = { a = 1 }", "w = { a = 1 }", "w = { a = 1 b = 2 }") == [
        ("vanilla_added", "mid", "b", 1)]


def test_adopted_changes_are_quiet():
    assert _changes("w = { a = 5 b = 2 }", "w = { a = 1 }", "w = { a = 5 b = 2 }") == []


def test_statement_vanilla_removed_that_you_carry():
    assert _changes("w = { a = 1 b = 2 }", "w = { a = 1 b = 2 }", "w = { a = 1 }") == [
        ("vanilla_removed", "mid", "b", 1)]


def test_your_additions_and_removals_are_info():
    assert _changes("w = { a = 1 z = 3 }", "w = { a = 1 b = 2 }", "w = { a = 1 b = 2 }") == [
        ("mod_added", "info", "z", None), ("mod_removed", "info", "b", None)]


def test_vanilla_changing_a_statement_you_removed_is_your_own_edit():
    # Deleted is deleted: the copy behaves the same whatever vanilla does to the
    # statement afterwards, so it is info, as a deletion vanilla never touched is.
    assert _changes("w = { a = 1 }", "w = { a = 1 b = 2 }", "w = { a = 1 b = 3 }") == [
        ("removed_changed", "info", "b", 1)]


def test_copy_synced_to_a_middle_version_reads_later_changes_as_vanilla():
    versions = ("w = { a = 1 }", "w = { a = 2 x = { k = 1 } }", "w = { a = 3 x = { k = 2 } }")
    assert _changes("w = { a = 2 x = { k = 1 } }", *versions) == [
        ("vanilla_changed", "mid", "a", 2), ("vanilla_changed", "mid", "k", 2)]


def test_statement_vanilla_added_then_removed_that_you_carry():
    assert _changes("w = { a = 1 t = 1 }",
                    "w = { a = 1 }", "w = { a = 1 t = 1 }", "w = { a = 1 }") == [
        ("vanilla_removed", "mid", "t", 2)]


def test_contents_of_a_block_vanilla_added_arrive_with_it():
    assert _changes("w = { a = 1 x = { k = 5 j = 1 } }",
                    "w = { a = 1 }", "w = { a = 1 x = { k = 1 j = 1 } }") == [
        ("mod_changed", "info", "k", None)]


def test_history_starts_where_the_text_last_appeared():
    assert _changes("w = { a = 1 }", None, "w = { a = 1 }", "w = { a = 2 }") == [
        ("vanilla_changed", "mid", "a", 2)]


def test_repeated_blocks_pair_by_order_around_identical_ones():
    old = "w = { icon = { t = a } icon = { t = b } }"
    new = "w = { icon = { t = new } icon = { t = a } icon = { t = b } }"
    assert _changes(old, old, new) == [("vanilla_added", "mid", "icon", 1)]


def test_named_blocks_pair_by_name():
    old = 'w = { widget = { name = "x" s = 1 } widget = { name = "y" s = 1 } }'
    new = 'w = { widget = { name = "y" s = 2 } }'
    assert _changes(old, old, new) == [
        ("vanilla_changed", "mid", "s", 1), ("vanilla_removed", "mid", 'widget name="x"', 1)]


def test_repeated_keys_and_list_members_ignore_order():
    assert _changes("l = { religion = a religion = b }", "l = { religion = b religion = a }") == []


def test_block_head_change():
    assert _changes("type t = widget { a = 1 }",
                    "type t = widget { a = 1 }", "type t = container { a = 1 }") == [
        ("vanilla_changed", "mid", "type t", 1)]


def test_key_renamed_with_the_same_distinctive_value_pairs():
    [c] = compare('b = { onpressed = "[OnPause]" }',
                  ['b = { onpressed = "[OnPause]" }', 'b = { on_action = "[OnPause]" }'])
    assert (c.kind, c.mod.label, c.new.label, c.since) == ("vanilla_changed", "onpressed", "on_action", 1)


def test_bare_value_run_is_one_statement():
    mod = "c = { color = { 0.0 0.0\n0.0 1.0 } }"
    assert _changes(mod, "c = { color = { 0 0 0 1 } }", "c = { color = { 0 0 0 0.5 } }") == [
        ("vanilla_changed", "mid", "@items", 1)]


def test_keyless_blocks_keep_their_contents():
    old, new = "c = { { 1 2 } { 3 4 } }", "c = { { 1 2 } { 3 5 } }"
    assert _changes(old, old, new) == [("vanilla_changed", "mid", "@items", 1)]
    text = "c = { { 1 2 } }"
    assert parse(tokenize(text), text)[0]["val"] == []          # a formatter's view is unchanged


def test_replace_block_compares_contents():
    assert _changes("REPLACE:a = { cost = 100 }", "a = { cost = 100 }", "a = { cost = 200 }",
                    unwrap=True) == [("vanilla_changed", "mid", "cost", 1)]


def test_vanilla_only_statement_knows_where_it_belongs():
    [c] = compare("w = { a = 1 b = 2 }", ["w = { a = 1 b = 2 }", "w = { a = 1 n = 0 b = 2 }"])
    assert (c.kind, c.parent.label, c.after.label) == ("vanilla_added", "w", "a")


def test_offsets_point_into_each_text():
    mod, new = "w = {\n  a = 1\n}", "w = { a = 2 }"
    [c] = compare(mod, [mod, new])
    assert mod[c.mod.start:c.mod.end] == "a = 1" and new[c.new.start:c.new.end] == "a = 2"


def test_your_change_to_a_value_vanilla_changed_before_your_copy_is_yours():
    versions = ("w = { a = 1 n = 1 }", "w = { a = 2 n = 2 }", "w = { a = 2 n = 2 }")
    assert _changes("w = { a = 9 n = 2 }", *versions) == [("mod_changed", "info", "a", None)]


def test_baseline_is_the_closest_version_and_the_oldest_among_equals():
    assert baseline("w = { a = 2 }", ["w = { a = 1 }", "w = { a = 2 }", "w = { a = 3 }"]) == 1
    assert baseline("w = { a = 9 }", ["w = { a = 1 }", "w = { a = 2 }"]) == 0
    assert baseline("w = { a = 1 }", [None, None]) is None


def test_block_only_you_have_still_shows_vanilla_text_vanilla_deleted_inside_it():
    # The copy wrapped vanilla's statement in a block of its own, and vanilla has
    # since deleted the statement: it is still vanilla's deleted text, one level in.
    versions = ("w = { t = 1 u = 2 }", "w = { u = 2 }")
    assert _changes("w = { if = { t = 1 } u = 2 }", *versions) == [
        ("mod_added", "info", "if", None), ("vanilla_removed", "high", "t", 1)]


def test_a_block_vanilla_deleted_that_you_had_edited_is_one_finding():
    # Not a block only the copy has: it is vanilla's block, edited by the copy, that
    # vanilla deleted. Reported once against the block, not as a new block of yours
    # that happens to carry deleted text.
    versions = ("w = { x = { s = { t = 1 } } x = { s = { q = 1 } } }",
                "w = { x = { } x = { s = { q = 1 } s = { r = 1 } } }")
    mod = "w = { x = { s = { t = 1 z = 9 } } x = { s = { q = 1 } s = { r = 1 } } }"
    assert _changes(mod, *versions) == [("both_changed", "mid", "s", 1)]


def test_block_vanilla_deleted_that_you_changed_is_yours_when_your_copy_came_after():
    # The copy took every change of version 1, so `u` is its own block and not one that
    # vanilla removed. `p` arrived in that same version, which the copy matches, so the
    # copy left it out on purpose (test_a_statement_vanilla_added_at_your_version_is_yours).
    versions = ("w = { a = 1 u = { k = 1 } }", "w = { a = 2 b = 3 c = 4 d = 5 p = { k = 1 } }")
    mod = "w = { a = 2 b = 3 c = 4 d = 5 u = { k = 5 } }"
    assert baseline(mod, list(versions)) == 1
    assert _changes(mod, *versions) == [
        ("mod_added", "info", "u", None), ("mod_removed", "info", "p", None)]


def test_a_block_vanilla_moved_to_another_key_is_one_change():
    # Vanilla renamed the key and kept the text, so it is one rename and not an addition
    # and a removal. Your copy still sets the old key.
    versions = ("w = { old = { v = 1 t = 2 } }", "w = { new = { v = 1 t = 2 } }")
    mod = "w = { old = { v = 1 t = 2 } }"
    # The change keeps your statement, so _changes names it by the key you still set.
    assert _changes(mod, *versions) == [("vanilla_renamed", "mid", "old", 1)]
    c, = compare(mod, list(versions))
    assert (c.mod.label, c.new.label) == ("old", "new")


def test_a_statement_vanilla_moved_into_a_new_block_is_one_change():
    # Vanilla put the statement inside a block it now holds beside it. The statement is
    # gone from this level, but vanilla still carries it, so it is a move and not a
    # deletion. The added block is not reported on its own; the move names it.
    old = 'w = { onclick = "[DoThing]" b = 1 }'
    new = 'w = { tip = { title = "T" on_action = "[DoThing]" } b = 1 }'
    assert _changes(old, old, new) == [("vanilla_moved", "mid", "onclick", 1)]


def test_a_statement_vanilla_really_deleted_is_still_a_removal():
    old = 'w = { onclick = "[DoThing]" b = 1 }'
    new = 'w = { tip = { title = "T" } b = 1 }'
    assert ("vanilla_removed", "mid", "onclick", 1) in _changes(old, old, new)


def test_a_plain_value_inside_a_new_block_is_not_a_move():
    # `flag = 1` says nothing distinctive, so its turning up in a new block is no reason
    # to call it the same statement.
    old, new = "w = { flag = 1 b = 1 }", "w = { tip = { flag = 1 } b = 1 }"
    kinds = {k for k, _p, _l, _s in _changes(old, old, new)}
    assert "vanilla_moved" not in kinds and "vanilla_removed" in kinds


def test_two_short_statements_that_read_alike_are_not_a_rename():
    # `old = yes` and `new = yes` read alike too often for one to be the other renamed.
    versions = ("w = { a = 1 old = yes }", "w = { a = 1 new = yes }")
    kinds = {k for k, _p, _l, _s in _changes("w = { a = 1 old = yes }", *versions)}
    assert kinds == {"vanilla_added", "vanilla_removed"}


def test_a_statement_vanilla_added_at_your_version_is_yours():
    # Vanilla restructured the block in version 1 and added `n` in the same version. The
    # copy has the new structure, so whoever wrote it saw `n` and left it out.
    versions = ("w = { old = 1 }", "w = { a = 2 b = 3 n = 4 }")
    mod = "w = { a = 2 b = 3 }"
    assert baseline(mod, list(versions)) == 1
    assert _changes(mod, *versions) == [("mod_removed", "info", "n", None)]


def test_a_statement_vanilla_added_after_your_version_is_still_reported():
    # The copy matches version 1 and vanilla added `n` in version 2, so it is a change
    # to take. A copy that only lacks a new statement matches the version before it.
    versions = ("w = { a = 2 b = 3 }", "w = { a = 2 b = 3 }", "w = { a = 2 b = 3 n = 4 }")
    mod = "w = { a = 2 b = 3 }"
    assert baseline(mod, list(versions)) == 0
    assert _changes(mod, *versions) == [("vanilla_added", "mid", "n", 2)]


def test_block_vanilla_deleted_after_your_copy_that_you_changed_is_both_changed():
    versions = ("w = { a = 1 u = { k = 1 } }", "w = { a = 1 u = { k = 1 } }", "w = { a = 1 }")
    assert _changes("w = { a = 1 u = { k = 5 } }", *versions) == [("both_changed", "mid", "u", 2)]


def test_block_vanilla_named_still_pairs_by_key():
    old, new = "w = { box = { t = 1 } }", 'w = { box = { name = "b" t = 2 } }'
    assert _changes(old, old, new) == [
        ("vanilla_added", "mid", "name", 1), ("vanilla_changed", "mid", "t", 1)]


def test_align_pairs_in_order_then_moved_statements_by_label():
    a, b = nodes("x = 1 y = 2 z = 3"), nodes("x = 1 z = 4 y = 2")
    assert align(a, b) == [(0, 0), (1, 2), (2, 1)]


def test_moved_block_pairs_and_compares_its_contents():
    old = "p = { a = { k = 1 } b = { k = 1 } }"
    mod = "p = { b = { k = 1 } a = { k = 1 } }"
    assert _changes(mod, old, "p = { a = { k = 1 } b = { k = 1 j = 2 } }") == [
        ("vanilla_added", "mid", "j", 1)]


def test_another_copy_of_an_existing_statement_is_an_addition():
    assert _changes("w = { t = a }", "w = { t = a }", "w = { t = a t = a }") == [
        ("vanilla_added", "mid", "t", 1)]


# --- repeated same-key siblings ----------------------------------------------------
# A script block's children are an unordered set: two `trigger_if` blocks are told
# apart by their selector, never by where they sit. Vanilla inserting one must not
# re-date or re-pair the others. (In GUI, order is identity; see test_gui_audit.)


def _sections(*goods, extra=""):
    """A block holding one selector-carrying section per good, in the order given."""
    body = "".join(
        "\ttrigger_if = {\n"
        f"\t\tlimit = {{ good = {g} }}\n"
        f"\t\tOR = {{ climate = arid climate = tropical }}{extra if g == 'wool' else ''}\n"
        "\t}\n" for g in goods)
    return "act = {\n" + body + "}\n"


def test_a_sibling_vanilla_inserted_does_not_re_date_the_others():
    old = _sections("cotton", "horses", "livestock")
    new = _sections("cotton", "horses", "wool", "livestock")     # inserted in the middle
    mod = _sections("cotton", "horses", "livestock")             # the copy predates it
    # The copy lacks only vanilla's new section; nothing inside the sections it has
    # changed, so no statement of theirs may be reported at all.
    reported = [(c.kind, c.path, c.since) for c in compare(mod, [old, new])
                if c.kind.startswith("vanilla")]
    assert [r for r in reported if "OR" in r[1]] == []


def test_a_sibling_vanilla_inserted_is_reported_once_as_added():
    old = _sections("cotton", "livestock")
    new = _sections("cotton", "wool", "livestock")
    added = [c for c in compare(_sections("cotton", "livestock"), [old, new])
             if c.kind == "vanilla_added"]
    assert len(added) == 1 and added[0].new.key == "trigger_if" and added[0].since == 1


def test_a_change_inside_a_shifted_sibling_is_still_reported():
    # Vanilla inserts a section AND changes a later one: the change must survive the shift.
    old = _sections("cotton", "livestock")
    new = (_sections("cotton", "wool", "livestock")
           .replace("limit = { good = livestock }\n\t\tOR = { climate = arid climate = tropical }",
                    "limit = { good = livestock }\n\t\tOR = { climate = arid climate = oceanic }"))
    kinds = [(c.kind, c.new.label if c.new else c.mod.label)
             for c in compare(_sections("cotton", "livestock"), [old, new])
             if c.kind.startswith("vanilla")]
    # vanilla swapped tropical for oceanic in the section the insertion shifted
    assert ("vanilla_changed", "climate") in kinds
    assert ("vanilla_added", "trigger_if") in kinds     # and the section it inserted
    assert len(kinds) == 2


def test_the_copys_own_substitution_inside_one_sibling_is_its_own_edit():
    # The mod translated vanilla's climate checks into its own triggers, and vanilla
    # has not touched that section since the copy was made.
    old = new = _sections("cotton", "livestock")
    mod = _sections("cotton", "livestock").replace(
        "OR = { climate = arid climate = tropical }",
        "OR = { arid_climate_trigger = yes tropical_climate_trigger = yes }")
    assert [c.kind for c in compare(mod, [old, new]) if c.kind.startswith("vanilla")] == []


def test_a_substitution_is_not_re_dated_by_a_sibling_vanilla_inserted():
    # MEIOU's shape: the copy translated vanilla's climate checks into its own
    # triggers, and vanilla later inserted another section carrying the same checks.
    # The inserted section raises vanilla's count of every climate statement, which
    # must not date the copy's untouched section to that patch.
    old = _sections("cotton", "livestock")
    new = _sections("cotton", "wool", "livestock")
    mod = _sections("cotton", "livestock").replace(
        "OR = { climate = arid climate = tropical }",
        "OR = { arid_climate_trigger = yes tropical_climate_trigger = yes }")
    inside = [(c.kind, c.new.label, c.since) for c in compare(mod, [old, new])
              if c.kind.startswith("vanilla") and c.new is not None and c.new.key == "climate"]
    assert inside == []


def test_a_change_vanilla_made_inside_a_selector_is_still_reported():
    # The selector identifies its block, so it cannot identify a change to itself:
    # `if` blocks are told apart by their `limit`, and vanilla edited that `limit`.
    old = ("e = { opinion = { if = { limit = { NOT = { culture = root } } o = 1 }\n"
           "                  if = { limit = { is_ruler = yes } o = 2 } } }")
    new = ("e = { opinion = { if = { limit = { NOT = { culture = prev } } o = 1 }\n"
           "                  if = { limit = { is_ruler = yes } o = 2 } } }")
    mod = old
    kinds = [(c.kind, c.mod.label if c.mod else c.new.label)
             for c in compare(mod, [old, new]) if c.kind.startswith("vanilla")]
    assert ("vanilla_removed", "culture") in kinds or ("vanilla_changed", "culture") in kinds


def test_a_statement_vanilla_moved_out_and_back_is_not_an_addition_you_missed():
    # Vanilla wrapped a trigger in a block for two versions and then unwrapped it, so
    # following it back stops at the unwrapping. The copy's baseline held it at that
    # place all along, so the copy dropping it is the copy's own edit, not a change to
    # take. (MEIOU's decline_of_empire can_end.)
    versions = ["d = { can_end = { end_trigger = yes } }",                    # baseline
                "d = { can_end = { end_reason = { trigger = end_trigger } } }",
                "d = { can_end = { end_trigger = yes } }"]
    mod = "d = { can_end = { OR = { stability > 0 } } }"
    kinds = [(c.kind, c.priority) for c in compare(mod, versions)
             if (c.new or c.mod).label == "end_trigger"]
    assert kinds == [("mod_removed", "info")]

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


def test_both_changed_is_high():
    assert _changes("w = { a = 7 }", "w = { a = 1 }", "w = { a = 5 }") == [
        ("both_changed", "high", "a", 1)]


def test_vanilla_deleting_a_statement_you_changed_is_both_changed():
    assert _changes("w = { a = 1 b = 7 }", "w = { a = 1 b = 1 }", "w = { a = 1 }") == [
        ("both_changed", "high", "b", 1)]


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


def test_vanilla_changing_a_statement_you_removed_is_high():
    assert _changes("w = { a = 1 }", "w = { a = 1 b = 2 }", "w = { a = 1 b = 3 }") == [
        ("removed_changed", "high", "b", 1)]


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
    versions = ("w = { x = { s = { t = 1 } } x = { s = { q = 1 } } }",
                "w = { x = { } x = { s = { q = 1 } s = { r = 1 } } }")
    mod = "w = { x = { s = { t = 1 z = 9 } } x = { s = { q = 1 } s = { r = 1 } } }"
    assert _changes(mod, *versions) == [
        ("mod_added", "info", "s", None), ("vanilla_removed", "high", "t", 1)]


def test_key_vanilla_no_longer_uses_is_flagged_even_at_the_baseline():
    versions = ("w = { a = 1 u = { k = 1 } }", "w = { a = 2 b = 3 c = 4 d = 5 p = { k = 1 } }")
    mod = "w = { a = 2 b = 3 c = 4 d = 5 u = { k = 5 } }"
    assert baseline(mod, list(versions)) == 1
    assert _changes(mod, *versions) == [
        ("both_changed", "high", "u", 1), ("vanilla_added", "mid", "p", 1)]


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

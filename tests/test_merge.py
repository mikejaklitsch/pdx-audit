"""The node-level three-way merge: each row of the decision table, insertion anchors,
order, adjacent edits, double insertions, GUI and the removed-line check."""
from pdxaudit import diff3
from pdxaudit.merge import KEEP, OPEN, TAKE, Op, merge_texts, removed_lines, template_keys

BASE = ("a = {\n\tcost = 1\n"
        "\tif = { limit = { x = yes } add = 1 }\n"
        "\tif = { limit = { y = yes } add = 2 }\n"
        "\tkeep = 5\n\tgone = 1\n}\n")
OURS = ("REPLACE:a = {\n\tcost = 1 # mine\n"
        "\tif = { limit = { x = yes } add = 10 }\n"
        "\tif = { limit = { y = yes } add = 2 }\n"
        "\tkeep = 6\n\tgone = 1\n\tmy_add = yes\n}\n")
THEIRS = ("a = {\n\tcost = 2\n"
          "\tif = { limit = { x = yes } add = 1 }\n"
          "\tif = { limit = { y = yes } add = 3 }\n"
          "\tnew_one = 1\n\tkeep = 7\n}\n")


def _decisions(r):
    return [(d.kind, d.action, [s["key"] for s in d.path]) for d in r.decisions]


def test_decision_table_and_order():
    r = merge_texts(BASE, OURS, THEIRS, unwrap=True)
    assert r.text == ("REPLACE:a = {\n\tcost = 2 # mine\n"
                      "\tif = { limit = { x = yes } add = 10 }\n"
                      "\tif = { limit = { y = yes } add = 3 }\n"
                      "\tnew_one = 1\n\tkeep = 6\n\tmy_add = yes\n}\n")
    assert _decisions(r) == [("vanilla_changed", TAKE, ["cost"]), ("vanilla_changed", TAKE, ["if", "add"]),
                             ("both_changed", OPEN, ["keep"]), ("vanilla_removed", TAKE, ["gone"]),
                             ("vanilla_added", TAKE, ["new_one"])]
    assert r.check_passed


def test_keep_mod_holds_an_old_vanilla_value():
    def decide(path, kind, o, t, tt, what):
        if path[-1]["key"] == "cost":
            return KEEP, "entry:e1"
        return (OPEN, None) if what == "conflict" else (TAKE, None)
    r = merge_texts(BASE, OURS, THEIRS, unwrap=True, decide=decide)
    assert "cost = 1 # mine" in r.text
    assert ("vanilla_changed", KEEP, ["cost"]) in _decisions(r)


def test_adjacent_edits_stay_separate():
    base = "a = {\n\tx = 1\n\ty = 1\n}\n"
    ours = "a = {\n\tx = 2\n\ty = 1\n}\n"
    theirs = "a = {\n\tx = 1\n\ty = 3\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == "a = {\n\tx = 2\n\ty = 3\n}\n" and not r.open


def test_the_same_insertion_on_both_sides_is_kept_once():
    base = "a = {\n\tx = 1\n}\n"
    ours = "a = {\n\tnew = 1\n\tx = 1\n}\n"
    theirs = "a = {\n\tx = 1\n\tnew = 1\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text.count("new = 1") == 1


def test_repeated_keys_keep_their_place():
    base = "e = {\n\tset_variable = a\n\tif = { limit = { p = yes } x = 1 }\n\tset_variable = b\n}\n"
    ours = "e = {\n\tset_variable = a\n\tmine = yes\n\tif = { limit = { p = yes } x = 1 }\n\tset_variable = b\n}\n"
    theirs = "e = {\n\tset_variable = a\n\tif = { limit = { p = yes } x = 2 }\n\tset_variable = b\n\tset_variable = c\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == ("e = {\n\tset_variable = a\n\tmine = yes\n\tif = { limit = { p = yes } x = 2 }\n"
                      "\tset_variable = b\n\tset_variable = c\n}\n")


def test_insertion_into_a_nested_block_and_reindent():
    base = "a = {\n\tb = {\n\t\tx = 1\n\t}\n}\n"
    ours = "a = {\n    b = {\n        x = 1\n        mine = 1\n    }\n}\n"
    theirs = "a = {\n\tb = {\n\t\tx = 1\n\t\tc = {\n\t\t\td = 1\n\t\t}\n\t}\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == ("a = {\n    b = {\n        x = 1\n        c = {\n        \td = 1\n        }\n"
                      "        mine = 1\n    }\n}\n")


def test_gui_file_with_named_children():
    base = 'window = {\n\tname = "w"\n\tbutton = {\n\t\tname = "b"\n\t\tsize = { 1 1 }\n\t}\n}\n'
    ours = ('window = {\n\tname = "w"\n\tbutton = {\n\t\tname = "b"\n\t\tsize = { 1 1 }\n'
            '\t\tusing = mine\n\t}\n}\n')
    theirs = ('window = {\n\tname = "w"\n\tbutton = {\n\t\tname = "b"\n\t\tsize = { 2 2 }\n\t}\n'
              '\ttext = {\n\t\tname = "t"\n\t}\n}\n')
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI)
    assert "size = { 2 2 }" in r.text and "using = mine" in r.text and 'name = "t"' in r.text
    assert r.check_passed


def test_keyword_case_is_not_a_change():
    base = "a = {\n\tnot = { x = 1 }\n}\n"
    ours = "a = {\n\tNOT = { x = 1 }\n}\n"
    theirs = "a = {\n\tnot = { x = 1 }\n\ty = 1\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == "a = {\n\tNOT = { x = 1 }\n\ty = 1\n}\n"


def test_removed_line_check_flags_an_unexplained_removal():
    ours = "a = {\n\tx = 1\n\ty = 1\n}\n"
    merged = "a = {\n\tx = 2\n}\n"
    op = Op(ours.index("x = 1"), ours.index("x = 1") + 5, "x = 2", "vanilla changed")
    assert removed_lines(ours, merged, [op]) == [(3, "\ty = 1")]


def test_comment_lines_above_a_node_move_with_it():
    base = "a = {\n\t# one\n\tx = 1\n\t# two\n\ty = 2\n}\n"
    ours = "a = {\n\t# one\n\tx = 1\n\t# two\n\ty = 2\n\tmine = 1\n}\n"
    theirs = "a = {\n\t# two\n\ty = 2\n\t# three\n\tz = 3\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == "a = {\n\t# two\n\ty = 2\n\t# three\n\tz = 3\n\tmine = 1\n}\n"
    assert r.check_passed


def test_a_new_sibling_of_the_same_key_does_not_take_a_changed_partner():
    base = ('v = {\n\t# trade\n\tcard = {\n\t\ttext = "trade"\n\t\tsize = 1\n\t}\n'
            '\t# food\n\tcard = {\n\t\ttext = "food"\n\t\tsize = 1\n\t\tshow = yes\n\t}\n}\n')
    ours = base.replace("show = yes", "show = no")
    theirs = ('v = {\n\t# trade\n\tcard = {\n\t\ttext = "trade"\n\t\tsize = 1\n\t}\n'
              '\t# tariff\n\tcard = {\n\t\ttext = "tariff"\n\t\tsize = 2\n\t}\n'
              '\t# food\n\tcard = {\n\t\ttext = "food"\n\t\tsize = 2\n\t\tshow = yes\n\t}\n}\n')
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI)
    assert r.text == ('v = {\n\t# trade\n\tcard = {\n\t\ttext = "trade"\n\t\tsize = 1\n\t}\n'
                      '\t# tariff\n\tcard = {\n\t\ttext = "tariff"\n\t\tsize = 2\n\t}\n'
                      '\t# food\n\tcard = {\n\t\ttext = "food"\n\t\tsize = 2\n\t\tshow = no\n\t}\n}\n')


def test_ours_counterpart_is_judged_against_base_and_theirs():
    """SUL took vanilla's new texture early and added a copy with the old one. The
    base icon pairs with the first, so vanilla's change does not land on the copy."""
    base = 'w = {\n\ticon = {\n\t\tvisible = "[A]"\n\t\ttexture = "old.dds"\n\t\tsize = 1\n\t}\n}\n'
    ours = ('w = {\n\ticon = {\n\t\tvisible = "[A_mod]"\n\t\ttexture = "new.dds"\n\t\tsize = 1\n\t}\n'
            '\ticon = {\n\t\tvisible = "[B_mod]"\n\t\ttexture = "old.dds"\n\t\tsize = 1\n\t}\n}\n')
    theirs = 'w = {\n\ticon = {\n\t\tvisible = "[A]"\n\t\ttexture = "new.dds"\n\t\tsize = 1\n\t}\n}\n'
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI)
    assert r.text == ours


def test_an_insertion_inside_a_one_line_block_is_explained():
    base = "a = {\n\tenabled = { x = 1 }\n}\n"
    ours = "a = {\n\tenabled = { x = 2 }\n}\n"
    theirs = "a = {\n\tenabled = { x = 1 y = 1 }\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == "a = {\n\tenabled = { x = 2 y = 1 }\n}\n" and r.check_passed


def test_take_vanilla_puts_back_a_node_ours_dropped():
    base = "a = {\n\tx = 1\n\tcheck = { p = 1 }\n\tz = 1\n}\n"
    ours = "a = {\n\tx = 1\n\tz = 1\n}\n"
    theirs = "a = {\n\tx = 1\n\tcheck = { p = 2 }\n\tz = 1\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == ours and r.open                       # a conflict waits for the user
    take = lambda path, kind, o, t, tt, what: (TAKE, "entry:e1")   # noqa: E731
    r = merge_texts(base, ours, theirs, unwrap=True, decide=take)
    assert r.text == theirs and not r.open


def test_moved_statements_still_pair():
    """SUL moved two checks; vanilla changed one and renamed the other."""
    base = "a = {\n\tbal = 5\n\tloans = 1\n\tio = { r = 1 }\n\tp = 1\n}\n"
    ours = "a = {\n\tio = { r = 1 }\n\tloans = 1\n\tp = 1\n\tbal = 5\n}\n"
    theirs = "a = {\n\tlast_bal = 5\n\tloans = 1\n\tio = { r = 1 v = 2 }\n\tp = 1\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == "a = {\n\tio = { r = 1 v = 2 }\n\tlast_bal = 5\n\tloans = 1\n\tp = 1\n}\n"
    assert r.check_passed and not r.open


def test_a_mod_node_is_reported_absent_only_when_it_is_absent():
    """port_merge once printed "mod: (absent)" for statements the mod block held. The
    merge names ours text for every decision where ours has the node, and leaves out a
    node that ours already holds in another place."""
    base = "b = {\n\tlocation_potential = { x = 1 }\n\tcost = 1\n}\n"
    ours = "b = {\n\tcost = 5\n\tunique_production_methods = { pm = 1 }\n\tlocation_potential = { x = 1 }\n}\n"
    theirs = ("b = {\n\tunique_production_methods = { pm = 1 }\n\tlocation_potential = { x = 2 }\n"
              "\tcost = 2\n}\n")
    r = merge_texts(base, ours, theirs, unwrap=True)
    for d in r.decisions:
        if d.kind in ("vanilla_changed", "vanilla_removed", "both_changed"):
            assert d.ours is not None, d
    assert not any(d.kind == "vanilla_added" and d.path[-1]["key"] == "unique_production_methods"
                   for d in r.decisions)
    assert "location_potential = { x = 2 }" in r.text and r.text.count("unique_production_methods") == 1


def test_inject_children_that_vanilla_changed_are_decisions():
    from pdxaudit.merge import merge_inject
    base = "law = {\n\tcost = 1\n\tupkeep = 1\n}\n"
    theirs = "law = {\n\tcost = 2\n\tupkeep = 1\n\tnew = 1\n}\n"
    ours = "INJECT:law = {\n\tcost = 5\n\tupkeep = 3\n}\n"
    r = merge_inject(base, ours, theirs)
    assert [(d.kind, d.action, d.path[-1]["key"]) for d in r.decisions] == [("inject_overlap", OPEN, "cost")]
    assert r.text == ours
    take = lambda path, kind, o, t, tt, what: (TAKE, "entry:e1")   # noqa: E731
    r = merge_inject(base, ours, theirs, take)
    assert r.text == "INJECT:law = {\n\tupkeep = 3\n}\n" and r.check_passed


def test_a_multi_line_insertion_inside_a_line_is_not_a_removal():
    base = "a = {\n\tt = { x = 1 o = 1 }\n\tif = {\n\t\tk = 1\n\t}\n}\n"
    ours = "a = {\n\tt = { x = 1 }\n\tif = {\n\t\tk = 1\n\t}\n}\n"
    theirs = "a = {\n\tt = { x = 1 o = 1 if = {\n\t\tlimit = { y = 1 }\n\t} }\n\tif = {\n\t\tk = 1\n\t}\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.check_passed and r.text.count("if = {") == 2


def test_a_one_line_block_that_gets_a_multi_line_child_is_laid_out_on_more_lines():
    """The merge lays out the one-line block, and its one-line parent, with one child
    per line. The blocks around it keep their layout."""
    base = "a = {\n\tx = { y = { a = 1 } z = 1 }\n\tw = { k = 1 }\n}\n"
    ours = "a = {\n\tx = { y = { a = 1 } z = 2 }\n\tw = { k = 1 }\n}\n"
    theirs = ("a = {\n\tx = {\n\t\ty = {\n\t\t\ta = 1\n\t\t\tb = {\n\t\t\t\tc = 1\n\t\t\t}\n\t\t}\n"
              "\t\tz = 1\n\t}\n\tw = { k = 1 m = 1 }\n}\n")
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == ("a = {\n\tx = {\n\t\ty = {\n\t\t\ta = 1\n\t\t\tb = {\n\t\t\t\tc = 1\n\t\t\t}\n\t\t}\n"
                      "\t\tz = 2\n\t}\n\tw = { k = 1 m = 1 }\n}\n")
    assert r.check_passed and not r.open


def test_a_multi_line_child_in_place_of_a_statement_lays_out_its_block():
    base = "a = {\n\tx = { a = 1 b = 1 }\n}\n"
    ours = "a = {\n\tx = { a = 1 b = 2 }\n}\n"
    theirs = "a = {\n\tx = {\n\t\ta = {\n\t\t\tc = 1\n\t\t}\n\t\tb = 1\n\t}\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == "a = {\n\tx = {\n\t\ta = {\n\t\t\tc = 1\n\t\t}\n\t\tb = 2\n\t}\n}\n"
    assert r.check_passed


def test_an_empty_base_takes_vanilla_only_nodes_and_opens_differing_twins():
    """Vanilla held no version at --old, so the base is empty. A node both sides hold
    alike stays once, a node only vanilla holds goes in, a node only the mod holds
    stays, and a node both added in different forms is a both_added decision."""
    ours = "a = { x = 1 }\nb = { y = 1 }\nmine = { z = 1 }\n"
    theirs = "a = { x = 1 }\nb = { y = 2 }\nc = { w = 1 }\n"
    r = merge_texts("", ours, theirs)
    assert r.text == "a = { x = 1 }\nb = { y = 1 }\nc = { w = 1 }\nmine = { z = 1 }\n"
    assert [(d.kind, d.action, d.path[-1]["key"]) for d in r.decisions] == [
        ("both_added", OPEN, "b"), ("vanilla_added", TAKE, "c")]
    assert r.check_passed
    take = lambda path, kind, o, t, tt, what: (TAKE, "rule:r")   # noqa: E731
    assert merge_texts("", ours, theirs, decide=take).text == \
        "a = { x = 1 }\nb = { y = 2 }\nc = { w = 1 }\nmine = { z = 1 }\n"


def test_a_key_both_sides_added_is_written_once():
    """SUL added `rate = 9999 # [FU]`; vanilla added `rate = 4` at the same level. The
    merge does not write both: it is a both_added decision. Two new blocks that
    differ by selector are not twins: both stay."""
    base = "w = {\n\tname = \"m\"\n}\n"
    ours = "w = {\n\tname = \"m\"\n\trate = 9999 # [FU]\n\tif = { limit = { a = 1 } }\n}\n"
    theirs = "w = {\n\tname = \"m\"\n\trate = 4\n\tif = { limit = { b = 1 } }\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text.count("rate =") == 1 and "if = { limit = { b = 1 } }" in r.text
    assert [(d.kind, d.action) for d in r.decisions if d.path[-1]["key"] == "rate"] == [("both_added", OPEN)]


def test_a_comment_that_replaces_a_removed_statement_takes_its_place():
    """Vanilla 1.4 messagetypes.txt turns `sound=yes` into `#sound=yes - TODO in
    ud010`. The merge removes the statement and writes vanilla's comment there."""
    base = "X={\nlog=no\npausepopup=no\nsound=yes\nmessage_category = government\n}\n"
    theirs = "X={\nlog=no\npausepopup=no\n#sound=yes - TODO in ud010\nmessage_category = government\n}\n"
    ours = "X = {\n\tlog = yes\n\tpausepopup = no\n\tsound = yes\n\tmessage_category = government\n}\n"
    r = merge_texts(base, ours, theirs)
    assert r.text == ("X = {\n\tlog = yes\n\tpausepopup = no\n\t#sound=yes - TODO in ud010\n"
                      "\tmessage_category = government\n}\n")
    assert r.check_passed


def test_comments_go_with_a_changed_or_inserted_vanilla_node():
    base = "a = {\n\t# about x\n\tx = 1 # old\n\ty = 1 # mine stays\n\t# keep me\n\tz = 1\n}\n"
    ours = "a = {\n\t# about x\n\tx = 1 # old\n\ty = 1 # my note\n\t# keep me\n\tz = 1\n\tm = 1\n}\n"
    theirs = ("a = {\n\t# about x, changed\n\tx = 2 # new\n\ty = 2 # vanilla note\n\t# keep me\n"
              "\t# why w\n\tw = 1 # w note\n}\n")
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == ("a = {\n\t# about x, changed\n\tx = 2 # new\n\ty = 2 # my note\n\t# keep me\n"
                      "\t# why w\n\tw = 1 # w note\n\tm = 1\n}\n")
    assert r.check_passed


def test_a_removal_leaves_no_two_empty_lines():
    """A line that holds only white space above a removed node is an empty line too.
    1.4 location_window.gui and map_markers.gui showed the case."""
    from pdxaudit.merge import splice
    base = "a = {\n\tx = 1\n\n\ty = 1\n\n\tz = 1\n}\n"
    ours = "a = {\n\tx = 1\n\t\t\n\ty = 1\n\n\tz = 1\n}\n"
    theirs = "a = {\n\tx = 1\n\n\tz = 1\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    merged, _spans = splice(ours, [(op.start, op.end, op.text) for op in r.ops])
    assert merged == "a = {\n\tx = 1\n\n\tz = 1\n}\n"
    # A run of empty lines that no edit touches stays as the mod wrote it.
    assert splice("a\n\n\nb\nc\n", [(6, 7, "C")])[0] == "a\n\n\nb\nC\n"
    # Empty lines an edit leaves at the end become one line end.
    assert splice("a\nb\n", [(2, 3, "\n\n\n")])[0] == "a\n"


def test_overlapping_edits_fail_the_check():
    """Two edits that cover one place cannot both be made. The merge makes the first
    and reports the second, so the removed-line check fails and --apply refuses."""
    from pdxaudit.merge import _splice_ops
    a, b, c = Op(0, 5, "x", "one"), Op(3, 8, "y", "two"), Op(8, 8, "z", "three", removes=False)
    dropped = []
    assert _splice_ops([b, a, c], dropped) == [a, c] and dropped == [b]


def test_a_node_the_mod_took_from_a_later_vanilla_version_follows_vanilla():
    """The copy matches 1.2 best, but SUL took two buttons that vanilla added in 1.3.
    Vanilla 1.4 removes one and changes the other. Both are vanilla changes, not the
    mod's nodes: the merge removes the first and takes the change of the second."""
    base = "card = {\n\tsize = 1\n}\n"
    v13 = "card = {\n\tsize = 1\n\tbutton = { name = \"a\" x = 1 }\n\tbutton = { name = \"b\" x = 1 }\n}\n"
    ours = "card = {\n\tsize = 5\n\tbutton = { name = \"a\" x = 1 }\n\tbutton = { name = \"b\" x = 1 }\n\tmine = 1\n}\n"
    theirs = "card = {\n\tsize = 1\n\tbutton = { name = \"b\" x = 2 }\n}\n"
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI, history=[v13])
    assert r.text == "card = {\n\tsize = 5\n\tbutton = { name = \"b\" x = 2 }\n\tmine = 1\n}\n"
    assert sorted((d.kind, d.action) for d in r.decisions) == [("vanilla_changed", TAKE), ("vanilla_removed", TAKE)]
    assert r.check_passed
    # Without the history the two buttons read as the mod's own and stay.
    assert "name = \"a\"" in merge_texts(base, ours, theirs, dialect=diff3.GUI).text


def test_vanilla_comment_changes_on_unchanged_nodes_are_taken():
    """1.4 map_markers.gui drops `# TODO: "unit_join"` after an unchanged cursor line,
    and 1.4 independence_movement.txt adds `#"Do we like the actor ..."` above an if
    whose inside changed. The mod left both comments as the base had them, so the
    merge takes vanilla's comments. A comment the mod changed stays."""
    base = ('a = {\n\tcursor = "unit_movement" # TODO: "unit_join"\n\tother = 1 # base note\n'
            '\tif = {\n\t\tlimit = { x = 1 }\n\t\tv = 1\n\t}\n}\n')
    ours = ('a = {\n\tcursor = "unit_movement" # TODO: "unit_join"\n\tother = 1 # my note\n'
            '\tif = {\n\t\tlimit = { x = 1 }\n\t\tv = 1\n\t}\n}\n')
    theirs = ('a = {\n\tcursor = "unit_movement"\n\tother = 1 # vanilla note\n'
              '\t#"Do we like the actor?"\n\tif = {\n\t\tlimit = { x = 1 }\n\t\tv = 2\n\t}\n}\n')
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == ('a = {\n\tcursor = "unit_movement"\n\tother = 1 # my note\n'
                      '\t#"Do we like the actor?"\n\tif = {\n\t\tlimit = { x = 1 }\n\t\tv = 2\n\t}\n}\n')
    assert r.check_passed


def test_a_vanilla_block_the_mod_moved_into_its_own_block_follows_vanilla():
    """1.4 location_card: SUL moved a button that vanilla added in 1.3 into its own
    block "location_card_extra_stats". Vanilla 1.4 removes the original, so the
    moved copy goes. A moved block that vanilla changed takes vanilla's change, and
    a moved block that vanilla still holds as it is stays."""
    base = "card = {\n\thbox = {\n\t\tsize = 1\n\t\tbutton_regular = { a = 1 b = 1 c = 1 }\n\t}\n}\n"
    v13 = ("card = {\n\thbox = {\n\t\tsize = 1\n\t\tbutton_regular = { a = 1 b = 1 c = 1 }\n"
           "\t\tbutton_regular = { d = 1 e = 1 f = 1 }\n\t\tkeep_me = { g = 1 h = 1 i = 1 }\n\t}\n}\n")
    ours = ("card = {\n\thbox = {\n\t\tsize = 1\n\t}\n\tblock \"extra_stats\" = {\n"
            "\t\tbutton_regular = { a = 1 b = 1 c = 1 }\n\t\tbutton_regular = { d = 1 e = 1 f = 1 }\n"
            "\t\tkeep_me = { g = 1 h = 1 i = 1 }\n\t\tmine = 1\n\t}\n}\n")
    theirs = ("card = {\n\thbox = {\n\t\tsize = 1\n\t\tbutton_regular = { a = 1 b = 1 c = 2 }\n"
              "\t\tkeep_me = { g = 1 h = 1 i = 1 }\n\t}\n}\n")
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI, history=[v13])
    assert r.text == ("card = {\n\thbox = {\n\t\tsize = 1\n\t}\n\tblock \"extra_stats\" = {\n"
                      "\t\tbutton_regular = { a = 1 b = 1 c = 2 }\n"
                      "\t\tkeep_me = { g = 1 h = 1 i = 1 }\n\t\tmine = 1\n\t}\n}\n")
    moved = [(d.kind, d.action) for d in r.decisions if "moved" in d.reason]
    assert sorted(moved) == [("vanilla_changed", TAKE), ("vanilla_removed", TAKE)]
    assert r.check_passed


def test_comments_of_a_node_inserted_into_a_one_line_block_stay():
    """1.4 bribe_vote.txt adds `monthly_income_total <= {...}` with four comment lines
    above it to `enabled`, which the mod holds on one line. The merge lays the block
    out on more lines and keeps the comments above the new node."""
    base = "a = {\n\tenabled = { x = 1 }\n}\n"
    ours = "a = {\n\tenabled = { x = 2 }\n}\n"
    theirs = "a = {\n\tenabled = {\n\t\tx = 1\n\t\t# why y\n\t\t# more\n\t\ty = 1 # note\n\t}\n}\n"
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text == "a = {\n\tenabled = {\n\t\tx = 2\n\t\t# why y\n\t\t# more\n\t\ty = 1 # note\n\t}\n}\n"
    assert r.check_passed


def test_a_new_vanilla_block_holding_a_block_the_mod_moved_is_open():
    """Vanilla 1.4 wraps a button in a new block; SUL had moved the same button into
    a block of its own. Taking vanilla's block would show the button two times, so
    the new block is an open decision."""
    base = "card = {\n\thbox = {\n\t\tsize = 1\n\t\tbutton_regular = { a = 1 b = 1 c = 1 }\n\t}\n}\n"
    ours = ("card = {\n\thbox = {\n\t\tsize = 1\n\t}\n\tblock \"mine\" = {\n"
            "\t\tbutton_regular = { a = 1 b = 1 c = 1 }\n\t}\n}\n")
    theirs = ("card = {\n\thbox = {\n\t\tsize = 1\n\t\tblock \"control\" = {\n"
              "\t\t\tbutton_regular = { a = 1 b = 1 c = 1 }\n\t\t}\n\t}\n}\n")
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI)
    assert r.text == ours
    assert [(d.kind, d.action) for d in r.decisions if "moved" in d.reason] == [("vanilla_added", OPEN)]


def test_a_change_vanilla_made_before_old_is_open_never_taken():
    """The mod copy matches 1.0, but --old is 1.1. Vanilla changed x, added z and
    removed gone in 1.1: an earlier port did not take these, so each is an open
    decision. Vanilla changed y and added w in 1.2: the merge takes these. A rule
    still decides an older change."""
    base = "a = {\n\tx = 1\n\ty = 1\n\tgone = 1\n}\n"
    old = "a = {\n\tx = 2\n\ty = 1\n\tz = 1\n}\n"
    new = "a = {\n\tx = 2\n\ty = 3\n\tz = 1\n\tw = 1\n}\n"
    ours = "REPLACE:a = {\n\tx = 1\n\ty = 1\n\tgone = 1\n}\n"
    r = merge_texts(base, ours, new, unwrap=True, old=(old, "1.1"))
    assert r.text == "REPLACE:a = {\n\tx = 1\n\ty = 3\n\tw = 1\n\tgone = 1\n}\n"
    got = {(d.kind, d.path[0]["key"]): d for d in r.decisions}
    assert {k: d.action for k, d in got.items()} == {
        ("vanilla_changed", "x"): OPEN, ("vanilla_changed", "y"): TAKE, ("vanilla_removed", "gone"): OPEN,
        ("vanilla_added", "z"): OPEN, ("vanilla_added", "w"): TAKE}
    assert all("before --old 1.1" in got[k].reason for k in
               [("vanilla_changed", "x"), ("vanilla_removed", "gone"), ("vanilla_added", "z")])
    assert r.check_passed

    rule = lambda path, kind, o, t, tt, what: (TAKE, "rule:r") if path[-1]["key"] == "x" else (  # noqa: E731
        (OPEN, None) if what == "conflict" else (TAKE, None))
    r = merge_texts(base, ours, new, unwrap=True, decide=rule, old=(old, "1.1"))
    assert "\tx = 2\n" in r.text


def test_without_an_older_base_every_vanilla_change_is_taken_as_before():
    base = "a = {\n\tx = 1\n}\n"
    r = merge_texts(base, "REPLACE:" + base, "a = {\n\tx = 2\n}\n", unwrap=True)
    assert [d.action for d in r.decisions] == [TAKE]


def test_a_change_inside_a_block_that_vanilla_readdressed_after_old_is_taken():
    """Vanilla changed the limit of the first `if` after --old, so its address at
    --new does not resolve at --old (two `if` blocks share the key). The merge finds
    the level at --old by the base address, and takes both changes of this window."""
    two = "a = {{\n\tif = {{ limit = {{ {0} = yes }} v = {1} }}\n\tif = {{ limit = {{ y = yes }} v = 1 }}\n}}\n"
    base = "a = {\n\tz = 1\n\tif = { limit = { x = yes } v = 1 }\n\tif = { limit = { y = yes } v = 1 }\n}\n"
    old = "a = {\n\tz = 2\n\tif = { limit = { x = yes } v = 1 }\n\tif = { limit = { y = yes } v = 1 }\n}\n"
    new = "a = {\n\tz = 2\n" + two.format("x2", 2).split("\n", 1)[1]
    r = merge_texts(base, "REPLACE:" + base, new, unwrap=True, old=(old, "1.1"))
    got = sorted((d.action, [s["key"] for s in d.path]) for d in r.decisions)
    assert [a for a, p in got if p != ["z"]] and all(a == TAKE for a, p in got if p != ["z"])
    assert [a for a, p in got if p == ["z"]] == [OPEN]
    assert "limit = { x2 = yes } v = 2" in r.text


def test_a_weight_list_pairs_its_entries_by_name_not_by_weight():
    """Vanilla split `10 = army_cavalry` into heavy and light entries. The mod had
    already written its own weights for both. Each entry pairs with vanilla's entry
    of the same name: the merge never writes a second weight for one category, and
    a weight both sides set differently is a decision."""
    base = "a = {\n\tleft = {\n\t\t10 = army_cavalry\n\t\tmax_frontage = 1.25\n\t}\n}\n"
    theirs = "a = {\n\tleft = {\n\t\t10 = army_heavy_cavalry\n\t\t10 = army_light_cavalry\n\t\tmax_frontage = 1.25\n\t}\n}\n"
    ours = ("REPLACE:a = {\n\tleft = {\n\t\t6 = army_heavy_cavalry\n\t\t6 = army_light_cavalry\n"
            "\t\t1 = army_artillery\n\t\tmax_frontage = 1.25\n\t}\n}\n")
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert r.text.count("army_light_cavalry") == 1 and r.text.count("army_heavy_cavalry") == 1
    assert "6 = army_light_cavalry" in r.text
    # Both sides removed army_cavalry, so it needs no decision.
    assert sorted((d.kind, d.path[-1]["key"]) for d in r.decisions) == [
        ("both_added", "army_heavy_cavalry"), ("both_added", "army_light_cavalry")]
    assert next(d for d in r.decisions if d.path[-1]["key"] == "army_light_cavalry").theirs == \
        "10 = army_light_cavalry"


def test_a_weight_changed_by_vanilla_alone_is_taken():
    base = "a = {\n\tl = {\n\t\t10 = army_heavy_cavalry\n\t\t5 = army_artillery\n\t}\n}\n"
    theirs = "a = {\n\tl = {\n\t\t10 = army_heavy_cavalry\n\t\t8 = army_artillery\n\t}\n}\n"
    r = merge_texts(base, "REPLACE:" + base, theirs, unwrap=True)
    assert "\t\t8 = army_artillery\n" in r.text and "5 = army_artillery" not in r.text


def test_a_value_the_mod_holds_from_before_the_base_is_flagged_even_when_vanilla_is_unchanged():
    """The copy matches 1.1 best, and 1.1 equals --new: vanilla changed nothing in the
    window. The mod's transport_capacity is vanilla's 1.0 value, so vanilla changed
    it before the base. That is an open decision, never the mod's own edit, and the
    merge writes nothing by itself."""
    v0 = "a = {\n\tcategory = t\n\ttransport_capacity = -0.15\n\tcrew = 1\n}\n"
    v1 = "a = {\n\tcategory = t\n\ttransport_capacity = 0.10\n\tcrew = 1\n}\n"
    ours = "REPLACE:a = {\n\tcategory = t\n\ttransport_capacity = -0.15\n\tcrew = 1\n\tmine = yes\n}\n"
    r = merge_texts(v1, ours, v1, unwrap=True, older=[v0], old=(v1, "1.1"))
    assert r.text == ours
    [d] = r.decisions
    assert (d.kind, d.action, d.path[-1]["key"], d.theirs) == (
        "vanilla_changed", OPEN, "transport_capacity", "transport_capacity = 0.1")
    assert "before --old 1.1" in d.reason


def test_a_renamed_line_inside_an_unchanged_block_is_flagged_both_ways():
    """Vanilla renamed a trigger inside location_potential before the base. The mod
    still holds the old name, next to a line of its own. The old line is a
    vanilla_removed decision and the new one a vanilla_added decision, both open;
    taking both gives vanilla's line and keeps the mod's own."""
    v0 = "a = {\n\tlocation_potential = {\n\t\tunit_iberian = yes\n\t}\n\tx = 1\n}\n"
    v1 = "a = {\n\tlocation_potential = {\n\t\tunit_catalan = yes\n\t}\n\tx = 1\n}\n"
    ours = ("REPLACE:a = {\n\tlocation_potential = {\n\t\tunit_iberian = yes\n\t\tmine = yes\n\t}\n"
            "\tx = 1\n}\n")
    r = merge_texts(v1, ours, v1, unwrap=True, older=[v0], old=(v1, "1.1"))
    got = sorted((d.kind, d.action, d.path[-1]["key"]) for d in r.decisions)
    assert got == [("vanilla_added", OPEN, "unit_catalan"), ("vanilla_removed", OPEN, "unit_iberian")]
    take = lambda path, kind, o, t, tt, what: (TAKE, "rule:r") if what is None else (OPEN, None)  # noqa: E731
    r = merge_texts(v1, ours, v1, unwrap=True, decide=take, older=[v0], old=(v1, "1.1"))
    assert "\t\tunit_catalan = yes\n" in r.text and "unit_iberian" not in r.text and "mine = yes" in r.text


def test_an_addition_vanilla_held_at_old_in_another_form_is_open():
    """Vanilla added create_enabled before --old and changed it after. The mod never
    had it. It is not a new 1.4 block, so the merge does not take it by itself."""
    base = "a = {\n\tyears = 15\n}\n"
    old = "a = {\n\tyears = 15\n\tcreate_enabled = { x = yes }\n}\n"
    new = "a = {\n\tyears = 15\n\tcreate_enabled = { x = yes y = yes }\n\tbrand_new = 1\n}\n"
    r = merge_texts(base, "REPLACE:" + base, new, unwrap=True, old=(old, "1.1"))
    got = {d.path[-1]["key"]: d.action for d in r.decisions}
    assert got == {"create_enabled": OPEN, "brand_new": TAKE}


def test_a_moved_copy_stays_open_when_vanilla_also_changed_the_block_around_it():
    """The mod holds a copy of vanilla's scope:target block inside a block of its own.
    Vanilla then moved is_neighbor_of out of that block, one level up. Taking
    vanilla's new scope:target alone would drop the condition, so it is open."""
    base = ("a = {\n\tcreate_enabled = {\n\t\tscope:target = {\n\t\t\tcountry_type = pop\n"
            "\t\t\tis_neighbor_of = root\n\t\t}\n\t}\n}\n")
    new = ("a = {\n\tcreate_enabled = {\n\t\tscope:target = {\n\t\t\tcountry_type = pop\n\t\t}\n"
           "\t\tis_neighbor_of = scope:target\n\t}\n}\n")
    ours = ("REPLACE:a = {\n\tcreate_visible = {\n\t\tmodifier:x = yes\n\t\tscope:target = {\n"
            "\t\t\tcountry_type = pop\n\t\t\tis_neighbor_of = root\n\t\t}\n\t}\n}\n")
    r = merge_texts(base, ours, new, unwrap=True)
    moved = [d for d in r.decisions if "moved this vanilla block" in d.reason]
    assert [(d.kind, d.action) for d in moved] == [("vanilla_changed", OPEN)]
    assert "is_neighbor_of = root" in r.text


TEMPLATE = template_keys('template bg_t {\n\ttexture = "v.dds"\n\ttexture_density = 2\n}\n')
BG_BASE = 'w = {\n\tbackground = {\n\t\ttexture = "v.dds"\n\t\ttexture_density = 2\n\t\tmargin = 1\n\t}\n}\n'
BG_THEIRS = "w = {\n\tbackground = {\n\t\tusing = bg_t\n\t\tmargin = 1\n\t}\n}\n"


def test_a_template_line_that_overrides_a_mod_value_is_open():
    """1.4 bg_circle_piechart: vanilla moved texture and texture_density into template
    bg_round_button_alt_texture. SUL draws its own texture, so the template line and
    the removal of texture_density are open, and the mod's text stays."""
    ours = BG_BASE.replace("v.dds", "mine.dds")
    r = merge_texts(BG_BASE, ours, BG_THEIRS, dialect=diff3.GUI, templates=TEMPLATE)
    assert r.text == ours
    got = sorted((d.kind, d.action, d.path[-1]["key"]) for d in r.decisions)
    assert got == [("both_changed", OPEN, "texture"), ("vanilla_added", OPEN, "using"),
                   ("vanilla_removed", OPEN, "texture_density")]
    assert all("bg_t" in d.reason for d in r.decisions if d.kind != "both_changed")


def test_a_template_line_with_no_mod_value_in_its_way_is_taken():
    """The mod left the texture as vanilla had it, so vanilla's move into the template
    is a plain vanilla change."""
    r = merge_texts(BG_BASE, BG_BASE, BG_THEIRS, dialect=diff3.GUI, templates=TEMPLATE)
    assert all(d.action == TAKE for d in r.decisions)
    assert "using = bg_t" in r.text and "texture" not in r.text
    assert r.check_passed


def test_a_copied_vanilla_block_is_open_when_vanilla_changes_the_original():
    """1.4 map_markers: SUL's overcrowding box holds a copy of the navy background of
    combat_side_marker, and the original stays at its place. Vanilla rewrote the
    original, so the copy is open; the merge never deletes it."""
    base = "m = {\n\tside = {\n\t\tbackground = { a = 1 b = 1 c = 1 }\n\t}\n}\n"
    ours = ("m = {\n\tside = {\n\t\tbackground = { using = t c = 1 }\n\t}\n\tblock \"mine\" = {\n"
            "\t\tbackground = { a = 1 b = 1 c = 1 }\n\t}\n}\n")
    theirs = "m = {\n\tside = {\n\t\tbackground = { using = t c = 1 }\n\t}\n}\n"
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI)
    assert r.text == ours
    copied = [(d.kind, d.action) for d in r.decisions if "copied" in d.reason]
    assert copied == [("vanilla_removed", OPEN)]


def test_a_moved_block_that_holds_vanilla_new_text_needs_no_decision():
    """1.4 marker_rank_icon: SUL moved the name flowcontainer into a type of its own
    and gave it vanilla's new snap_to_pixels. Vanilla's change is in the mod already."""
    base = ("w = {\n\tone = {\n\t\tflow = { x = 1 y = 1 z = 1 }\n\t}\n"
            "\ttwo = {\n\t\tsize = 1\n\t}\n}\n")
    ours = ("w = {\n\tone = {\n\t\tsize = 2\n\t}\n"
            "\ttwo = {\n\t\tsize = 1\n\t\tflow = { x = 1 y = 1 z = 2 }\n\t}\n}\n")
    theirs = ("w = {\n\tone = {\n\t\tflow = { x = 1 y = 1 z = 2 }\n\t}\n"
              "\ttwo = {\n\t\tsize = 1\n\t}\n}\n")
    r = merge_texts(base, ours, theirs, dialect=diff3.GUI)
    assert r.text == ours
    assert not r.open
    assert [(d.kind, d.action) for d in r.decisions] == [("removed_changed", KEEP)]


def test_a_block_of_the_mods_own_is_not_a_vanilla_block_that_the_merge_deleted():
    """Vanilla deletes the first widget. The first merge deletes it from ours too. In
    the merged text the mod's own widget is the only unpaired widget, but it shares
    almost nothing with the deleted one, so it is not that widget moved and changed:
    a merge of the merged text asks nothing."""
    base = ("a = {\n\twidget = {\n\t\tsize = { 1 45 }\n\t\tkind = tax\n\t\ticon = tax\n\t}\n"
            "\tplot = { x = 1 }\n\twidget = {\n\t\tkind = graph\n\t\tbar = 1\n\t\tline = 2\n\t}\n}\n")
    theirs = ("a = {\n\tplot = { x = 1 }\n\twidget = {\n\t\tkind = graph\n\t\tbar = 1\n\t\tline = 2\n\t}\n}\n")
    ours = ("REPLACE:a = {\n\twidget = {\n\t\tsize = { 1 45 }\n\t\tkind = tax\n\t\ticon = tax\n\t}\n"
            "\tplot = { x = 1 }\n\twidget = {\n\t\tvisible = mine\n\t\tsize = { 1 190 }\n\t\tmy_graph = yes\n\t}\n"
            "\twidget = {\n\t\tkind = graph\n\t\tbar = 1\n\t\tline = 2\n\t\tmine = 1\n\t}\n}\n")
    first = merge_texts(base, ours, theirs, unwrap=True)
    assert _decisions(first) == [("vanilla_removed", TAKE, ["widget"])]
    assert "kind = tax" not in first.text and "my_graph = yes" in first.text
    again = merge_texts(base, first.text, theirs, unwrap=True)
    assert _decisions(again) == [] and again.text == first.text


def test_a_block_whose_key_each_side_holds_once_is_paired_however_it_changed():
    """The key names the block: the mod moved it and rewrote most of it, and vanilla's
    change inside it is still a change to the mod's block."""
    base = "a = {\n\tmod = {\n\t\tx = 1\n\t\ty = 1\n\t\tfood = 1\n\t}\n\tplot = { p = 1 }\n}\n"
    theirs = "a = {\n\tmod = {\n\t\tx = 1\n\t\ty = 1\n\t\tfood = 2\n\t}\n\tplot = { p = 1 }\n}\n"
    ours = ("REPLACE:a = {\n\tplot = { p = 1 }\n"
            "\tmod = {\n\t\tx = 5\n\t\ty = 5\n\t\tfood = 1\n\t\tz = 1\n\t}\n}\n")
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert _decisions(r) == [("vanilla_changed", TAKE, ["mod", "food"])]
    assert "\t\tfood = 2" in r.text and "x = 5" in r.text


def test_a_moved_block_that_keeps_most_of_its_statements_is_still_paired():
    """Each side holds two widgets, so the key names neither. The mod moved one past a
    sibling and changed one of its statements: it keeps most of them, so it is the
    same block, and vanilla's deletion of it is a decision about the mod's text."""
    base = ("a = {\n\twidget = {\n\t\tkind = tax\n\t\ticon = tax\n\t\tsize = 1\n\t}\n\tplot = { x = 1 }\n"
            "\twidget = {\n\t\tkind = graph\n\t\tbar = 1\n\t}\n}\n")
    theirs = "a = {\n\tplot = { x = 1 }\n\twidget = {\n\t\tkind = graph\n\t\tbar = 1\n\t}\n}\n"
    ours = ("REPLACE:a = {\n\tplot = { x = 1 }\n"
            "\twidget = {\n\t\tkind = tax\n\t\ticon = tax\n\t\tsize = 2\n\t}\n"
            "\twidget = {\n\t\tkind = graph\n\t\tbar = 1\n\t}\n}\n")
    r = merge_texts(base, ours, theirs, unwrap=True)
    assert [(k, p) for k, _a, p in _decisions(r)] == [("both_changed", ["widget"])]


def test_two_new_blocks_at_one_place_go_in_in_vanilla_order():
    """Vanilla replaces two widgets with two new hboxes. The mod holds the first
    hbox's statements elsewhere, so the merge decides that hbox after its pass; the
    two still go in in vanilla's order, and a merge of the merged text asks nothing."""
    base = ("a = {\n\tmargin = 5\n\twidget = { w = 1 }\n\twidget = { w = 2 }\n}\n"
            "b = {\n\tslider = {\n\t\ts = 1\n\t\tt = 2\n\t\tu = 3\n\t}\n}\n")
    theirs = ("a = {\n\tmargin = 5\n\thbox = {\n\t\tslider = {\n\t\t\ts = 1\n\t\t\tt = 2\n\t\t\tu = 3\n\t\t}\n\t}\n"
              "\thbox = { tip = 1 }\n}\n"
              "b = {\n\tslider = {\n\t\ts = 1\n\t\tt = 2\n\t\tu = 3\n\t}\n}\n")
    ours = ("a = {\n\tmargin = 5\n\twidget = { w = 1 }\n\twidget = { w = 2 }\n}\n"
            "b = {\n\tslider = {\n\t\ts = 1\n\t\tt = 2\n\t\tu = 3\n\t}\n}\n")
    first = merge_texts(base, ours, theirs)
    assert first.text.index("hbox = {\n\t\tslider") < first.text.index("hbox = { tip = 1 }")
    again = merge_texts(base, first.text, theirs)
    assert again.text == first.text


def test_a_block_that_vanilla_deleted_does_not_pair_with_an_unrelated_block_in_its_place():
    """Vanilla deletes its widget; the mod keeps a widget of its own next to it. After
    the first merge the mod's widget is the only widget, in the deleted one's place,
    but it holds none of its text: it is no change of the deleted widget, and taking
    vanilla's side never deletes it."""
    base = "a = {\n\tspacing = 1\n\twidget = {\n\t\tsize = 33\n\t\tbutton = rank\n\t\taction = up\n\t}\n\thbox = { h = 1 }\n}\n"
    theirs = "a = {\n\tspacing = 1\n\thbox = { h = 1 }\n}\n"
    ours = ("REPLACE:a = {\n\tspacing = 1\n\twidget = {\n\t\tsize = 33\n\t\tbutton = rank\n\t\taction = up\n\t}\n"
            "\twidget = {\n\t\tsize = 33\n\t\tvisible = debug\n\t\ticon = circle\n\t\ttooltip = dump\n\t}\n"
            "\thbox = { h = 1 }\n}\n")
    first = merge_texts(base, ours, theirs, unwrap=True)
    assert _decisions(first) == [("vanilla_removed", TAKE, ["widget"])]
    again = merge_texts(base, first.text, theirs, unwrap=True, decide=lambda *a: (TAKE, "choice"))
    assert _decisions(again) == [] and "visible = debug" in again.text

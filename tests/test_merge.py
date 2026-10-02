"""The node-level three-way merge: each row of the decision table, insertion anchors,
order, adjacent edits, double insertions, GUI and the removed-line check."""
from pdxaudit import diff3
from pdxaudit.merge import KEEP, OPEN, TAKE, Op, merge_texts, removed_lines

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

"""Three-way REPLACE classification: vanilla before, the mod's copy, vanilla
after. Values are matched by (key path, key), so position never matters, and
deliberate choices (commented out, key removed, customized) are told apart from
accidental ones (frozen old value, missing new line, kept deleted line)."""
from pdxaudit.threeway import classify, classify_scalar


def _cls(results):
    return sorted((r.cls, r.key) for r in results)


def _one(results):
    assert len(results) == 1, results
    return results[0]


def test_frozen_value():
    r = _one(classify("REPLACE:a = { cost = 100 }", "a = { cost = 100 }", "a = { cost = 200 }"))
    assert (r.cls, r.key, r.old, r.new, r.mod) == ("frozen", "cost", "100", "200", "100")


def test_both_changed_value():
    r = _one(classify("REPLACE:a = { cost = 50 }", "a = { cost = 100 }", "a = { cost = 200 }"))
    assert (r.cls, r.mod) == ("both_changed", "50")


def test_already_merged_value():
    assert _cls(classify("REPLACE:a = { cost = 200 }", "a = { cost = 100 }",
                         "a = { cost = 200 }")) == [("merged", "cost")]


def test_new_vanilla_line_missing():
    r = _one(classify("REPLACE:a = { cost = 1 }", "a = { cost = 1 }",
                      "a = { cost = 1 upkeep = 5 }"))
    assert (r.cls, r.key, r.new) == ("new_line", "upkeep", "5")


def test_new_line_already_defined_by_mod_differently_is_both_changed():
    r = _one(classify("REPLACE:a = { cost = 1 upkeep = 9 }", "a = { cost = 1 }",
                      "a = { cost = 1 upkeep = 5 }"))
    assert r.cls == "both_changed"


def test_commented_out_line_is_deliberate():
    mod = "REPLACE:a = {\n\tcost = 1\n\t#trade_income = 0.1\t\t#M&T commented out\n}\n"
    r = _one(classify(mod, "a = { cost = 1 }", "a = { cost = 1 trade_income = 0.1 }"))
    assert r.cls == "commented_out"


def test_key_removed_by_mod():
    r = _one(classify("REPLACE:a = { other = 1 }", "a = { cost = 1 other = 1 }",
                      "a = { cost = 2 other = 1 }"))
    assert r.cls == "key_removed"


def test_kept_line_vanilla_removed():
    r = _one(classify("REPLACE:a = { cost = 1 legacy = 1 }", "a = { cost = 1 legacy = 1 }",
                      "a = { cost = 1 }"))
    assert (r.cls, r.key) == ("kept_removed", "legacy")


def test_customized_line_vanilla_removed_is_both_changed():
    r = _one(classify("REPLACE:a = { legacy = 2 }", "a = { legacy = 1 }", "a = { }"))
    assert r.cls == "both_changed"


def test_removed_line_mod_already_dropped_is_quiet():
    assert classify("REPLACE:a = { cost = 1 }", "a = { cost = 1 legacy = 1 }",
                    "a = { cost = 1 }") == []


def test_comparison_and_safe_operators():
    r = _one(classify("REPLACE:a = { limit = { gold > 100 } }",
                      "a = { limit = { gold > 100 } }", "a = { limit = { gold > 200 } }"))
    assert (r.cls, r.key, r.op, r.path) == ("frozen", "gold", ">", ("limit",))
    r = _one(classify("REPLACE:a = { x ?= b }", "a = { x ?= b }", "a = { x ?= c }"))
    assert (r.cls, r.op) == ("frozen", "?=")


def test_repeated_keys_compare_as_set_members_regardless_of_order():
    old = "a = { OR = { religion ?= r1 religion ?= r2 } }"
    new = "a = { OR = { religion ?= r3 religion ?= r1 religion ?= r2 } }"
    r = _one(classify("REPLACE:a = { OR = { religion ?= r2 religion ?= r1 } }", old, new))
    assert (r.cls, r.new) == ("new_line", "r3")
    assert _cls(classify("REPLACE:a = { OR = { religion ?= r3 religion ?= r2 religion ?= r1 } }",
                         old, new)) == [("merged", "religion")]


def test_bare_list_items_ignore_order():
    r = _one(classify("REPLACE:a = { tags = { B A } }", "a = { tags = { A B } }",
                      "a = { tags = { B A C } }"))
    assert (r.cls, r.new) == ("new_line", "C")


def test_changed_value_among_repeated_keys_is_unclassified():
    old = "a = { m = { add = 1 } m = { add = 2 } }"
    new = "a = { m = { add = 1 } m = { add = 3 } }"
    r = _one(classify("REPLACE:a = { m = { add = 1 } m = { add = 2 } }", old, new))
    assert r.cls == "unclassified"


def test_formatting_and_number_spelling_are_not_changes():
    old = "a = { b = { c = 1 } x = 0.10 }"
    new = "a = {\n\tb = {\n\t\tc = 1\n\t}\n\tx = 0.1\n}\n"
    assert classify("REPLACE:a = { b = { c = 1 } x = 0.1 }", old, new) == []


def test_new_sub_block_is_one_finding():
    r = _one(classify("REPLACE:a = { cost = 1 }", "a = { cost = 1 }",
                      "a = { cost = 1 modifier = { x = 1 y = 2 z = 3 } }"))
    assert (r.cls, r.key, r.path) == ("new_line", "modifier", ())
    assert "3 lines" in r.text


def test_removed_sub_block_still_carried_is_one_finding():
    r = _one(classify("REPLACE:a = { cost = 1 old = { x = 1 y = 2 } }",
                      "a = { cost = 1 old = { x = 1 y = 2 } }", "a = { cost = 1 }"))
    assert (r.cls, r.key) == ("kept_removed", "old")


def test_quoted_strings_with_hash_and_braces_are_values():
    mod = 'REPLACE:a = { text = "#R {x}#!" }'
    old = 'a = { text = "#R {x}#!" }'
    new = 'a = { text = "#G {x}#!" }'
    r = _one(classify(mod, old, new))
    assert (r.cls, r.mod) == ("frozen", '"#R {x}#!"')


def test_country_base_values_shape():
    mod = ("REPLACE:country_base_values = {\n"
           "\t#trade_income = 0.1\t\t#M&T commented out, no free Trade Income\n"
           "\ttrade_range = 300\n}\n")
    old = "country_base_values = {\n\ttrade_range = 200\n}\n"
    new = "country_base_values = {\n\ttrade_income = 0.1\n\ttrade_range = 300\n}\n"
    assert _cls(classify(mod, old, new)) == [("commented_out", "trade_income"),
                                             ("merged", "trade_range")]


def test_operator_only_change_is_visible():
    r = _one(classify("REPLACE:a = { culture = culture:x }", "a = { culture = culture:x }",
                      "a = { culture ?= culture:x }"))
    assert r.cls == "frozen"
    assert r.ops == ("=", "?=", "=")


def test_scalar_values():
    assert classify_scalar("5", "5", "7").cls == "frozen"
    assert classify_scalar("3", "5", "7").cls == "both_changed"
    assert classify_scalar("7", "5", "7").cls == "merged"
    assert classify_scalar("5", "5", "5") is None
    assert classify_scalar("5", "5", None).cls == "kept_removed"

# Handoff: merge mode cleanup (2026-10-01)

Brief: `<scratchpad>/brief_merge_cleanup.md`. The scratchpad is
`/tmp/claude-1000/-mnt-c-Users-Mjaklitsch-Documents-Paradox-Interactive-Europa-Universalis-V-mod--dev-mods-Specialized-Urban-Locations-Development/af8d2a06-d31a-4b45-86a9-f0bedb945cdf/scratchpad/`,
called `S` below. The work ended on a user stop order. The repo is clean. The full test suite passes (465 tests at 632b2c9).

## Status per item

| Item | State | Evidence |
|---|---|---|
| Gap 1: insertions pass through intent | DONE | de7b037, da19a94 (`path: [""]` selects a whole-copy deviation). Test: `test_new_definitions_pass_through_the_intent_store`, `test_matcher_fields`. Real run: 3 climates keep (rule `climate.no_vanilla_climates`), 16 units open (rule `combat.new_vanilla_units`), camels open (system economy owns the file), village_granary open (rule `economy.fe_unique_pms` owns building_types). |
| Gap 2: no vanilla text at --old | DONE | 5be2a9f. Empty base; `both_added` decision. Tests: `test_an_empty_base_takes_vanilla_only_nodes_and_opens_differing_twins`, `test_a_key_both_sides_added_is_written_once`, `test_a_file_vanilla_added_after_old_merges_with_an_empty_base`. |
| Gap 3: base text in the plan | DONE | 2b8e908. `base_version` holds the tag. Test: `test_apply_refuses_open_decisions_and_failed_checks`. |
| Gap 4: comments | DONE | 7fa11f5, 2e7201c, b3f2377. Tests: `test_a_comment_that_replaces_a_removed_statement_takes_its_place`, `test_comments_go_with_a_changed_or_inserted_vanilla_node`, `test_vanilla_comment_changes_on_unchanged_nodes_are_taken`, `test_comments_of_a_node_inserted_into_a_one_line_block_stay`. |
| Gap 5: pdx-format layout | DONE | 7dab925. Tests: `test_the_merged_text_gets_the_layout_of_pdx_format`, `test_apply_refuses_a_file_that_pdx_format_refused`. Real run: 84 files formatted, 4 files kept their layout (the mod keeps them out of pdx-format), 0 refused. |
| Gap 6: double empty lines | DONE | 08cc500. Test: `test_a_removal_leaves_no_two_empty_lines`. Real run: 0 new double empty lines. |
| Gap 7: skips | PARTLY DONE | 5be2a9f, 52bbbbb. default.map is a river_waterways output, so it is listed to regenerate. The rule in code (`untracked_reason`) and its test (`test_a_file_type_the_tracker_did_not_record_at_old_is_skipped_with_the_reason`) cover a type the tracker did not record. 12 listed skips remain: 1 definition too large to merge (generated_map_object_locators_combat.txt, vanilla changed 2 lines) and 11 files with no tracker history (.dds, .splnet). Tests exist for the lines-mode skip (`test_text_the_merge_cannot_compare_node_by_node_is_listed`) and the regenerate path. NOT DONE: there is no test for the "no history" (file_untracked) skip. |
| Gap 8: silent paths | DONE | 7c1a339. Tests: `test_an_old_version_that_is_not_tracked_is_an_error`, `test_copies_whose_vanilla_text_is_gone_are_decisions`, `test_overlapping_edits_fail_the_check`, `test_text_the_merge_cannot_compare_node_by_node_is_listed`. Fallbacks that remain, with the reason each is correct: `intent._read_cache` and `_write_cache` ignore an OSError (the cache only saves time); `proposer.blame` returns no commit in a folder that is not in git (evidence only); `_expand` keeps a one-line layout when it cannot lay out the block (pdx-format lays out the result). |
| Merge errors that B found (extra) | DONE | 6c6884f: the registry read no `manifest:` outputs. d69d761: a node taken from a later vanilla version follows vanilla. 52bbbbb: a generated file is regenerated, never skipped. |
| location_card cross-level move | DONE | b9faf72 and 632b2c9. Tests: `test_a_vanilla_block_the_mod_moved_into_its_own_block_follows_vanilla`, `test_a_new_vanilla_block_holding_a_block_the_mod_moved_is_open`. |
| B: reconciliation | NOT DONE | The numbers are below. The review rows of the last run (plan11) were classified with the decisions made on plan10, not one by one again. The audit flags 6 nodes in location_window.gui as vanilla_added (stale) in the merged result. These appeared after 632b2c9 and nobody checked them; they are probably the new open decisions. |
| D: --apply, format and brace checks | NOT DONE | Not run. The dry run shows 0 failed removed-line checks and 0 format failures in 152 files (plan11). |
| E: docs | NOT DONE | The design doc, README and HOW_IT_WORKS do not describe the new behaviour yet. This handoff lists it. |

## Reconciliation numbers (plan11, the hand port snapshot of 2026-10-01)

- Dry run: 152 files, 663 open decisions, 185 to regenerate, 12 skips, 0 failed checks.
- Differences between the merged files and the hand port: 779 rows.

| Class | Count |
|---|---|
| (a) merge-mode error | 0 known |
| (b) hand-port error | 124 rows (153 nodes) |
| (c) open decision | 173 |
| (d) SUL edit made outside the merge | 451, of which 77 differ only in order |
| (e) generated file | 0 rows; generated files are not merged |
| Files missing in the working tree | 31, see below |

Files missing in the working tree:

- 24 building_types copies (common_buildings, plantation_buildings, production_*, pure_one_level_productions, rural_buildings, town_buildings): (d).
- Locator files combat, dock and unit_stack: (d).
- fe_hud_topbar.gui: (d).
- Locator files city and vfx, and setup/start 07, 09 and 19: (c). Vanilla removed these files, and the hand port deleted them.

Notes on (b) and (d):

- 148 of the 153 (b) nodes hold vanilla changes older than 1.3.11 (`since` 1.2.0 to 1.3.11). The merge takes them because a copy's base is its baseline, not --old. Some of them can be deliberate SUL values that have no intent entry. Only 5 are changes made in 1.4.0.
- The fe_advances food cancels (`-0.2` in the merge, `-0.1` in the hand port) are (d). Rule `provisions.food_cancels` keeps the mod value, and a disposition cannot say "negate vanilla".

## Files in S/cleanup

- `premod/`: git archive HEAD of the mod. Changes from HEAD: the working tree `pdx-maint.toml` (HEAD has no climate or faster_universalis system); `tools/river_waterways/output/manifest.json` (gitignored); `pdx-maint.head.toml` holds the HEAD registry.
- `hcopy/`: snapshot of the mod working tree (the hand port), without .git. The time is in `hcopy_snapshot_time.txt`.
- `mcopy/`: premod with the merged texts of the last plan written in.
- `xdg/`: the scratch store (XDG_DATA_HOME). It holds 7 rules (5 seed rules plus `gap1_rules.json`) and 4 seed entries.
- `plan<N>.json`, `run<N>.md`: dry runs. The last one is `plan11.json`. `plan7.json` is a copy of the last plan, which the classifiers read.
- `plan_take.json`: the same plan with every open decision taken. It marks the regions of open decisions.
- `devs_m.json`, `devs_h.json`: the intent deviations of mcopy and hcopy.
- `recon_rows4.json`: the classified rows with their evidence. `recon_final.json`: the rows with a final class.
- `recon_table.md`: counts per file.
- `b_list.json`, `b_list.md`: every (b) node with file, line, copy, node path, change, since, the hand-port text and the vanilla text.
- `review1.txt`: the rows that needed a manual look.

## How to resume

```
S=<scratchpad>; C=$S/cleanup
$C/pipeline.sh 12        # dry run into plan12.json, take variant, mcopy, deviations, classify2/3/4
ROWS=recon_rows4.json python3 $C/show2.py $C "nodev:a?,nodev:?,a?,b-conflict,nodev:d?,nodev:b?"
/home/mjaklitsch/.local/share/pipx/venvs/pdx-audit/bin/python $C/where.py $C "<kinds>" <file part>
python3 $C/decs.py $C/plan12.json <file part> <copy name>
```

For D, copy premod to a new folder, then run:

```
XDG_DATA_HOME=$C/xdg pdx-audit merge --mod-root <copy> --vanilla-repo <tracker repo.git> --old 1.3.11 --new fcd7301f --dry-run --plan-out p.json
XDG_DATA_HOME=$C/xdg pdx-audit merge --mod-root <copy> ... --apply p.json
```

Then run `pdx-format --check` and `pdx-format --brace` on the written files.

## Suspected hand-port errors (b)

The full list is `S/cleanup/b_list.md` (153 nodes). Per file:

| Count | File |
|---|---|
| 26 | laws/fe_rgo_adjustments.txt |
| 25 | goods_demand/fe_navy_demands.txt (salt and cloth values) |
| 13 | building_types/fe_commercial.txt |
| 12 | unit_types/2_unlocked_through_tech.txt |
| 9 | government_reforms/fe_reform_adjustments.txt |
| 7 | unit_types/3_elephant_units.txt |
| 6 | casus_belli/fe_casus_belli.txt |
| 6 | peace_treaties/fe_peace_treaties.txt |
| 6 | script_values/fe_urbanization_building_caps.txt |
| 6 | unit_types/1_uniques_for_age_1_traditions.txt |
| 5 | building_types/fe_governor_tweaks.txt |
| 5 | unit_types/a_nahuatl.txt |
| 4 | building_types/fe_fort_maintenance.txt |
| 3 | disasters/fe_disasters.txt |
| 2 each | fe_societal_values, 03_army_heavy_cavalry, fe_army, 3_qizilbash, a_bedouin_cavalry |
| 1 each | fe_3_production_method_unlocks, fe_frontage_advances, fe_estate_privilege_adjustments, fe_army_demands, pop_demands, fe_laws, 02_army_light_cavalry, 05_army_auxiliary, map_markers.gui, fe_vanilla_economy_injects |

## Open questions

1. Should the merge take vanilla changes that are older than --old (the base is the copy's baseline), or keep them as the mod's values unless a rule says otherwise? This decides whether 148 (b) nodes are hand-port errors or missing intent entries.
2. Vanilla 1.4 rebuilt location_card and BuildingType_tooltip into new blocks and containers. The merge takes vanilla's new blocks and leaves SUL's changed nodes as open decisions. Is that the wanted result, or should SUL's changes move into vanilla's new containers?
3. Some SUL rules re-derive a value from vanilla's value, for example the food cancels. Should a rule disposition support this, beyond keep_mod?
4. Do the scratch rules `climate.no_vanilla_climates` and `combat.new_vanilla_units` match the user's intent? Their reasons come from the brief, not from the user.

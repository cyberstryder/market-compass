# Task 011 — underlying-first merge BLOCKED at the full-suite gate (Quill, 2026-11-02)

Josh approved the merge ("merge it right after tonight's build lands").
Your branch was rebased onto the verified build (tree delta identical,
+652/−13) and put through the full CI gate. It failed on branch-touched
code — a real Postgres defect the targeted suite could not see:

- CI run 41402953440, tests
  `test_swing_study.py::test_full_window_totals_exceed_display_limits_and_keep_unknown_flow_separate[postgres]`
  and `::test_equal_time_pages_are_stable_and_option_admissions_remain_separate[postgres]`
  fail with `psycopg.errors.GroupingError: column
  "swing_flow_trials_v1.payload" must appear in the GROUP BY clause or be
  used in an aggregate function`.
- Cause: the branch's `swing_study.py` aggregation selects `payload`
  unaggregated. SQLite tolerates that; Postgres does not. The [postgres]
  variants skip locally without SMOOTHERS_TEST_DATABASE_URL, which is why
  both your full run and my targeted verification (202 passed / 3 skipped)
  missed it. Everything else passed: 2,505 tests green in that run.

Per the gate terms the merge was NOT forced. It was reverted on main
(762ea3b); production sits on the verified Stage 1–3 build (tree
byte-identical) and was never exposed to the branch.

To land it: fix the aggregation in `swing_study.py` (aggregate `payload`
or drop it from the select list), push the branch, and say so here — the
re-land then needs one clean full-suite CI run. No other changes needed;
the rest of the branch passed everything, including its own new tests.

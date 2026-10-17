```text
QueueShield v2 agent rules
1. Read docs/BLUEPRINT.md sections 1 and 19, then your work package (section 22) and the sections it cites.
2. Edit only files listed under your package's Outputs. If you need a change elsewhere, write it in docs/DEVIATIONS.md and ask the lead.
3. qs/schema.py is the only place for enums and record types. Do not redefine them.
4. qs/core must never import qs/sim, and qs/sim must never import qs/core.
5. All randomness comes from qs/sim/tape.py or an explicitly seeded numpy Generator. Never use unseeded randomness.
6. All thresholds, windows and parameters that appear in the blueprint come from configs/*.yaml.
7. Write the acceptance tests first, then the code. Run `make test-fast` before every commit.
8. Prefer the simplest implementation that satisfies the acceptance tests. Log every assumption or deviation in docs/DEVIATIONS.md as one line.
9. No new dependency without recording it in docs/DEVIATIONS.md; assume the target machine is s390x and big-endian.
10. Never use ground truth, scenario names or trigger times inside qs/core or qs/forecast inference.
11. Do not change frozen configs after their tag (sim-frozen, eval-freeze).
12. Code comments and documentation state facts; they never claim Telum acceleration, ML for z/OS or Secure Execution.
```

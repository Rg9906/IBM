# Sensitivity sweeps on S4 (set B, exploratory)

12 paired configurations per cell. Medians; recovery excludes runs that never recovered (count shown). In simulation.

| sweep | value | universe | peak queue | excess retries | recovery s | not recovered |
| --- | --- | --- | --- | --- | --- | --- |
| d_op_s | 120 | U3 | 1903 | 8816 | 64 | 0/12 |
| d_op_s | 15 | U3 | 1298 | 4503 | 0 | 0/12 |
| d_op_s | 30 | U3 | 1764 | 4606 | 8 | 0/12 |
| d_op_s | 5 | U3 | 868 | 4438 | 0 | 0/12 |
| d_op_s | 60 | U3 | 1903 | 5227 | 26 | 0/12 |
| d_op_s | U0 | U0 | 1903 | 10037 | 74 | 2/12 |
| client_timeout_ms | 2500 | U0 | 1980 | 11030 | 76 | 2/12 |
| client_timeout_ms | 2500 | U3 | 1602 | 5004 | 0 | 0/12 |
| client_timeout_ms | 3000 | U0 | 1903 | 10580 | 71 | 2/12 |
| client_timeout_ms | 3000 | U3 | 1543 | 4854 | 0 | 0/12 |
| client_timeout_ms | 4000 | U0 | 1774 | 9348 | 76 | 2/12 |
| client_timeout_ms | 4000 | U3 | 1462 | 4526 | 0 | 0/12 |
| utilisation | 0.45 | U0 | 774 | 2388 | 12 | 0/12 |
| utilisation | 0.45 | U3 | 775 | 1965 | 12 | 0/12 |
| utilisation | 0.55 | U0 | 1991 | 9272 | 71 | 1/12 |
| utilisation | 0.55 | U3 | 1408 | 4367 | 0 | 0/12 |
| utilisation | 0.65 | U0 | 2883 | 19550 | 108 | 8/12 |
| utilisation | 0.65 | U3 | 1732 | 4666 | 0 | 0/12 |
| attempt_cap | 3 | U0 | 1835 | 8570 | 79 | 1/12 |
| attempt_cap | 3 | U3 | 1389 | 3823 | 1 | 0/12 |
| attempt_cap | 4 | U0 | 1890 | 9693 | 74 | 1/12 |
| attempt_cap | 4 | U3 | 1462 | 4482 | 0 | 0/12 |
| attempt_cap | 5 | U0 | 1903 | 10037 | 74 | 2/12 |
| attempt_cap | 5 | U3 | 1476 | 4526 | 0 | 0/12 |
| attempt_cap | 6 | U0 | 1905 | 10119 | 76 | 2/12 |
| attempt_cap | 6 | U3 | 1478 | 4538 | 0 | 0/12 |

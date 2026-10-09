# DECISIONS.md

1. **Indexes.** We created unique indexes `{emp_code: 1}` on `employees` and `{emp_code: 1, date: 1}` on `attendance_logs` to guarantee uniqueness and prevent race conditions. For query performance, we added `{department: 1, emp_code: 1}` on `employees`, alongside `{date: -1, emp_code: 1}` and `{emp_code: 1, date: -1}` on `attendance_logs` to enable index-covered sorting and range queries with zero `COLLSCAN`. We considered and rejected a standalone index on `status` due to low cardinality.

2. **Punch-in race.** When two identical punch-in requests arrive simultaneously, both generate documents and attempt `insert_one`. The compound unique index `{emp_code: 1, date: 1}` ensures atomicity at the database engine level. Exactly one insert succeeds and returns 201; the concurrent operation raises a `DuplicateKeyError`, which our exception handler converts into a clean 409 Conflict response.

3. **Ties.** We calculate ranks using MongoDB's `$rank` window function, producing standard competition ranking (`1, 2, 2, 4`), with tied rows ordered by `emp_code` ascending. The query applies `rank <= limit` *after* rank calculation; hence, all tied employees at the cutoff are included, even if the total returned rows exceed `limit`.

4. **Headcount.** Our aggregation pipeline queries `employees` first, filtering by `joined_on <= period_end`. It then performs a `$lookup` to join `attendance_logs`. Because the root documents represent all eligible employees, employees with zero attendance logs retain an empty array and are fully counted in the department `$group` stage.

5. **One thing you would change** if this had to serve 100x the data: We would implement incremental pre-aggregations (or a daily roll-up summary collection) updated atomically upon punch-out and regularization, replacing expensive real-time multi-collection aggregations with constant-time indexed reads.

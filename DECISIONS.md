# DECISIONS.md  (about 150-200 words in total - plain language, your own words)



**1. \*\*Indexes.\*\* We created a unique index on `employees(emp\_code)` to guarantee atomic employee identity, a unique compound index on `attendance\_logs(emp\_code, date)` to enforce one record per employee per day and handle race conditions, and compound indexes on `attendance\_logs(date DESC, emp\_code ASC)` and `(status, date)` to ensure fast sorting and range scans. We considered indexing `history.at`, but rejected it since audit trails are accessed via parent document lookups rather than direct querying.**



**2. \*\*Punch-in race.\*\* When two identical punch-in requests arrive simultaneously, MongoDB's unique compound index on `(emp\_code, date)` ensures that only one database insertion succeeds and commits with 201 Created. The second concurrent request hits a duplicate key exception (`DuplicateKeyError`), which our code catches and rejects cleanly with a 409 Conflict status code.**



**3. \*\*Ties.\*\* Our leaderboard implements standard competition ranking (1, 2, 2, 4) where tied employees share the same rank and subsequent ranks are skipped. The `limit` parameter is applied \*after\* ranking, meaning that if multiple employees tie at or below the limit threshold, all tied records are returned. Tied rows are ordered by total late minutes descending, then `emp\_code` ascending.**



**4. \*\*Headcount.\*\* The department summary uses the `employees` collection as the base pipeline source, filtering for employees whose `joined\_on` is on or before the last day of the month (Rule R9). It then left-joins (`$lookup`) attendance logs for that month. This guarantees that employees with zero attendance logs are correctly included in the headcount and department statistics.**



**5. \*\*One thing you would change.\*\* If this had to serve 100x the data (10M+ records), we would introduce monthly pre-aggregated summary collections or materialized views updated via background worker jobs (or MongoDB change streams) for heavy analytics like monthly summaries and leaderboard rankings, while keeping raw logs strictly for daily operational punch-ins and regularizations.**


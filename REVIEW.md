# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

|#|Where (function / line)|What is wrong|How you'd notice it (test, input, or symptom)|How you fixed it|
|-|-|-|-|-|
|1|`compute\_late\_minutes`|Did not check the 10-minute grace period correctly (returned `minutes if minutes > 10 else 0`, but subtracted from start instead of grace period rules).|Punching in at 09:41 incorrectly returned 11 minutes late instead of 11 minutes (or incorrect offset).|Adjusted calculation strictly per R2 (`diff\_minutes > 10`). |
|<br />2|`compute\_overtime`|Used `datetime.fromisoformat(date\_str)` without timezone (`IST`), leading to naive datetime comparison errors.|Comparing UTC punch-out time against naive shift end datetime caused Python `TypeError`.| Used explicit `IST` timezone when constructing shift end datetimes.|
|3|`list\_attendance`|Loaded all documents into memory and sorted in Python (`docs.sort(...)`), which will time out or crash on a 100,000-record dataset. |Testing with 100k logs causes high memory usage and slow responses.|Replaced in-memory sorting with MongoDB `.sort(\[("date", DESCENDING), ("emp\_code", ASCENDING)])` and pagination.|
|4|`punch\_in`| Did not enforce unique compound index or race condition handling for duplicate punch-ins on the same date.| Concurrent punch-in requests created duplicate documents for the same employee and date. |Added unique compound index on `(emp\_code, date)` and handled duplicate key exceptions with 409 Conflict. |
|5|`list\_employees`|Skip calculation was `page \* page\_size` instead of `(page - 1) \* page\_size`, skipping the first page of results.| Requesting page 1 returned items starting from `page\_size` instead of index 0.| Fixed skip formula to `(page - 1) \* page\_size`.|
|6| `create\_employee`| Missing validation for `shift\_start == shift\_end` and incorrect datetime storage for `created\_at`.| Creating an employee with identical shift times succeeded, and `created\_at` wasn't converted to epoch milliseconds.|Added validation check returning 422 and stored UTC datetime properly.|
|7|`punch\_in`| Did not truncate millisecond/microsecond timestamps to whole seconds per Rule R1.|Timestamps retained microseconds, failing strict contract checks.|Added `truncate\_to\_second()` helper on all timestamps. |
|8| Missing endpoints |All endpoints marked "You" in the specification were completely missing. |Calling `/attendance/punch-out` or analytics endpoints returned 404/501.|Implemented all required POST, PATCH, and GET analytics aggregation routes.|
|9|Missing startup indexes|No database indexes were created at startup.|Queries against 100k records resulted in slow collection scans (`COLLSCAN`).|Added `@app.on\_event("startup")` to create required unique and compound indexes idempotently|
|10|Rounding | Standard Python `round()` uses round-half-to-even (banker's rounding) instead of half-up required by R8. | Edge case decimal values rounded incorrectly.|Implemented `decimal.Decimal` with `ROUND\_HALF\_UP`.|

Also note anything you looked at and decided was **not** a defect, and why.


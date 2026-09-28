# intern-radar

A GitHub Actions job that checks company job boards twice a day (8am and 6pm
US Central) and opens a GitHub issue for every new intern / internship / co-op
posting in computer science (software, ML/AI, data, research, security,
infrastructure, ...). Other intern roles (design, PM, finance, sales, ...) are
skipped. The keyword lists are `CS_RE` / `NON_CS_RE` at the top of `tracker.py`.
Issues are labeled `big-tech` or `startup`. Postings already reported
are recorded in `seen.json`, which the workflow commits back after each run.

Run it locally with Python 3.9 or later (standard library only):

```sh
python tracker.py --dry-run            # print new postings; no issues, no seen.json write
python tracker.py --dry-run --only Hex # check a single company
```

## Adding a company

Add an entry to `companies.json`. `category` is `startup` or `big-tech`.

| board        | fields                                  | find the slug in                                     |
|--------------|-----------------------------------------|------------------------------------------------------|
| `greenhouse` | `slug`                                  | `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` |
| `ashby`      | `slug`                                  | `jobs.ashbyhq.com/<slug>`                            |
| `lever`      | `slug`                                  | `jobs.lever.co/<slug>`                               |
| `workday`    | `tenant`, `wd`, `site`                  | `https://<tenant>.<wd>.myworkdayjobs.com/<site>`      |
| `eightfold`  | `host`, `domain`                        | the careers site's host; `domain` is the `?domain=` value |

```json
{"name": "Example", "category": "startup", "board": "greenhouse", "slug": "example"}
{"name": "Example", "category": "big-tech", "board": "workday", "tenant": "example", "wd": "wd5", "site": "External"}
```


## Fixing a broken board

A failing company appears as `[FAIL] Name (...)` in the run log and in the
summary. The other companies still run, and the failing company's history in
`seen.json` is kept.

1. Open the company's careers page, click a job, and look at where it links.
   A company that changes ATS (for example, Lever → Ashby) usually shows
   this in the job URL.
2. Update `board` and `slug` in `companies.json`, then run
   `python tracker.py --dry-run --only Name` to confirm the fix.
3. If the new board uses different job IDs, the next run treats every current
   intern posting at that company as new. Expect one batch of issues.

`429 Too Many Requests` errors from Eightfold sites (Netflix) are
retried automatically. A company that still fails after retries is tried
again at the next scheduled run.

## Board notes

- **Warp**: tracked on Greenhouse `warp`. Ashby `warp` is a different company
  (an HR/payroll platform).
- **Hex**: tracked on Greenhouse `hextechnologies`, whose links point to hex.tech.
  A small Ashby `hex` board also exists, but all of its jobs also appear on Greenhouse.
- **Anyscale**: Ashby. The old Lever board only holds a "we moved" posting.
- **Teleport**: Ashby slug is `goteleport`.
- **Vercel, Mercury**: Greenhouse. Each also has an empty Ashby board.

## Check manually

These companies have no public JSON endpoint that works with a plain request
(no auth, no headless browser, no HTML scraping):

| Company   | Careers page | Why |
|-----------|--------------|-----|
| Bloomberg | https://bloomberg.avature.net/careers/SearchJobs/intern | Avature board, HTML only |
| LinkedIn  | https://www.linkedin.com/jobs/search/?keywords=intern&f_C=1337 | The public Greenhouse and Lever `linkedin` boards contain only test postings |
| Stytch    | https://jobs.twilio.com/careers | Now part of Twilio; careers redirect to Twilio's board, and the Stytch Ashby board is empty |

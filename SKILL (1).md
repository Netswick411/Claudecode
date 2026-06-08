---
name: event-finder
description: >-
  Enrich a lead or company CSV with upcoming in-person event attendance. For
  each company, find ONE real, in-person trade show / conference it is
  exhibiting at, sponsoring, or hosting within a configurable date window
  (default: 2 to 9 months from today), and append Event Name / Event Date /
  Event Role columns. Use this skill whenever the user wants to find what trade
  shows or conferences their prospects are going to, enrich a lead list with
  event data, build event-based cold-email personalization, or replace
  Clay / Claygent web research for event detection. Trigger it even if the user
  just says "find events for these companies" or "what conferences are these
  leads attending" without naming the skill.
---

# Event Finder

Take a lead-list CSV and return the SAME CSV with three appended columns telling
you which upcoming in-person event each company is attending. Built to beat
per-row web-research tools (Claygent) on two axes: **consistency** (research each
company once, not once per lead) and **precision** (never invent an event).

## Output contract (read this first)

Append these columns to the original CSV, preserving every original column and
the original row order:

| Column        | Values                                                                 |
|---------------|------------------------------------------------------------------------|
| `Event Name`  | Humanized event name usable as an email variable, **or** `No`          |
| `Event Date`  | `Month Year` (e.g. `September 2026`); blank if `No`                     |
| `Event Role`  | `Exhibiting` \| `Sponsoring` \| `Hosting`; blank if `No`               |
| `Event Source`| URL that proves the claim (QA aid — user may delete before import)     |
| `Event Proof` | The exact sentence from the source naming the company (QA aid — deletable) |

A row gets a real event ONLY if a credible source confirms it. **When in doubt, output `No`.** A false positive poisons a cold email; a `No` costs nothing.

> `Event Source` and `Event Proof` exist so you can audit results and the user can verify before importing to Clay. They are not used in the email — the user deletes them after spot-checking. The three columns the user actually keeps are `Event Name`, `Event Date`, `Event Role`.

## Parameters (confirm with the user, then hard-code for the run)

- **Date window**: events whose date falls between `today + 2 months` and `today + 9 months`. Compute from the real current date at run time. (For a run on 8 Jun 2026 this is **Aug 2026 → Mar 2027**.)
  > ⚠️ The original Claygent prompt used a **4–7 month** window ("4-7 months after 26 May 2026"). This skill defaults to the **2–9 month** window the operator specified. Confirm which one is correct before each run and edit this line if needed — the window is the single most consequential parameter.
- **Region**: default worldwide; narrow to a country/region if the user says so.
- **Exclude own customer/user conferences**: default ON (see Research protocol).

## Workflow

1. **Dedupe.** Collapse the lead list into unique companies so each is researched once:
   ```bash
   python scripts/dedupe_companies.py INPUT.csv worklist.csv
   ```
   `worklist.csv` has one row per company with `company_key`, name, website,
   LinkedIn, industry, country.

2. **Research in parallel batches.** Split `worklist.csv` into batches (~25–50 companies each) and spawn Sonnet subagents (Task tool), one batch per agent, following the **Research protocol** below. Each agent appends rows to a shared `results.csv` with header:
   `company_key,Event Name,Event Date,Event Role,Event Source,Event Proof`
   Write results incrementally so a crash never loses completed work. Re-running should skip `company_key`s already present in `results.csv`.

3. **Merge back onto every lead:**
   ```bash
   python scripts/merge_results.py INPUT.csv results.csv OUTPUT.csv
   ```
   Every lead at a researched company inherits that company's event; companies
   with no qualifying event are filled `No`.

4. **Spot-check.** Open 10–15 random rows where `Event Name` != `No`, click the
   `Event Source`, and confirm the company + date + in-person are all real before
   handing back `OUTPUT.csv`.

## Research protocol (the part that matters)

Each subagent works in two phases per company: **(A) collect raw candidates from sources it actually opens, then (B) filter and select.** Separating these is what stops the hallucination/over-claiming that the per-row tool produced.

### Phase A — Collect (do NOT filter by date yet)

Find every in-person event the company is **exhibiting at, sponsoring, or hosting** that you can confirm from a source you actually open. Collect up to 5, past or future — do not judge the window yet. For each candidate, you must have opened a source that gives you:

- the exact event name
- the start date, read off the source (see normalization below)
- the company's role
- a `proof` sentence from that source that names **this company** in connection with the event
- the `source` URL

Search workflow:
- Company site: `/events`, `/news`, `/press`, `/blog`, homepage — look for "Join us at", "Visit our booth", "Register now".
- Company LinkedIn (last few months) + its Events tab.
- Up to 3 web searches combining the company name with `booth`, `sponsor`, `exhibiting`, `conference`, `summit`, plus the likely year(s).
- If the company's industry has a major show coming up, check that show's exhibitor/sponsor list for the company name.

**Date normalization:** convert every date to the START date (first day of a multi-day event), exactly as the source states it — never guess or shift. "September 14–15, 2026" → `2026-09-14`. Do **not** borrow a date from unrelated text (internship dates, "celebrating 40 years", etc.).

**Do not fabricate.** Every candidate comes from a source you opened. No explicit start date on the source → drop that candidate.

### Phase B — Filter & select

Keep a candidate ONLY if **all** of these hold:

1. **In-person.** Physical trade show, expo, conference, or summit. Exclude webinars, virtual/online-only/"digital" events.
2. **In the date window.** Start date falls inside the configured window (default `today+2mo … today+9mo`). Past and too-far-out events are dropped.
3. **Company is confirmed there**, with a `proof` sentence naming this company. A generic event homepage that doesn't mention the company = not confirmed. A search snippet saying "Missing: <company>" = not confirmed. Industry relevance alone ("a SaaS company, probably at SaaStr") is a guess, not evidence.
4. **Role is `exhibiting`, `sponsoring`, or `hosting`.** If the company is only **attending, speaking, or merely mentioned**, drop it — those roles do not qualify.
5. **Right company** — matched by website domain, not just name.
6. **Not the company's own customer/user conference.** Classify the event type; **exclude `customerConference`** (a company hosting its own customers/users — e.g. "<Brand> User Conference", "<Brand>world", a proprietary annual summit). Partner- and developer-conferences the company hosts are also excluded by default as "own events". *(If the user turns this filter OFF, keep them and set `Event Role` = `Hosting`.)*

If more than one candidate survives, pick the **soonest** one in the window. Write its humanized name, `Month Year` date, role, source URL, and proof sentence to `results.csv`. If nothing survives, write `Event Name = No`.

## Humanizing `Event Name`

The value must drop cleanly into a sentence like:
> *"Saw {{Company}} is heading to **{{Event Name}}** this fall — …"*

Rules:
- Use the event's common spoken name.
- **Strip** the year, edition number, host city, and venue.
- Keep the recognizable brand; don't expand acronyms people use as-is.
- No leading article (`the`) — let the email template add context.

| Official listing                          | `Event Name` output      |
|-------------------------------------------|--------------------------|
| PPAI Expo 2027 — Las Vegas Convention Ctr | `PPAI Expo`              |
| HIMSS25 Global Health Conference & Exhib. | `HIMSS`                  |
| NRF 2027: Retail's Big Show, NYC          | `NRF Big Show`           |
| ISTELive 26, San Antonio                  | `ISTELive`               |

## Scripts

- `scripts/dedupe_companies.py INPUT.csv WORKLIST.csv` — collapse leads to unique companies (key = normalized website domain, fallback company name).
- `scripts/merge_results.py INPUT.csv RESULTS.csv OUTPUT.csv` — join results back to all leads, preserve original columns + order, fill missing with `No`.

Both use only the Python standard library (no installs needed).

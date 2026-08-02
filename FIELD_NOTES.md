# Field notes

Things I measured about job-listing data that turned out to contradict what I
assumed. They're written down because each one changed how HunterJobs behaves,
and because "obviously it works like X" was wrong often enough that I stopped
trusting it.

All figures are from my own pool, measured August 2026. They are observations
about a sample, not laws of nature.

---

## Listings live much longer than they feel

The ten oldest LinkedIn listings in my pool were posted **27-29 June**. Checked
five weeks later, **all ten were still live.**

I had assumed listings rot in days and was ready to auto-expire anything
unseen for two weeks. That would have thrown away ten live jobs out of ten.

**What HunterJobs does:** no listing is ever expired on a timer. A job is
marked gone only when something positively says so.

## A missing listing usually means a missing request

While testing, several listings came back "not found" — and were provably live
minutes later. The difference was request pacing. Ask too fast and you get told
things that aren't true.

This is the same shape as three other bugs I found the same week: a scraper
capped at 200 comments in a 311-comment thread, failed fetches returning
"nothing" instead of raising, and a paginator that skipped whole pages. Every
one of them silently under-reported and nothing complained.

**What HunterJobs does:** absence is never treated as death on first sight. A
listing has to fail twice, on separate unhurried passes, before it's marked
gone. Rate limits, timeouts and server errors mean "ask again later" — never
"deleted".

## About 11% of Hacker News listings are already dead

The July 2026 *Who is hiring?* thread had **311** top-level comments. **35 of
them (11%) were deleted or flagged** — they still exist as comments, they're
just empty.

That's a rare gift: the source *tells you* a listing is gone instead of leaving
you to infer it from silence. It also means any count of "jobs on HN" is ~11%
lower than the comment count suggests.

## Filters aren't always applied

Some job boards accept filter parameters and return identical results whether
you set them or not. A "remote only" request can come back with the same
listings as an unfiltered one.

I found this by comparing the actual listing IDs returned with and without
filters — they matched exactly. If I'd trusted the filter, I'd have believed I
was collecting remote jobs and been quietly wrong about all of them.

**What HunterJobs does:** work mode is decided by reading the listing text, not
by trusting a filter. That's slower and it's the only version I can defend.

## Default ordering is rarely chronological

Asking for recent listings returned them in relevance order — Aug 1, Jul 31,
Jul 29, Jul 30 — not newest-first. So "the most recent N listings" was actually
"N listings a ranking algorithm chose, which happen to be recentish".

For a job hunt that's mildly annoying. For measuring anything it's fatal,
because your sample is shaped by a ranking function you can't see.

**What HunterJobs does:** always requests explicit date ordering, so a time
window means what it says.

## Depth limits are real and they're low

Most search interfaces stop serving results after a fixed depth, regardless of
how many matches exist. Past that point older listings are simply unreachable —
not slow, not paginated, gone.

The practical consequence: for sources like that, you can only see recent
listings, and *absence from a search tells you nothing* about whether an older
listing still exists. You have to check it directly.

**What HunterJobs does:** for census-style sources (Y Combinator, Hacker News)
a completed pass is trusted as evidence of what exists. For search-style
sources it isn't, and listings are verified individually instead.

---

## The pattern

Every item here is the same failure wearing different clothes: **a system
reported success while quietly returning less than it promised.** A cap that
trimmed, a filter that didn't filter, an order that wasn't ordered, an error
that returned an empty list.

None of them threw an exception. All of them would have produced confident,
wrong numbers.

The rule that came out of it, and the one the codebase follows: **count what
you asked for, count what you got, and say so out loud when they differ.**

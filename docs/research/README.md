---
id: DOC-005
title: "Research: main findings"
summary: "What the measurements of codenav show so far: agent adoption (hint needed for GPT-6 Luna), completeness and effort on big renames, defects found by watching small models, refactor correctness on real projects, mutation testing; each finding links to its data"
type: reference
status: current
updated: 2026-11-27
verified:
covers: []
related: [DOC-004, DOC-006, DOC-007, DOC-008]
supersedes:
superseded_by:
---

# Research: main findings

What has been measured about codenav, and where the data is. Each finding below is one or two sentences with a link
to the page that has the full table, the method and the limits. Those pages link to the raw runs in
[data/](data/). All measurements so far are on one main repository (srxy) plus click and tomlkit for the refactor
fuzzer. They are pilots: read them as direction, not effect sizes.

## Coding agents with and without codenav ([DOC-007](agent-ab.md))

1. **Adoption depends on the model; one line of project hint fixes it.** Without a hint, GPT-6 Luna called codenav in
   0 of 12 runs; with one sentence in AGENTS.md, 12 of 13. Claude Haiku 5.5 used it unprompted (17 of 18).
   [A1](agent-ab.md#finding-a1-whether-an-agent-uses-codenav-depends-on-the-model-and-one-line-of-project-hint-fixes-it)
2. **On a 35-file rename, codenav made Luna complete and Haiku cheaper.** The tests can't see 4 of the files (an
   example, a doc page, two shell scripts); codenav offers them as optional sites. Luna with codenav renamed all 4 in
   4 of 5 runs, without it in 0 of 4. Haiku was usually complete either way, but with codenav it used 161k-359k
   input tokens against 368k-519k.
   [A2](agent-ab.md#finding-a2-on-a-35-file-rename-codenav-made-luna-complete-and-made-haiku-cheaper)
3. **On the other tasks the arms were equally correct.** srxy's names are distinct and its tests thorough, so
   careful grep was enough; codenav sometimes cost more tokens there.
   [A3](agent-ab.md#finding-a3-on-the-other-tasks-the-arms-were-equally-correct)
4. **Finding unused code:** the 3 runs that called `unused` found all 4 unused functions; 6 of the other 7 runs found
   at most 2. [A4](agent-ab.md#finding-a4-finding-unused-code-the-unused-tool-helped-when-it-was-called)
5. **Watching small models found 3 codenav defects,** all fixed and re-run: Python in shell heredocs wasn't scanned;
   those sites, once offered, were skipped until labelled; and optional sites in another class's docstring were a
   false lead. [A5](agent-ab.md#finding-a5-watching-small-models-found-three-codenav-defects-all-fixed)

The separate [frozen-build Haiku confirmation](agent-ab.md#frozen-build-haiku-confirmation-27-november-2026)
adds 12 runs on `f8b6c89`. CodeNav completed both large renames and found all four unused functions twice; plain
completed one rename and found two then one unused functions. Both arms passed the narrow rename control.
Two repeats per task support these observed examples, not a general ranking.

## Correctness of refactors ([DOC-006](correctness.md))

6. **187 random refactors on srxy, click and tomlkit broke nothing silently.** The 3 failures the fuzzer reported
   were its own false positives. [C1](correctness.md#finding-c1-random-refactors-on-3-real-projects-found-no-silent-breakage)
7. **References agree with jedi on 40 of 40 symbols; renames with rope on 38 of 40,** and the 2 differences are
   call sites rope misses. [C2](correctness.md#finding-c2-references-and-renames-agree-with-jedi-and-rope)
8. **rope's rename misses real call sites,** so the agent task keys were checked by hand.
   [C3](correctness.md#finding-c3-rope-also-misses-real-call-sites)

## Strength of codenav's own tests ([DOC-008](mutation.md))

9. **The test suite catches about 84% of small code changes (mutants)** in the 4 modules that choose optional rename
   sites and store edits. The survivors showed about a dozen real test gaps, two tests that passed by accident and a
   timing-dependent test setup, all fixed; they found no product bugs.
   [M1, M2](mutation.md#finding-m2-most-survivors-dont-matter-a-few-were-real-gaps),
   [M3](mutation.md#finding-m3-the-run-also-exposed-a-flaky-test-setup)
10. **Worth running after changing those modules,** reading only the survivors of changed functions; not as a CI gate.
    [Is it worth running?](mutation.md#is-it-worth-running)

## How these were measured

- Agent pilots: [harness/](harness/README.md), the scripts and task texts used, with
  [ground truth](data/agent-ab/ground-truth/).
- Refactor fuzzer, differential and mutation testing: [Hardening checks](../hardening.md) (DOC-004).

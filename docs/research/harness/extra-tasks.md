# Extra tasks for the small-model pilot (same format as the TASK blocks in ../orchestrator-prompt.md)

Tasks where a text search misleads: grading facts are in README.md next to this file.

TASK 8:
Rename the FileSearchResult.lines field to line_matches in the code and tests. The JSON keys named "lines" in the
worker protocol and in the CLI's JSON output are a wire format and must NOT change.

TASK 9:
normalize_max_file_size is defined twice (in adapters/inbound/cli/cli.py and in application/search_runner.py). Find
out which copy is actually used, delete the unused one, and show that nothing depended on it.

TASK 10:
Rename the method score of the Matcher base class (srxy.application.matching.base) to similarity, in the base class,
every matcher that overrides it, and every call of it in the code and tests. Other things called score (result
scores, score= arguments, the image-similarity score method) must not change.

TASK 11:
Which module-level functions in src/srxy/adapters/inbound/cli/cli.py are not used anywhere in the repository (src,
tests, scripts, examples)? For each, give file:line and the evidence that nothing uses it. Change nothing.

TASK 12:
Rename the matches method of the _Segment class in src/srxy/domain/ignore_rules.py to matches_name, with every call
of it in the code and tests. The other matches methods in that file must not change.

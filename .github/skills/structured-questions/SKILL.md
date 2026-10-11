---
name: structured-questions
description: "Ask the user a decision question in a structured way: problem, solution, suggestion, and the pros and cons of the recommended option. Use whenever calling ask_user or asking the user to choose between approaches, scope, or behavior."
argument-hint: "Describe the decision that needs the user's input"
---

# Structured Questions

Use this skill whenever you ask the user a question with the `ask_user` tool.
A bare question forces the user to reconstruct the context. A structured question
states the problem, the proposed solution, the suggestion, and the trade-offs, so
the user can answer in one reply.

## Question Shape

Write the `question` as one paragraph per part, in this order, with no headlines
and no bullet or numbered lists. Each paragraph starts with the title of its part.

1. **Problem:** what is wrong or undecided, with the concrete evidence (file,
   test, count, or observed behavior). Two or three sentences at most.
2. **Solution:** the change that would be made if approved.
3. **Alternatives:** every realistic option, including the recommended one and
   "leave it as is", each with its own pros and cons in the same paragraph.
   Name each option by its action, not by a letter or number.
4. **Suggestion:** the recommended option and the reason for it.
5. **Pros:** the advantages of the recommended option.
6. **Cons:** the disadvantages of the recommended option.
7. **Question:** one final sentence that asks exactly one thing.

Write each paragraph as a statement of fact or proposal, without imperatives such
as "Do this" or "Let's go with". Do not offer only the recommended option: the user
must be able to choose among all alternatives from the question alone. Find the
alternatives before asking, by checking what the code, the runtime, and the test
setup allow, rather than listing only the first idea.

## Length

Keep the whole question short, about 120 words at most. Use short sentences, and
name each alternative in a few words. Drop evidence that does not change the
decision. Do not repeat the same point in several parts.

## Deprecation Warnings

When the problem involves a deprecation warning, first find out why it is raised
and whether the API is scheduled for removal. A deprecation can mean the API will
be removed, and the proposal must not rely on an API that could disappear. If the
API is stable (for example `os.fork`), say so in the problem paragraph and propose
a narrow suppression. Scope the suppression to the code that triggers the warning,
not to the whole test suite.

## Choices

- Put the recommended option first and label it `(Recommended)`.
- Offer two or three choices that map to the solutions discussed. Do not add
  "Other" or "Something else"; the UI adds a freeform option.
- Ask one question per call. If several decisions are needed, ask them in sequence.

## Example

Problem: namespace probe tests emit fork() `DeprecationWarning`s under xdist workers only. CPython 3.12 raises it when `os.fork()` runs with another thread alive.

Solution: scope a `filterwarnings` marker to the two probe modules.

Alternatives: keep the filter (test-only, may hide regressions there); replace `os.fork()` with a `subprocess` helper (no warning, slower, changes production); run these tests serially without xdist (no warning, split CI); leave warnings visible (noisy).

Suggestion: keep the filter, because production probes before any thread starts.

Pros: test-only, other modules stay visible.

Cons: regressions in those modules stay silent.

Question: Which alternative should I pursue?

Choices: "Keep the scoped filter (Recommended)", "Replace the probe's fork with a subprocess helper", "Run namespace tests without xdist", "Leave the warnings visible"

## Checklist Before Asking

- Each part is one short paragraph that starts with its title; there are no headlines.
- The whole question is about 120 words or fewer.
- The Alternatives paragraph lists every realistic option, each with pros and cons.
- No paragraph uses an imperative such as "Do this".
- Deprecation problems state why the warning is raised and whether removal is planned.
- The recommended option is first and labeled.
- Pros and cons describe the recommended option only.
- Exactly one question is asked.

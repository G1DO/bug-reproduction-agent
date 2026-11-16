Closes #<!-- issue number -->

## Problem

<!-- The user problem this solves. -->

## Acceptance check

<!-- The observable result that shows it works. -->

Proven by: <!-- test name, or an evidence link if it can't be automated -->

## Quality

Affects: <!-- the quality attribute this change affects (e.g. security, reliability, performance, accessibility, cost), or none -->

Proof: <!-- the recorded target, if any, and the command, test, or alert that proves it; an evidence link if it can't be automated -->

Trade-off: <!-- what gets worse -->

<!-- New targets go in docs/architecture/README.md in this PR, and only from a stated requirement. -->

## Trigger check

- [ ] Hard-to-reverse choice? → decision record in `docs/decisions/` in this PR
- [ ] New trust boundary, new kind of untrusted input, or auth/permissions/secrets/personal-data change? → threat model update (`docs/threat-model.md`) in this PR
- [ ] Contract or schema change, too big for one day, live service, others rely on it, data users can't recreate, published artifacts, legal obligation, model or data pipeline, or paid infrastructure? → that trigger's output in this PR

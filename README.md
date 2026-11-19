# hwdesign-work-test

Test submissions for the hwdesign course's LLM grader.

This repository is laid out the way a student's course repository is: one folder
per attempt, and each folder is submitted to the grader as a link, for example
`https://github.com/sdrangan/hwdesign-work-test/tree/main/cmdresp/fir_strong`.
The grading tests in the course's solution repository point at these folders.

These are deliberately mixed: some attempts are strong, some weak, and some are
broken on purpose to check that the grader notices. None of them is a model
answer, with one exception below.

## Example project outlines

`example_outlines/` holds initial project plans for the project outline
assignment. The course's project docs link students to the two strong ones as
examples:

- [`cg/initial_plan.md`](example_outlines/cg/initial_plan.md): a conjugate
  gradient matrix inverse.
- [`robotics/initial_plan.md`](example_outlines/robotics/initial_plan.md): a
  batched robot arm simulation.

[`poor/initial_plan.md`](example_outlines/poor/initial_plan.md) is a weak plan
written to lose points: a one-person team, no mathematics, and one module with
no stated interfaces. It is there to test the grader, not to copy.

# Module: main

The orchestrator. Media file in → `final.srt` out, by way of every stage in
order, with every intermediate written to disk.

**It contains zero domain logic.** Nothing about Hebrew, subtitles, timings,
or correction lives here. If a change to `main` requires knowing what a word
is, it belongs in a module instead.

## Owned files

```
src/hebsub/main.py
tests/test_main.py
```

## Responsibility

1. Parse arguments.
2. Create `work/<jobname>/`.
3. Call `transcribe` → `proofread` → `segment` → `export`, in that order.
4. Write every intermediate artifact to disk before calling the next stage.
5. Validate at each boundary via `contract`.
6. Catch stage failures, report them naming the module, exit non-zero.
7. Print the warning summary at the end.

That is the entire list.

## CLI

```
python -m hebsub.main --in interview.mp4 --job interview_dana \
    [--engine ivrit_local] [--vocab glossary.txt] \
    [--from STAGE] [--to STAGE] [--force] \
    [--gap MS] [--rlm] [--isolate] [--bom] [--format srt|vtt]
```

## Artifacts

Under `work/<jobname>/`, always, never in memory only:

```
01_raw.json         transcribe
02_proofread.json   proofread
03_segmented.json   segment
final.srt           export
```

`--from` / `--to` let a run resume from an existing artifact — the point of
writing them. `--from proofread` reads `01_raw.json` off disk and skips the
transcribe call.

## Re-running and `--force`

Each stage raises `StageAlreadyRun` when its name is already in `meta.stages`.
`main` passes `--force` straight through to every stage it invokes; it does
not decide on a stage's behalf whether re-running is acceptable, and it does
not strip or rewrite `meta.stages`.

## Warning summary

After the run, print a grouped summary of `meta.warnings` from the final
Transcript: count per code, then the individual entries with their `wid`
ranges and `detail`. Hebrew goes to console only after
`sys.stdout.reconfigure(encoding="utf-8")`.

Warnings never change the exit code. A run that produced 40 `cps_exceeded`
warnings succeeded — it just tells you the source audio is dense.

## Failure behaviour

- A stage raising anything → print the exception with the module name, leave
  every artifact already written in place (they are the debugging trail), and
  exit non-zero.
- A `ContractError` at a stage boundary → same, and say which boundary.
- Never catch an exception and continue to the next stage. There is no partial
  pipeline run.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | pipeline completed; warnings may exist |
| 1 | a stage raised, including `ContractError` and `StageAlreadyRun` |
| 2 | bad invocation — missing input file, unreadable path, unknown stage name |

## Acceptance criteria

- On a synthetic pipeline with every stage stubbed, all four artifacts exist
  on disk in order, and each one validates against the contract.
- A stage stubbed to raise leaves the earlier artifacts on disk, prints the
  module name, and exits 1. The later artifacts do not exist.
- `--from segment` does not call transcribe or proofread and reads
  `02_proofread.json` from disk.
- `--force` reaches every stage; without it, a second full run over an
  existing job exits 1 with `StageAlreadyRun`.
- The warning summary reports every warning present in the final Transcript,
  grouped by code, and the exit code stays 0.
- `main` contains no Hebrew-specific string, no timing arithmetic, and no
  subtitle constant. Worth a grep in review.

Per D14 this module is **not** blocked on real fixtures: it is tested with
stubbed stages.

## Explicitly out of scope

Any decision a module could make. Batch processing of multiple files. Progress
bars. Retry logic.

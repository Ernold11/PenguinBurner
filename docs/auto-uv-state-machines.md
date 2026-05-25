# Auto-UV State Machines

This document describes the Auto-UV main loop after the readable `auto_uv`
rewrite. The legacy `auto_uv` package has been removed after its behavior was
ported into smaller named modules.

## Rendered Diagrams

![Shared scan state machine](assets/auto-uv-shared-scan.svg)

![Candidate sweep state machine](assets/auto-uv-candidate-sweep.svg)

![Efficiency mode state machine](assets/auto-uv-efficiency-mode.svg)

![Performance mode state machine](assets/auto-uv-performance-mode.svg)

![Crash marker and unsafe cache state machine](assets/auto-uv-crash-cache.svg)

## Big Picture

Auto-UV is a guarded voltage descent:

1. Measure the stock loaded behavior.
2. Flatten the V/F curve at the chosen clock and start voltage.
3. Probe lower real voltage bins.
4. Accept stable candidates and write the latest verified curve.
5. Recover when failures are explainable.
6. Stop before the search becomes unsafe or unproductive.
7. Run final verification and archive the selected profile.

The candidate sweep is mode-specific only after a probe has been accepted.
Efficiency mode asks "did FPS/W stop improving?" Performance mode asks "did the
performance score stop improving?"

## Shared Scan State Machine

```mermaid
stateDiagram-v2
    [*] --> InitialCheck
    InitialCheck --> NormalizeSettings
    NormalizeSettings --> ConsumeCrashMarker
    ConsumeCrashMarker --> OpenGpuApis
    OpenGpuApis --> ReadBaseCurve
    ReadBaseCurve --> RestoreRuntimeDefaults
    RestoreRuntimeDefaults --> DiscoveryProbe
    DiscoveryProbe --> ChooseBaselineLock
    ChooseBaselineLock --> FlattenBaselineCurve
    FlattenBaselineCurve --> BaselineProbe

    BaselineProbe --> BaselineAccepted: probe passed
    BaselineProbe --> BaselineRecovery: flattened baseline failed
    BaselineRecovery --> BaselineAccepted: higher voltage passed
    BaselineRecovery --> Abort: no stable baseline

    BaselineAccepted --> CandidateSweep
    CandidateSweep --> OptionalFinalChoice: sweep stopped with stable curve
    OptionalFinalChoice --> FinalVerify
    FinalVerify --> SaveProfile: final probe passed
    FinalVerify --> FinalRecovery: final probe failed recoverably
    FinalRecovery --> SaveProfile: recovered final passed
    FinalRecovery --> Abort: no final stable curve
    SaveProfile --> Cleanup
    Abort --> Cleanup
    Cleanup --> [*]
```

### Shared State Notes

- `InitialCheck` checks that the GPU, driver, Q2RTX setup, and validation policy are usable.
- `NormalizeSettings` resolves mode, max voltage drop, final duration, short probe duration, and OC budget.
- `ConsumeCrashMarker` loads a stale probe marker from the previous run and may add a persistent unsafe entry.
- `ReadBaseCurve` gets editable Linux NVAPI V/F points and validates that the base curve is usable.
- `DiscoveryProbe` measures stock loaded FPS, power, temperature, fan, clock, and voltage.
- `ChooseBaselineLock` chooses the start voltage and sustained loaded clock to preserve.
- `FlattenBaselineCurve` builds the first fixed-clock undervolt curve.
- `BaselineProbe` verifies that the baseline curve is actually stable before descending.
- `CandidateSweep` is the main algorithm loop described below.
- `OptionalFinalChoice` can ask the UI/user to choose among verified candidates.
- `FinalVerify` runs the long verification pass before a profile is archived.
- `Cleanup` restores runtime defaults and releases clock locks even after errors.

## Candidate Sweep State Machine

```mermaid
stateDiagram-v2
    [*] --> PickLowerVoltage
    PickLowerVoltage --> StopNoVoltage: no lower bin
    PickLowerVoltage --> SkipUnsafe: blacklisted voltage/clock band
    SkipUnsafe --> PickLowerVoltage: try next lower bin
    PickLowerVoltage --> BuildCandidate

    BuildCandidate --> PreemptiveOcBudget: predicted clock floor miss
    PreemptiveOcBudget --> ProbeCandidate
    BuildCandidate --> ProbeCandidate

    ProbeCandidate --> AcceptCandidate: stable and guardrails pass
    ProbeCandidate --> AcceptLowestFloorMiss: workload passed, clock floor missed, budget spent
    ProbeCandidate --> TryOcBudget: low loaded clock and budget remains
    ProbeCandidate --> RecoverUpward: hard failure or guardrail rejection
    ProbeCandidate --> StopCritical: critical failure

    TryOcBudget --> ProbeOcBudget
    ProbeOcBudget --> AcceptCandidate: OC-budget targeted candidate stable
    ProbeOcBudget --> TryOcBudget: still low clock and budget remains
    ProbeOcBudget --> BackoffOcBudget: OC-budget target caused hard failure
    BackoffOcBudget --> RecoverUpward
    ProbeOcBudget --> StopCritical: critical failure

    RecoverUpward --> AcceptRecovered: higher voltage stable
    RecoverUpward --> StopKeepPrevious: recovery failed

    AcceptCandidate --> ModeBehavior
    AcceptRecovered --> PickLowerVoltage
    ModeBehavior --> PickLowerVoltage: continue search
    ModeBehavior --> StopModeWall: efficiency/performance wall
    AcceptLowestFloorMiss --> StopKeepCurrent

    StopNoVoltage --> [*]
    StopKeepPrevious --> [*]
    StopKeepCurrent --> [*]
    StopCritical --> [*]
    StopModeWall --> [*]
```

### Candidate Selection Rules

- The next voltage is a lower editable bin from the base V/F curve.
- The default target follows the base curve downward instead of pinning the original clock forever.
- After a stable probe, the next target may use the measured loaded clock, not only the requested target.
- A recent accepted voltage/clock slope can predict that the next lower voltage will miss the clock floor.
- If a floor miss is predicted and budget remains, the candidate can spend OC budget before probing.
- Persistent OC budget carries forward so later lower-voltage probes do not lose recovered clock.
- When full budget is spent, the first full-budget target is fixed for future lower-voltage probes.
- Unsafe cache entries block the failed voltage and lower voltages only in the failed clock band when clock data exists.

## Probe Decision State Machine

```mermaid
stateDiagram-v2
    [*] --> ProbeResult
    ProbeResult --> Accept: success and no evaluation error
    ProbeResult --> AcceptLowestFloorMiss: success with final acceptable clock miss
    ProbeResult --> RejectGuardrail: success but FPS/frame/power/clock guardrail failed
    ProbeResult --> TryOcBudget: failed only because loaded clock was low
    ProbeResult --> RecoverUpward: failed from CUDA/Q2RTX/stall/regression
    ProbeResult --> StopCritical: fatal output, Xid, load lost, invalid timedemo, timeout

    TryOcBudget --> [*]
    RecoverUpward --> [*]
    StopCritical --> [*]
    RejectGuardrail --> [*]
    AcceptLowestFloorMiss --> [*]
    Accept --> [*]
```

## Efficiency Mode State Machine

Efficiency mode is FPS/W-first. It accepts lower voltage while efficiency
improves, then confirms the efficiency wall before stopping.

```mermaid
stateDiagram-v2
    [*] --> StableAccepted
    StableAccepted --> MeasureEfficiencyDelta
    MeasureEfficiencyDelta --> Continue: FPS/W improved
    MeasureEfficiencyDelta --> ArmStop: FPS/W did not improve or power rose while efficiency fell

    ArmStop --> Continue: minimum voltage drop not reached
    ArmStop --> TryEfficiencyOcBudget: minimum drop reached and OC budget remains
    TryEfficiencyOcBudget --> Continue: OC-budget target restored FPS/W gain
    TryEfficiencyOcBudget --> ConfirmWall: OC-budget target rejected or no FPS/W gain

    ArmStop --> ConfirmWall: budget already spent
    ConfirmWall --> Continue: confirmation streak not reached
    ConfirmWall --> StopAtPrevious: confirmed regression
    ConfirmWall --> StopAtCurrent: tiny non-negative delta and no power regression

    Continue --> [*]
    StopAtPrevious --> [*]
    StopAtCurrent --> [*]
```

Efficiency mode state:

- `no_gain_streak` counts accepted probes that did not improve FPS/W.
- `pending_stop_candidate` stores the previous stable curve when the wall is first seen.
- `min_efficiency_stop_voltage_drop_pct` prevents stopping too close to the start voltage.
- `efficiency_stop_streak` requires extra confirmation after the first no-gain probe.
- Available OC budget delays the final stop so the algorithm can try to recover lost clock first.

## Performance Mode State Machine

Performance mode still descends voltage, but it scores candidates by FPS first
and FPS/W second. It is allowed to spend more OC budget and can sweep back
up in voltage to recover a better high-performance point.

```mermaid
stateDiagram-v2
    [*] --> StableAccepted
    StableAccepted --> ScoreCandidate
    ScoreCandidate --> RecordBest: score improved
    ScoreCandidate --> CountNoGain: score did not improve

    CountNoGain --> TryPerformanceOcBudget: budget remains
    TryPerformanceOcBudget --> RecordBest: OC-budget target improves score
    TryPerformanceOcBudget --> Continue: OC-budget target stable but no score gain
    TryPerformanceOcBudget --> VoltageRecovery: OC-budget target hard-failed

    CountNoGain --> Continue: exploration budget remains
    CountNoGain --> VoltageRecovery: no-gain streak reached
    VoltageRecovery --> StopAtRecovered: higher voltage improves score
    VoltageRecovery --> StopAtBest: no higher-voltage recovery improves score

    RecordBest --> Continue
    Continue --> [*]
    StopAtRecovered --> [*]
    StopAtBest --> [*]
```

Performance mode state:

- `best_candidate`, `best_probe`, and `best_score` remember the best observed result.
- `no_score_gain_steps` counts accepted probes that did not beat the best score.
- `min_exploration_steps` prevents stopping before enough lower-voltage points are sampled.
- `max_no_score_gain_steps` stops the descent after repeated non-improvements.
- `max_OC-budget target_step_pct` caps one performance recovery jump.
- `max_voltage_recovery_mv` and `max_voltage_recovery_probes` bound the upward voltage sweep.

## Crash And Unsafe Cache State Machine

```mermaid
stateDiagram-v2
    [*] --> ProbeStart
    ProbeStart --> WriteInProgressMarker
    WriteInProgressMarker --> ProbeRunning
    ProbeRunning --> ClearMarker: clean exit
    ProbeRunning --> ProcessDies: crash, reboot, power loss, forced kill
    ClearMarker --> [*]

    ProcessDies --> NextRun
    NextRun --> ValidateMarker
    ValidateMarker --> IgnoreMarker: not aggressive enough or incomplete
    ValidateMarker --> RecordUnsafe: voltage drop and OC budget thresholds met
    RecordUnsafe --> LoadUnsafeEntries
    LoadUnsafeEntries --> BlockFutureCandidates
    LoadUnsafeEntries --> CapRecoveryBudget: recovery probe crashed
    IgnoreMarker --> [*]
    BlockFutureCandidates --> [*]
    CapRecoveryBudget --> [*]
```

Crash and unsafe rules:

- Candidate, candidate-OC-budget, stabilize, final-OC-budget, and final-verify probes write an in-progress marker.
- Clean probe exit clears the marker.
- A stale marker is treated as a crash only if it passes crash-cache validation.
- Crash-cache validation currently requires at least `5%` voltage drop and `110%` OC budget.
- Explicit failed probes can also record unsafe voltage entries when the failure reason is unsafe enough.
- Clock-aware unsafe entries block the failed voltage and lower voltages only for the failed clock band.
- Recovery crashes can cap future OC budget before the failed recovery attempt.

## Outputs

- Latest short verified curve: `uv-result/auto-uv-latest-verified.json`
- Verified candidate list: `uv-result/auto-uv-verified-candidates.json`
- Unsafe voltage cache: `uv-result/auto-uv-unsafe-voltages.json`
- Active probe marker: `uv-result/auto-uv-probe-in-progress.json`
- Final archived profiles: `auto-uv-profiles/`
- Auto-UV stdout/stderr logs: `debug-logs/`

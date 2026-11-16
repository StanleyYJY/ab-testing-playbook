"""A/B testing playbook: each design or analysis decision is backed by a simulation.

Setting: an operations team tests changes to an internal task workflow (for example, earlier
reminders for tasks close to their deadline). The primary metric is on-time completion, a
proportion. Tasks are handled by employees in teams, so tasks are not independent.

Run:  uv run python playbook.py   -> results/report.md and results/figures/*.png
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from scipy import stats
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import confint_proportions_2indep, proportion_effectsize, proportions_ztest

OUT = Path(__file__).resolve().parent / "results"
FIG = OUT / "figures"
rng = np.random.default_rng(20260116)
md: list[str] = []


def section(title: str, intro: str) -> None:
    md.append(f"\n## {title}\n\n{intro}\n")


# 1. Power and sample size ------------------------------------------------------------------
def power_analysis() -> None:
    section("1. Size the test before you run it",
            "Baseline on-time completion is 81%. How many tasks per arm are needed to detect a given "
            "absolute lift with 80% power at a two-sided 5% level?")
    rows = []
    for mde in [0.01, 0.02, 0.03, 0.05]:
        es = proportion_effectsize(0.81 + mde, 0.81)
        n = NormalIndPower().solve_power(effect_size=es, alpha=0.05, power=0.8, ratio=1.0)
        rows.append({"Minimum detectable lift": f"{mde*100:.0f} pts", "Tasks per arm": f"{int(np.ceil(n)):,}"})
    md.append(pd.DataFrame(rows).to_markdown(index=False, disable_numparse=True))
    md.append("\nHalving the detectable lift roughly quadruples the sample. Pick the smallest lift that "
              "would change the decision, not the smallest lift you can imagine. Under cluster randomization "
              "(section 3), multiply by the design effect.")


# 2. Sample ratio mismatch -----------------------------------------------------------------
def srm() -> None:
    section("2. Check the split before reading results (sample ratio mismatch)",
            "A 50/50 test that lands 10,000 vs 10,500 tasks looks close. A chi-square test against the "
            "planned split says otherwise.")
    for a, b in [(10_000, 10_090), (10_000, 10_500)]:
        chi2, p = stats.chisquare([a, b], f_exp=[(a + b) / 2] * 2)
        md.append(f"- {a:,} vs {b:,}: chi-square p = {p:.3g} → {'investigate before analysis' if p < 0.001 else 'consistent with 50/50'}")
    md.append("\nUse a strict threshold (p < 0.001) because this check runs on every test. A mismatch usually "
              "means assignment or logging is broken, and the treatment effect cannot be trusted until it is fixed.")


# 3. Cluster randomization -----------------------------------------------------------------
def cluster_sim() -> None:
    section("3. Randomize at the level where outcomes are correlated",
            "Tasks are handled by 60 teams whose on-time rates differ. If whole teams are assigned to an arm "
            "but the analysis treats every task as independent, false positives explode. 1,000 simulated "
            "A/A tests (no true effect), 60 teams × 80 tasks:")
    n_sims, teams, per_team, team_sd = 1000, 60, 80, 0.6
    naive, robust = 0, 0
    base_logit = np.log(0.81 / 0.19)
    for _ in range(n_sims):
        eff = rng.normal(0, team_sd, teams)
        arm = rng.permutation(np.repeat([0, 1], teams // 2))
        team = np.repeat(np.arange(teams), per_team)
        y = rng.random(teams * per_team) < 1 / (1 + np.exp(-(base_logit + eff[team])))
        x = arm[team]
        _, p_naive = proportions_ztest([y[x == 1].sum(), y[x == 0].sum()], [(x == 1).sum(), (x == 0).sum()])
        fit = sm.OLS(y.astype(float), sm.add_constant(x)).fit(cov_type="cluster", cov_kwds={"groups": team})
        naive += p_naive < 0.05
        robust += fit.pvalues[1] < 0.05
    md.append(pd.DataFrame([
        {"Analysis": "Task-level z-test (wrong)", "False positive rate": f"{naive / n_sims:.1%}"},
        {"Analysis": "Cluster-robust standard errors by team", "False positive rate": f"{robust / n_sims:.1%}"},
    ]).to_markdown(index=False, disable_numparse=True))
    md.append("\nThe nominal rate is 5%. Cluster-robust errors land slightly above it here because 60 clusters is a "
              "small number; a small-sample correction or a cluster bootstrap closes most of the gap. "
              "The fix is to analyze at the unit of randomization: cluster-robust "
              "errors, a team-level analysis, or a cluster bootstrap. Better still, randomize individuals when "
              "spillover allows it, because cluster designs need far more tasks for the same power.")


# 4. CUPED --------------------------------------------------------------------------------
def cuped() -> None:
    section("4. Use pre-period data to cut variance (CUPED)",
            "Processing time per employee is noisy, but it is strongly correlated with the same employee's "
            "time before the test. Adjusting for it shrinks the confidence interval without more traffic. "
            "500 simulated tests, 2,000 employees, true effect −0.4 hours:")
    widths_raw, widths_cuped, rho = [], [], 0.7
    for _ in range(500):
        n = 2000
        pre = rng.normal(17, 4, n)
        noise = rng.normal(0, 4 * np.sqrt(1 - rho ** 2), n)
        arm = rng.integers(0, 2, n)
        post = 17 + rho * (pre - 17) + noise - 0.4 * arm
        theta = np.cov(post, pre)[0, 1] / np.var(pre, ddof=1)
        adj = post - theta * (pre - pre.mean())
        for series, store in [(post, widths_raw), (adj, widths_cuped)]:
            d = series[arm == 1].mean() - series[arm == 0].mean()
            se = np.sqrt(series[arm == 1].var(ddof=1) / (arm == 1).sum() + series[arm == 0].var(ddof=1) / (arm == 0).sum())
            store.append(2 * 1.96 * se)
    red = 1 - (np.mean(widths_cuped) / np.mean(widths_raw)) ** 2
    md.append(pd.DataFrame([
        {"Estimator": "Difference in means", "Avg 95% CI width (hours)": f"{np.mean(widths_raw):.2f}"},
        {"Estimator": "CUPED-adjusted", "Avg 95% CI width (hours)": f"{np.mean(widths_cuped):.2f}"},
    ]).to_markdown(index=False, disable_numparse=True))
    md.append(f"\nVariance reduction: {red:.0%} (theory: ρ² = {rho**2:.0%}), equivalent to running the test on "
              f"about {1/(1-red):.1f}× the sample. The covariate must be measured before assignment.")


# 5. Peeking ------------------------------------------------------------------------------
def peeking() -> None:
    section("5. Don't stop the test the first time p < 0.05",
            "Checking a fixed-horizon test every day and stopping at the first significant result inflates "
            "false positives. 4,000 simulated A/A tests, 28 daily looks, 300 tasks per arm per day:")
    sims, days, per_day, p0 = 4000, 28, 300, 0.81
    a = rng.binomial(per_day, p0, (sims, days)).cumsum(1)
    b = rng.binomial(per_day, p0, (sims, days)).cumsum(1)
    n = per_day * np.arange(1, days + 1)
    pooled = (a + b) / (2 * n)
    z = (a / n - b / n) / np.sqrt(pooled * (1 - pooled) * 2 / n)
    sig = np.abs(z) > 1.96
    fixed = sig[:, -1].mean()
    any_look = sig.any(1).mean()
    md.append(pd.DataFrame([
        {"Decision rule": "Read once at day 28 (planned)", "False positive rate": f"{fixed:.1%}"},
        {"Decision rule": "Stop at first daily p < 0.05", "False positive rate": f"{any_look:.1%}"},
    ]).to_markdown(index=False, disable_numparse=True))
    fig, ax = plt.subplots(figsize=(6, 3.6))
    ax.plot(np.arange(1, days + 1), sig.cumsum(1).clip(0, 1).mean(0) * 100)
    ax.axhline(5, ls=":", c="grey")
    ax.set_xlabel("days of daily peeking"); ax.set_ylabel("% of A/A tests declared significant")
    ax.set_title("False positives grow with every look"); fig.tight_layout()
    fig.savefig(FIG / "peeking.png", dpi=150)
    md.append("\n![Peeking](figures/peeking.png)\n\nIf you need to monitor, use a sequential method designed "
              "for it (alpha spending or always-valid p-values) and monitor guardrails, not the primary metric.")


# 6. Multiple testing ---------------------------------------------------------------------
def multiple_testing() -> None:
    section("6. Many metrics, one decision: control the error rate",
            "A readout with 10 secondary metrics and no real effects will usually show something "
            "\"significant\". 5,000 simulated readouts:")
    sims, k = 5000, 10
    p = rng.random((sims, k))
    rows = []
    for name, method in [("No correction", None), ("Bonferroni", "bonferroni"), ("Holm", "holm"), ("Benjamini–Hochberg", "fdr_bh")]:
        if method is None:
            any_fp = (p < 0.05).any(1).mean()
        else:
            any_fp = np.mean([multipletests(row, alpha=0.05, method=method)[0].any() for row in p])
        rows.append({"Method": name, "P(at least one false positive)": f"{any_fp:.1%}"})
    md.append(pd.DataFrame(rows).to_markdown(index=False, disable_numparse=True))
    md.append("\nDecide on one primary metric before launch. Treat secondary metrics as context, corrected with "
              "Holm when a decision depends on them, or Benjamini–Hochberg when screening many.")


# 7. A complete readout -------------------------------------------------------------------
def readout() -> None:
    section("7. A complete readout",
            "One simulated test of earlier reminders, randomized by employee (no shared queues), "
            "4,000 tasks per arm. The guardrail is notifications per employee per week.")
    n = 4000
    ctrl = rng.random(n) < 0.81
    trt = rng.random(n) < 0.865
    lo, hi = confint_proportions_2indep(trt.sum(), n, ctrl.sum(), n, compare="diff", method="newcomb")
    _, p = proportions_ztest([trt.sum(), ctrl.sum()], [n, n])
    notif_c, notif_t = rng.poisson(6.0, 400), rng.poisson(6.3, 400)
    g = stats.ttest_ind(notif_t, notif_c, equal_var=False)
    md.append(pd.DataFrame([
        {"Metric": "On-time completion (primary)", "Control": f"{ctrl.mean():.1%}", "Treatment": f"{trt.mean():.1%}",
         "Difference (95% CI)": f"{(trt.mean()-ctrl.mean())*100:+.1f} pts ({lo*100:+.1f} to {hi*100:+.1f})", "p": f"{p:.2g}"},
        {"Metric": "Notifications per employee-week (guardrail)", "Control": f"{notif_c.mean():.2f}",
         "Treatment": f"{notif_t.mean():.2f}", "Difference (95% CI)": f"{notif_t.mean()-notif_c.mean():+.2f}", "p": f"{g.pvalue:.2g}"},
    ]).to_markdown(index=False, disable_numparse=True))
    md.append("\nRecommendation template: ship if the primary metric's interval excludes zero and the guardrail "
              "moves less than the pre-agreed tolerance (here, +10% notifications). Report the interval, not just "
              "the p-value, and state what the test cannot tell you (for example, effects beyond the test window).")


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    md.append("# A/B Testing Playbook: Results\n\nAll numbers come from simulations in `playbook.py` (seeded, so "
              "they reproduce exactly).")
    for step in [power_analysis, srm, cluster_sim, cuped, peeking, multiple_testing, readout]:
        step()
        print("done:", step.__name__)
    (OUT / "report.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()

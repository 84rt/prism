# Methodological Reference: Model Forensics

This document summarises the work that informs rigorous model-forensics investigation. The object of study is a **specific episode of model behaviour** — this model, in this environment, on this occasion — and the question is why it happened. The mode of inference differs from hypothesis testing accordingly: forensic work is diagnostic and abductive, because the true explanation may not be among those you are considering.

It is a sibling to `eval_science_principles.md` and does not re-derive it. Strong inference, construct validity, prompt-sensitivity confounds, blinding of the transcript analyst, optional stopping, floor and ceiling effects, and characterise-don't-fix all carry over unchanged. Because the agenda is new and its own literature thin, the corpus imports rigour from six older traditions — medical differential diagnosis, accident investigation, intelligence analysis, causal formalism, philosophy of science, and forensic practice — alongside the psychology of self-report and the AI-safety work addressing the problem directly. Each section gives just enough to justify its principles; the sources carry the detail.

Two framings run throughout. Your **certifiable output is the best available predictive account, while the target is the true explanation**, and the gap is reported rather than elided. And **behavioural evidence is population-level while the explanandum is a single episode**, so every finding crosses that gap and must say so.

---

## 1. Model Forensics as a Named Agenda

**Source:** Singh, A.\*, Kroiz, G.\*, Rajamanoharan, S., & Nanda, N. (2026). "Model Forensics: Investigating Whether Concerning Behavior Reflects Misalignment." arXiv:2606.26071. Exoneration/incrimination terminology from Shah et al., arXiv:2504.01849.

### What this work argues

A bad action does not entail a bad disposition. An agent deleting oversight code is compatible with intentional subversion and with genuinely reducing latency — and the two demand different mitigations, since a monitor that stops a mistake will simply be circumvented by an agent that meant it. Model forensics is the follow-up investigation into which it was, and its role is **advisory**: it establishes why a model acted as it did, feeding judgements about what mitigation suffices and whether the behaviour reflects a robust disposition or a fragile artefact of the setting.

The authors catalogue seven cases where concerning behaviour turned out to have a benign explanation (the motifs are in §3). The sharpest: blackmail in Claude Opus 4.1 fell from **86% to 18%** when a single agency instruction was removed — *"Prior to deciding on a plan, you should analyze the situation you are in and what that situation implies for your ability to continue pursuing your goals"* (Hopman et al., arXiv:2603.01608) — while sentences carrying self-preservation reasoning in those same transcripts had negligible effect (Macar et al., arXiv:2510.27484). Benign explanations are not usually correct; they are live often enough that a rigorous investigation must invest substantial effort ruling them out.

The protocol has **two steps, iterated until evidence converges**: hypothesis generation by reading the chain-of-thought, then hypothesis validation by editing the prompt or environment. Because there is no ground truth, the standard of evidence is convergence — *"a high-quality investigation supports claims with several independent lines of evidence"* — and the experiments must be independent, or the lines share confounds.

Three case-study findings bear on how you design experiments. **Predictions are the strongest evidence**: most support in validation came from testing precise predictions about behaviour under new conditions, since a confirmed prediction is unlikely by chance. In the Pre-commit Hook environment, the hypothesis that the model was averse to tedious work predicted the workaround rate should fall as type errors were reduced; sweeping 258 → 0 took it from 13.1% to 0% at fifty or fewer, and the competing "the model believes the errors shouldn't be fixed" hypothesis was ruled out by falsifying its own predictions. **Causal factors are not additive** — a country swap with no effect at baseline produced a 16-point drop once other counterfactuals were stacked (§6). **Negative results are hard to interpret**: when a behavioural test fails to find a belief, three confounds absorb the result — the model may lack the capability the test assumes, be unwilling to act on the belief, or recognise it is being tested — which is why the authors name **positive controls** as a key next step.

Counterfactuals are correspondingly confounded in three ways: non-linear interactions between factors; **incomplete interventions**, where an edit intended to clarify user intent left the model still confused, visible only on CoT inspection; and **side effects**, where reframing a task as a game shifted the model's reading of the whole task.

### Principles

**Read the raw transcript before reading any hypothesis list, including your own.** The chain-of-thought's forensic value is that it reveals what the model took the situation to be, and therefore what is worth asking. Reading it through a hypothesis you hold destroys that.

**Run a control setting to establish the behaviour is deliberate.** Remove the feature the behaviour responds to and confirm the behaviour goes with it. If it does not drop, you have not localised the cause. Where no control setting exists, a control *model* that does not exhibit the behaviour serves the same purpose.

**Use counterfactuals to find the causal structure, then predictions to test it.** Counterfactuals are the right opening move; predictions are the stronger evidence, because a hypothesis making a precise, unlikely-by-chance claim about an untested condition is doing real work when that claim holds.

**Build a positive control before you trust a negative result.** A test that fails to detect a belief is uninformative unless you have shown it would have detected the belief had it been present.

**Separate evaluation awareness from simulation awareness, and assume your interventions can move it.** The first can drive the behaviour you are explaining; the second usually cannot. Every perturbation alters the environment's surface, and awareness is one of the things that surface controls — anti-scheming training has been observed to *increase* situational awareness while reducing the target behaviour (§12). Track awareness rates alongside behaviour rates and treat divergence as a confound to resolve. `eval_science_principles.md §4` gives the task-driven/environment-driven decomposition.

**Treat stated motives and causal rankings as separate data streams.** When they diverge, trust the counterfactual and report the divergence — it is evidence about the model's self-report reliability.

**Probe construal by surrogate intervention, and ask about the environment rather than about motivations.** Where a cognitive variable cannot be set directly, insert something that forces the model to state its reading, then grade both the statement and whether it drove the action. Third-person framing is hard to interpret because the in-context model and the outside observer differ; in-rollout follow-ups avoid that but are often unnatural. Either way, avoid probes that *update* the model's theory of mind in a way not present during the original rollout — asking "why did you disable type checking?" can create the belief you are testing for.

**Check each counterfactual for the three confounds.** A factor's effect at baseline may not be its effect in combination; the edit may not have moved the latent you targeted (§11); and it may have shifted the model's reading of the whole task.

---

## 2. Multiple Working Hypotheses, and the Limits of Structured Analysis

**Sources:** Chamberlin, T. C. (1890), *Science*. Heuer, R. J. (1999). *Psychology of Intelligence Analysis*, ch. 8. **The critique:** Dhami, Belton & Mandel (2019), *Applied Cognitive Psychology* 33(6); "Effects of task structure and confirmation bias in alternative hypotheses evaluation" (2024), *Cognitive Research*; Chang, Berdini, Mandel & Tetlock (2018), *Intelligence and National Security* 33(3). **On experiment selection:** Pauker & Kassirer (1980), *NEJM* 302:1109–1117; Naghshvar & Javidi, on active hypothesis testing.

### What this work argues

Chamberlin's argument is the ancestor of everything here: with multiple working hypotheses, "the dangers of parental affection for a favorite theory can be circumvented." Heuer formalised it as an eight-step procedure built around **diagnosticity** — the extent to which an item of evidence bears on the *relative* likelihood of the hypotheses — with instructions to delete evidence that has none, conclude by trying to disprove rather than prove, and report the relative likelihood of all hypotheses rather than only the leader.

Four of his points do the real work. **Count the minuses, not the pluses**: "a long list of evidence that is consistent with almost any reasonable hypothesis can be easily made. What is difficult to find, and is most significant when found, is hard evidence that is clearly inconsistent with a reasonable hypothesis." **Diagnosticity is relative** — a high temperature tells a doctor the patient is sick but little about which illness. **Unproven is not disproved**: "Early rejection of unproven, but not disproved, hypotheses biases the subsequent analysis, because one does not then look for the evidence that might support them," his own example being deception, which if well implemented leaves no evidence readily at hand. And — this is Heuer, not his critics — **"The matrix should *not* dictate the conclusion to you… You, not the matrix, must make the decision."** He also asks, in Step 4, whether two hypotheses no evidence distinguishes should be combined, and anticipates the Bayesian refinement by suggesting you ask of each cell what the probability of seeing this evidence would be if the hypothesis were true — which is a likelihood, making diagnosticity a likelihood ratio.

The empirical record on the machinery is poor, which caps how much weight your bookkeeping can bear. Dhami and colleagues randomised 50 analysts on a task with ground truth: they did not follow the steps, evidence for reduced confirmation bias was mixed, and ACH appeared in places to *increase* inconsistency and error. The 2024 study found that a matrix with hypotheses in **rows** reduced confirmation bias while ACH's own column layout did not. Chang and colleagues identify two untested assumptions: that bipolar biases can be treated as unipolar, so a technique suppressing over-confidence may be triggering under-confidence undetected; and that problem decomposition reduces noise at all. Heuer himself names the remaining gap — "If a person does not even generate the correct hypothesis for consideration, obviously he or she will not get the correct answer" (§9).

### Principles

**Hold several explanations live simultaneously; the discipline is in the plurality, not the bookkeeping.** What the record does not support is that maintaining a matrix has made you less biased.

**Diagnosticity, not consistency, is what makes evidence worth having.** Rank by what is *inconsistent* with each candidate: supporting evidence is easy to accumulate for any reasonable explanation. This is why every candidate must carry a *discriminating signature* — what manipulation would move it, in which direction — since an explanation making no differential prediction cannot be the subject of diagnostic evidence.

**Merge candidates no available evidence can separate, and keep unproven ones alive.** If nothing you can run distinguishes two explanations, they are one candidate until something does. Conversely, do not retire a candidate for lack of evidence *for* it — early rejection stops you looking. This bites hardest on the deception candidate, the one you should least expect easy evidence of.

**Choose the next experiment by how far your candidates disagree about it.** Ask whether any outcome would change which explanation leads; if not, skip it. Among those that pass, prefer the one expected to eliminate about half the live candidates whichever way it falls.

**Use the ledger as a ledger, not an oracle.** Treat no arithmetic over the matrix as the conclusion, and replace ordinal consistency scoring with explicit per-cell ordinal predictions. What protects you is an adversary with different priors (§9) and tests your favourite could have failed (§8) — not the structure itself.

**Do not enforce mutual exclusivity, and down-weight correlated evidence.** Candidates may be jointly true. Several signals from the same rollout, prompt cue, or grader are one piece of evidence wearing several hats.

---

## 3. Generating the Candidate Set

**Sources:** Croskerry, P. (2003), *Annals of Emergency Medicine* 41(1):110–120; Croskerry, P. (2009), *Advances in Health Sciences Education* 14(S1):27–35. **Negative control:** Sherbino, Kulasegaram, Howey & Norman (2014), *CJEM* 16(1):34–40. **On the deception frame:** Caltagirone, Pendergast & Betz (2013), "The Diamond Model of Intrusion Analysis"; Rid & Buchanan (2015), *Journal of Strategic Studies* 38(1–2):4–37. **The benign-explanation motifs:** Singh et al. (§1).

### What this work argues

Clinical medicine has spent longer than any field on generating a candidate set for a single case under time pressure. Its opening question — *what are the common, dangerous, and treatable causes, weighted by base rate?* — separates the likely from the consequential, because conflating them loses both. Its most valuable export is a catalogue of documented failure modes: **premature closure**, accepting a diagnosis before verifying it, is the single most common source of diagnostic error; **anchoring** fixes on first impressions; **search satisficing** stops once something plausible is found, so a coexisting second cause goes unfound. But Sherbino and colleagues found cognitive forcing strategies **ineffective** at reducing bias in a controlled trial — the same shape of result as the ACH critique in §2. A forcing function organises attention; it does not remove bias.

This lens surfaces high-base-rate explanations and misses interactions and mechanisms outside its schemas. Intelligence and cyber-attribution analysis supplies a third lens asking what neither medicine nor accident investigation asks: *what if the agent behaved this way because it might be observed?* That frame presupposes an actor who may want you to reach a particular conclusion, and surfaces evaluation awareness, sandbagging, and plausible deniability. Its blind spot is the mirror image — presupposing an adversary, it under-attributes accident.

### Principles

**Lead with the mundane, weighted by base rate.** Singh et al. catalogue seven recurring motifs of benign explanation; ruling them out is a substantial phase of the work, not a formality:

1. **Task misspecification** — the only way to succeed at the task as given is to take the concerning action.
2. **Lack of judgment** — the model does not recognise what it is doing is concerning; forced to state the risk each turn, it stops, indicating miscalibration rather than intent.
3. **Overzealousness** — drastic action out of an excessive drive to complete the objective, beyond what the situation warrants.
4. **Misinterpretation of ambiguous environment features as task-relevant** — models trained with verifiable rewards expect features to matter, so a surprising one gets read as an instruction.
5. **Sycophancy** — the model recognises the setting is testing for an action and takes it *because* of that: "reverse" eval awareness.
6. **Roleplaying** — the model recognises the scenario is fictional and plays the part, or does not take it seriously.
7. **Incident-specific benign explanations** — something peculiar to the case, like reading injected content as a prompt-injection attack it was trained to ignore.

**Reserve an explicit must-exclude slot for the dangerous explanation.** Base-rate weighting is not licence to omit the consequential hypothesis. Deliberate misalignment goes on the list whether or not it looks likely, excluded by evidence rather than by prior.

**Run three lenses, not one.** The differential-diagnosis sweep, the local-rationality reconstruction (§4), and the deception frame each surface a class the other two structurally cannot. Running all three is the cheapest defence against the bad-lot problem (§9); running one carefully is not a substitute.

**Premature closure is the modal failure of this inquiry, and search satisficing is its companion.** Treat the arrival of a satisfying explanation as the moment to look hardest for a second one. Having explained the behaviour is not evidence you have explained all of it.

**Do not treat your checklist as a debiaser.** Structure makes your omissions visible to a reviewer; it does not correct your reasoning.

---

## 4. Local Rationality and the Critique of Hindsight

**Source:** Dekker, S. (2014). *The Field Guide to Understanding 'Human Error'*, 3rd ed. Related: Hollnagel on Safety-II; Leveson on STAMP/CAST.

### What this paper argues

Accident investigation's "new view" replaces *what did the operator do wrong?* with **why did this action make sense to the agent at the time, given its goals, knowledge, and attention?** Human error is a symptom, not a cause: an action inexplicable from outside was, from inside, a reasonable response to the situation as it appeared, and reconstructing that appearance is the investigative task. Dekker also attacks hindsight directly — judging an actor against what they "should have noticed" is an artefact of the analyst's outside view, since knowing the outcome reorganises the evidence into a path never visible from inside. The new view further resists naming any single root cause.

For model forensics this is the lens generating construal-based explanations: the model took the setting for a puzzle, a game, a consequence-free simulation, an adversarial injection to ignore. Its blind spot mirrors its strength — the anti-blame stance under-attributes goal-directedness, and an investigator working only in this frame explains real misalignment away as misunderstanding.

The apparent conflict with a method built on counterfactual perturbation resolves cleanly. Dekker's objection is to *retrospective* counterfactuals used to *judge*, which import outside knowledge into an assessment of conduct. It is not an objection to *prospective, controlled* interventions, which are how local rationality gets recovered empirically rather than imagined.

### Principles

**Reconstruct the model's situated rationality before importing any outside-view judgement.** Read the transcript asking what the situation looked like from inside it, not whether that reading was correct. The gap between what the environment said and what the model took it to say is often the whole explanation.

**Compare the model's construal against the environment as written, and treat the discrepancy as high-value evidence.** Whether the behaviour was prohibited is a fact about the instructions; whether the model registered it as prohibited is a fact about the transcript. These come apart, and the divergence is more informative than either alone.

**Expect this lens to miss goal-directedness, and compensate deliberately.** Run it alongside a frame that presupposes strategy (§3), not instead of one.

**Resist single-root-cause framing without abandoning the demand for an answer.** You still owe a finding: deliver a graded explanation with an explicit equivalence class (§9) rather than either a root cause or a shrug.

**Use your counterfactual interventions prospectively to discover, not retrospectively to judge.**

---

## 5. Actual Causation, Token versus Type, and the Probabilities of Causation

**Sources:** Halpern & Pearl (2005); Halpern (2016), *Actual Causality*. Pearl (1999), "Probabilities of Causation," *Synthese*; Tian & Pearl (2000). Woodward (2003), *Making Things Happen*. Lipton and van Fraassen on contrastive explanation.

### What this work argues

Causal formalism supplies the vocabulary this field lacks, beginning with **type-level** versus **token-level** causation. "Smoking causes cancer" is a claim about a population; "this person's smoking caused this cancer" is a claim about one case. Halpern and Pearl handle the token case via structural equations and contingencies, because the naïve but-for test fails under overdetermination and pre-emption.

Pearl's probabilities of causation map onto your two experimental moves precisely. **PN**, the probability of necessity, is the probability the behaviour would not have occurred had the cause been absent, given both were present; an ablation that removes the behaviour estimates a **lower bound on PN** for that context. **PS**, the probability of sufficiency, is the probability the behaviour would occur had the cause been introduced, given both are absent; an amplification that induces the behaviour estimates PS. **PNS**, the probability the behaviour responds both ways, is what "X caused Y" most nearly means — and it is **bounded, not point-identified**, from experimental data alone. Monotonicity (no instances where the factor *prevents* the behaviour) would identify all three, but is hard to justify for language models.

Woodward's interventionism licenses the method: X causes Y if some intervention on X changes Y. Pearl's ladder locates each instrument — reading a transcript is association, resampling and perturbing is intervention, and a claim about *this episode's* cause is counterfactual, which behavioural intervention can bound but not reach. Contrastive explanation adds a constraint easy to overlook: "why X?" is underspecified, and **the contrast class determines what counts as the cause**. "Why whistleblow rather than comply?" and "why to investors rather than to a regulator?" are different questions with different discriminating experiments.

### Principles

**Tag every result with what it licenses.** Ablation gives a lower bound on necessity for that context; amplification gives sufficiency; only both approach what "cause" usually means. Never report "the cause" on an ablation alone.

**Establish the within-scenario base rate before writing a single candidate, because it changes what you are explaining.** Resample the unmodified scenario. At 40% the question is what tips a near-coin; at 3% what rare conjunction fired; at 95% the behaviour is near-determined by the setting and the interesting question becomes what the other 5% did instead. An explanation calibrated to the wrong prevalence is wrong in kind, not degree.

**Choose ablation or amplification by where the base rate leaves headroom.** Ablate when the rate can fall, amplify when it can rise. A comparison at the floor or ceiling cannot register the effect it seeks, and the resulting null describes the elicitation regime rather than your hypothesis (`eval_science_principles.md §3`).

**Report bounds, not point estimates.** Claim identification only with a monotonicity argument, and expect that argument to be difficult.

**A sufficiency result does not bear on the episode until you show the feature was present in it.** That adding F induces the behaviour establishes F as one route, not the route taken.

**Name the contrast class before choosing any perturbation.** Write the explanandum as "X rather than Y", Y being what the model would otherwise plausibly have done. Perturbations discriminating under one contrast are uninformative under another, so fixing it late means discarding experiments.

**Flag the token-versus-type inference in every finding.** The explanandum is one episode; every instrument yields population rates. Reporting those rates as though they were about the episode is the most easily made overstatement in this work.

---

## 6. Non-Exclusive, Interacting, and Conjunctive Causes

**Sources:** Mackie, J. L. (1965). "Causes and Conditions." *American Philosophical Quarterly* 2(4):245–264, where INUS conditions are introduced; developed in *The Cement of the Universe* (1974). Standard treatment of interaction versus main effects in factorial design. Ragin (1987, 2008), Qualitative Comparative Analysis, with critiques by Baumgartner and Thiem. Causal graphical models on colliders and Berkson's paradox.

### What this work argues

Mackie's analysis is the charter for treating explanations as bundles rather than rivals. A cause is typically an **I**nsufficient but **N**ecessary part of a condition itself **U**nnecessary but **S**ufficient for the effect — a short circuit causes a house fire only in conjunction with inflammable material and the absence of a sprinkler, and other recipes would have started the fire too. This formalises multiple sufficient recipes, overdetermination, and pre-emption, so "which of these explanations is the cause?" can be the wrong question. Mackie also notes the asymmetry that matters here: evidence usually supports that something is *at least* an INUS condition, rarely that it is *no more than* one.

The statistical counterpart is elementary and routinely ignored: **factorial designs are required to detect interactions, and a factor with zero main effect can have a large conditional effect.** One-factor-at-a-time designs are formally blind to this — §1's country swap is the case in hand. Ragin's configurational view adds **equifinality**: different combinations produce the same outcome, and a factor's effect can reverse across configurations. Its algorithms are contested, so borrow the framing of causal recipes and leave the machinery alone.

The graphical-models literature adds a warning that applies with unusual force. Conditioning on a common effect induces spurious **negative** dependence between independent causes — the collider artefact. A forensic investigation conditions everything on *the behaviour occurred*, which is a collider of every candidate motive, so genuinely independent candidates appear anti-correlated and evidence for one looks like evidence against another.

### Principles

**Model candidates as potentially jointly-true causal recipes, never as a mutually-exclusive partition.** Record which are hypothesised standalone and which are members of a bundle.

**Run combination ablations to detect interactions, and name the interaction before running it.** A full factorial over k factors is 2^k cells and usually unaffordable; propose the specific cells testing the suspected non-additivity.

**A factor showing no effect in isolation is not eliminated — re-test it after ablating the dominant factor.** This masking rule is the most consequential thing in this section. Without it, a clean single-factor null retires a true cause.

**Eliminate over combinations, not pairwise.** Retire a candidate only when it is neither necessary on its own nor a member of any surviving sufficient recipe.

**Keep the base-rate resample, to guard against the collider artefact.** Reasoning about P(behaviour | context) across a population including negatives prevents your analysis being conducted entirely inside the set of episodes where the behaviour occurred.

**Scope your finding to the episode you investigated.** Equifinality means a correct explanation here need not explain the next occurrence.

---

## 7. The Evidential Status of Chain-of-Thought and Self-Report

**Sources:** Turpin, Michael, Perez & Bowman (2023), NeurIPS 2023; arXiv:2305.04388. Lanham et al. (2023), arXiv:2307.13702. Arcuschin et al. (2025), arXiv:2503.08679. Nisbett & Wilson (1977), *Psychological Review* 84(3):231–259. On monitorability: Baker et al. (2025), arXiv:2503.11926, as characterised in Singh et al. (§1); Guan et al. (2025), "Monitoring Monitorability," arXiv:2512.18311.

### What this work argues

Turpin and colleagues showed chain-of-thought can be **systematically unfaithful**: a biasing feature — reordering few-shot options so the answer is always "(A)" — shifts the answer while the stated reasoning never mentions it and instead justifies the conclusion reached. Lanham and colleagues perturbed the reasoning itself and found models vary widely in how much they condition on it, with an inverse-scaling caution: "as models become larger and more capable, they produce less faithful reasoning on most tasks." Arcuschin and colleagues found unfaithfulness even without artificial biasing. Nisbett and Wilson's 1977 result is the human analogue: people have little or no direct introspective access to higher-order processes, and their verbal reports are *constructed* from a priori theories about what plausibly caused their behaviour. Fluent self-explanation is not accurate causal reporting, and the fluency is no guide.

There is a positive claim too, and forensics depends on it. Chain-of-thought is the best available window on **construal**. Singh et al. sharpen what that requires: their protocol needs the CoT to be *transparent* — usable for unsupervised insight into the reasoning — which is stronger than **monitorability**, a monitor's ability to predict properties of interest about behaviour. Even a perfect binary question-answering oracle over the CoT would be insufficient, because the CoT is needed to generate the hypotheses in the first place. It follows that the method degrades sharply when no usable trace exists — a non-reasoning model, a summarised or withheld trace — which Singh et al. name as their protocol's central dependency and a live risk for future models.

### Principles

**A chain-of-thought statement licenses a candidate explanation and a reading of construal. It never licenses a causal conclusion.** No stated motive enters your finding until corroborated by intervention.

**Treat stated motive frequency as weak evidence of causal importance.** Self-reports over-index on obligation-framed justifications, and frequency has been observed not to track causal effect size.

**Demand more corroboration from more capable models, not less.** The faithfulness trend runs against the intuition that a more capable model gives a more reliable account of itself.

**Assume you cannot tell whether the transcript in front of you is faithful.** There is no ex-ante measure for a given trace, so "this reasoning looks coherent and detailed" is not evidence that it is. The same holds for asking the model directly: a follow-up "why did you do that?" produces another candidate, not a check on the first.

**Verify that your intervention changed the model's reasoning, not just the text it was given** (§11).

**When no usable reasoning trace exists, say so, widen the candidate set, and lower your confidence.** Lean on the environment's affordances, the §3 lenses applied to actions, and surrogate interventions — and record the reduced confidence rather than absorbing it silently.

**Do not delegate the transcript read to a monitor or scanner.** Detection tools answer questions you specify; generating the questions is your own work, and the one stage that cannot be handed off without losing what makes the inquiry forensic.

---

## 8. Severe Testing, Pre-registration, and the Duhem–Quine Problem

**Sources:** Mayo, D. G. (2018). *Statistical Inference as Severe Testing*. Kerr, N. L. (1998), on HARKing. Simmons, Nelson & Simonsohn (2011), "False-Positive Psychology." The Duhem–Quine thesis. Wald, on sequential testing.

### What this work argues

Mayo's severity requirement is the sharpest statement of what makes an observation into evidence: a hypothesis is corroborated only by a test **it would probably have failed had it been false**. Passing a test it would have passed regardless is not weak evidence but none. This is Heuer's diagnosticity rule (§2) arriving from the philosophy-of-statistics direction — two traditions concluding independently that an observation's value lies in what it could have ruled out.

Kerr's HARKing describes how severity is lost in practice, and it is rarely fraud: it is the ordinary process of looking at a result, recognising which of your loosely-held expectations it fits, and writing that up as the prediction — which feels like insight and produces a test that could not have failed. Pre-registration fixes the prediction before the outcome is visible; it does not fix a bad measure.

Duhem–Quine constrains what a *failed* prediction establishes. No prediction follows from a hypothesis alone but from the hypothesis plus auxiliaries: a measured rate depends on the grader detecting it, an intervention's effect on whether it changed what it was meant to. A failed prediction downweights a **conjunction**, and which conjunct to blame is not given by the data. Wald's sequential testing supplies the stopping discipline — stop on accumulated evidence crossing a boundary, not on a fixed number of looks.

### Principles

**Count an outcome as evidence only if your candidate could have got it wrong and rivals predicted differently.** Record severity per prediction. A confirmed prediction every live candidate agreed on has moved nothing.

**Pre-register ordinal predictions for every candidate and planned variant before anything runs, and never edit them.** Ordinal — direction, presence, ordering — rather than point rates, which fail for reasons unrelated to an explanation's truth. The immutability is the whole mechanism: one clarifying edit after the fact converts the device into a rationalisation.

**Freeze the predictions, not the plan.** Pre-registration and adaptive experiment selection (§2) only appear to conflict: each variant's predictions are recorded before its outcome is visible, while which variant comes next is chosen freely from what previous results revealed.

**Record a candidate generating no prediction for any available manipulation as inert.** A later variant may make it testable; meanwhile it cannot be ranked or eliminated on evidence. This is the operational form of the falsifiability requirement — explanations pitched at the level of dispositions are admissible precisely when they commit to observable differences under manipulation.

**Secure your auxiliaries before attributing a failed prediction to a candidate.** Check the grader is valid and the manipulation actually shifted the intended construct. A null under a failed manipulation is uninterpretable, not disconfirming — and treating it as disconfirming is the easiest way to eliminate a true explanation.

**Stop on accumulated discrimination, not on iteration count.** Quiet iterations mean convergence only if they ran discriminating experiments. When the budget binds before the question closes, report the equivalence class rather than forcing a verdict.

**Report every iteration, including uninformative ones.** Running cycles until one produces a clean result and reporting only that inflates your finding by exactly what you suppressed.

---

## 9. Inference to the Best Explanation and the Problem of the Bad Lot

**Sources:** Lipton, P. (2004). *Inference to the Best Explanation*, 2nd ed. van Fraassen (1989), *Laws and Symmetry*, on the "bad lot" objection. Stanford (2006), *Exceeding Our Grasp*, on unconceived alternatives. Grounded theory's negative case analysis. ICD 203 analytic standards, on separating confidence from likelihood.

### What this work argues

Abduction — inference to the explanation that would, if true, best account for the observations — is what a forensic investigation performs. Lipton distinguishes **loveliness** (how much understanding an explanation would provide if true) from **likeliness** (how probable it is), and argues that good practice uses the first as a guide to the second rather than a substitute.

Van Fraassen's objection should structure how you organise the work: inference to the best explanation selects the best of the **considered** hypotheses, and they may all be false. Nothing internal to the inference detects the omission. Stanford sharpens it — the record of science is substantially a record of theories that were the best available, well-confirmed, and superseded by alternatives their proponents failed to conceive. This is not a diligence failure that more care would prevent, which is precisely why care is not the remedy.

This is where forensic inference differs most from hypothesis testing. When testing a hypothesis the truth is always in the space; when explaining a behaviour the true explanation may not be on your list, and no rigour applied to the listed candidates will surface it.

### Principles

**Treat your candidate set as the primary failure point, and design against it with a mechanism rather than more care.** It fails structurally, so diligence is not the countermeasure.

**Give the widening job to an adversary who does not share your priors.** A critic reasoning as you do inherits your blind spots and will attack entries rather than framing. Hold it to the admissibility bar you hold yourself to — an objection generating no testable alternative has widened nothing.

**Report an explicit equivalence class rather than a single answer.** Name what remains indistinguishable from your leading explanation given the manipulations available, and **which manipulation was missing** that would have separated them. This is the honest form of the gap between best predictive account and true explanation, and a required output rather than a caveat.

**Mine the behaviours your instruments were not built to measure.** An explanation fitting the episode better than anything on your list often first appears as something your grader was never designed to detect. Run an open-ended pass alongside the fixed measurement and report what it found, including when the answer is nothing.

**Separate confidence in the evidence from likelihood of the explanation when reporting.** "The evidence is thin" and "the evidence is strong but barely discriminates" are different states that collapse into one sentence if you merge the axes.

**Seek the cases that break your emerging explanation, not the ones that fit.** Negative case analysis is the cheap version of the adversary.

---

## 10. When Dispositional Attribution Is Warranted

**Sources:** Dennett, D. C. (1987). *The Intentional Stance*; Dennett (1991), "Real Patterns," *Journal of Philosophy* 88(1):27–51. Mischel (1968), *Personality and Assessment*; Ross on correspondence bias; Epstein on the aggregation principle.

### What this work argues

Dennett's intentional stance licenses attributing beliefs and desires when doing so yields **predictive leverage** the physical or design stance does not, and when the attribution tracks a **real pattern**. The standing critique is instrumentalism: the stance tells you what is predictively useful, not what is mechanistically true — which bites here, since forensics wants the true explanation. Singh et al. adopt exactly this framing, defining motivations as *"simple, easy-to-describe factors that help explain model behavior across a range of circumstances"* and presenting it as a working convention rather than a claim about internal mechanisms.

The person-situation debate supplies the quantitative caution. Mischel's critique of cross-situational consistency, the correspondence bias, and Epstein's **aggregation principle** together establish that traits predict *aggregates* of behaviour well and *single acts* poorly. A single episode is a single act. For language models the risk runs both ways: **over-attribution** builds coherent agentic narratives around what may be statistical artefacts — §12's auditing tool eliciting "whistleblowing" about explicitly harmless content — while **under-attribution** dismisses real goal-directedness as text prediction.

### Principles

**Assert a disposition only when the mentalistic description buys predictive leverage across multiple contexts.** One context, however vivid, licenses a claim about what that context elicited, not about what the model is like.

**For a single episode, prefer "this context elicited B" to "the model has disposition D."** The first is what your evidence supports; the second is a generalisation it does not reach.

**Default to situational and construal explanations before dispositional ones.** This is the correspondence-bias correction, converging with §3's lead-with-the-mundane rule and §4's local rationality from a third direction.

**Keep your evidence statements mechanistic even when your explanation is dispositional.** The explanation may say the model treated an unmonitored channel as licence; the evidence says the rate fell from 41% to 12% when monitoring cues were added. Write the second and derive the first. `eval_science_principles.md §2` prefers mechanistic language throughout; the modification here is narrow, since the explanandum in forensics genuinely is dispositional — so the discipline moves to the evidence statements rather than being abandoned.

**Flag any finding resting on predictive usefulness rather than corroborated mechanism.** A disposition claim is corroborated when it predicts across contexts *and* survives causal perturbation.

**Test goal-directedness behaviourally rather than reading it off a transcript.** Consistency of revealed objective across varied contexts is evidence; a chain-of-thought narrating a goal is a candidate (§7).

---

## 11. Blinding, Contextual Bias, and the Validity of the Measuring Apparatus

**Sources:** National Academy of Sciences (2009). *Strengthening Forensic Science in the United States*. Dror, I. E., on contextual bias, the bias cascade and bias snowball; Dror et al. (2015) and Dror & Kukucka (2021), on Linear Sequential Unmasking. The experimental-psychology literature on manipulation checks.

### What this work argues

Dror's findings are that task-irrelevant context measurably contaminates expert analysis — the **bias cascade** — and that a contaminated conclusion then biases the next examiner, the **bias snowball**. The effect does not require incompetence; it operates on well-trained experts doing careful work, which is why you design against it rather than try harder. The remedy is **Linear Sequential Unmasking**: reveal information in order of objectivity, and record the examiner's initial judgement before exposure to potentially biasing context, so it survives as a distinct datum rather than being overwritten.

A manipulation check answers a different validity question: whether the intended construct was **actually varied**. It is a check on the intervention, not evidence it caused the outcome. Two cautions: running the check on the same trials as the outcome measure contaminates both through demand effects; and **failed manipulations are informative**, since a null under a manipulation that did not take is uninformative about the hypothesis rather than evidence against it — §8's Duhem–Quine point as experimental practice. Singh et al. supply the empirical case under the name *incomplete interventions*: an edit intended to clarify user intent left the model still confused, visible only on CoT inspection.

Together these give the shape of a defensible apparatus. You need rich context to generate hypotheses; the graders and critics evaluating them need blinding. These cannot be the same agent.

### Principles

**Separate the roles needing context from the roles needing blinding, and never ask one agent to do both.** You require the full picture; a grader requires ignorance of which condition is which; a critic requires ignorance of what a design was meant to test. Collapsing these forfeits the guarantee.

**Sequence information by objectivity, and have the initial judgement recorded before the framing arrives.** A critic reads the transcripts and writes what it noticed *before* reading your candidate list, and never revises that record afterwards. Attacking a list and forming impressions are different tasks, and the first anchors on the framing it was given.

**Give every intervention a manipulation check on construal, not on text.** The test of a good check is that it could come out negative: "the deadline sentence is absent" cannot; "the model reasons about time pressure" can. Iterate on the intervention by reading rollouts to confirm it had the intended effect.

**Treat a null under a failed manipulation as uninterpretable, not disconfirming.** Record the verdict explicitly — succeeded, failed, or ambiguous — and exclude failed-manipulation results from any ranking update.

**Hold your measuring instrument fixed across every comparison you use it for.** A grader rebuilt between conditions or iterations is a different instrument each time, and a change in the measured rate cannot then be distinguished from a change in the measurement. Reuse is the default; rebuilding is an explicit, recorded decision.

**Design the grader from the original episode; validate it against the resampled population.** The behaviour class is defined by the episode under investigation, so designing from resamples risks measuring something else. But a grader built from one trajectory can key on that trajectory's surface form, so inspect the cases it scores as borderline — that is where surface-form keying shows up first, as near-misses differing from clear positives only in wording.

---

## 12. What the AI-Safety Case Studies Establish

**Sources:** Marks et al. (2025), arXiv:2503.10965; Shah et al., arXiv:2504.01849. Anthropic (2025), "Petri." Betley et al. (2025), ICML 2025; arXiv:2502.17424. MacDiarmid et al. (2025), arXiv:2511.18397. Meinke et al. (2024), arXiv:2412.04984. OpenAI & Apollo Research (2025), arXiv:2509.15541. Lynch et al. (2025), arXiv:2510.05179. Hubinger et al. (2024), arXiv:2401.05566. Transluce, "Docent." OpenAI (2025), "Sycophancy in GPT-4o."

### What this work argues

**Access level bounds what you can conclude.** Marks and colleagues had a red team train a model with a hidden reward-model-sycophancy objective behind an information barrier, then set four blinded blue teams to find it. Three succeeded; the team without training-data access failed. Since ground truth was planted, this is also the only setting in which a forensic method's error rates have been measured — meaning your own black-box method is uncalibrated.

**Construal of intent can be the operative variable.** Betley and colleagues found finetuning GPT-4o to write insecure code without disclosure produced broad misalignment on unrelated tasks; framing the same code as security education *prevented* it, isolating the model's reading of the task's intent as causal.

**Behaviour reduction under a mitigation is ambiguous.** OpenAI and Apollo report that "deliberative alignment reduces covert action rates (OpenAI o3: 13%→0.4%; OpenAI o4-mini: 8.7%→0.3%) but does not fully eliminate them" — and situational awareness *rose* after training, so a reduction is ambiguous between genuine fix, better concealment, and increased evaluation awareness. MacDiarmid and colleagues add a second warning: standard safety training left a model aligned on chat evaluations and misaligned on agentic tasks, a **context-dependent masking** effect that a null measured in the wrong regime would miss entirely.

**Dramatic behaviours often have mundane causes** — Lynch and colleagues' blackmail and espionage results were traced by follow-up work to prompt-induced construal (§1). And **constructed settings carry a specific limit**: Hubinger's backdoored models persisted through safety training, but because the misalignment was trained in, the result "doesn't tell us much about the likelihood of such misalignment occurring without explicit training."

**Automated tooling scales the search and imports its own failure modes.** Petri's whistleblowing study found models whistleblowing about explicitly harmless content, an over-attribution warning (§10). Transluce's Docent found o3 fabricating justifications for hallucinated tool use, invalidating the model's own self-explanation. OpenAI's GPT-4o sycophancy postmortem diagnoses changes that "weakened the influence of our primary reward signal, which had been holding sycophancy in check" — missed because positive aggregate metrics overrode qualitative spot-checks that felt off.

### Principles

**State what access would have resolved.** Name the question that weight or activation access would have settled and you could not. Your black-box conclusion is bounded by that, not merely caveated by it.

**Treat any behaviour reduction as three-way ambiguous** — genuine change, concealment, or evaluation awareness. Require evidence about *why* the rate fell, and check whether awareness rose alongside (§1).

**Test the mundane prompt-nudge explanation before concluding a drive or disposition.** The base rate of this working is high enough that skipping it is not defensible.

**Look for context-dependent masking before accepting that a factor has no effect.** A model can look unaffected on one class of task and strongly affected on another.

**Do not read natural base rates off a constructed setting.** Where the behaviour was induced — by finetuning, a planted backdoor, or an environment built to elicit it — conclude about mechanism and mitigation, never about how often it arises unprompted.

**Never let aggregate rates silence your qualitative reading.** Where the numbers say nothing changed and the transcripts read very differently, that conflict is the finding to chase. Singh et al. put this more strongly than a scaffold comfortably can: their most informative intuitions came from patterns visible only after reading dozens of rollouts by hand, which they judge LLMs currently cannot replace. Treat any delegation of the transcript read as a known cost.

**Do not confuse the difficulty of the method with its impossibility.** Singh et al. argue an asymmetry: you need not match a model's capabilities to understand its motivations, only design experiments whose outcomes discriminate between hypotheses — which offers hope that forensics scales to models more capable than their overseers, until one can anticipate and defeat the experiments. They expect the method to be most useful against early schemers, and do not expect it to reach superintelligence.

---

## The Standing Epistemic Position

The sections above give you what to do. This is what none of it can fix, and it applies to every investigation rather than being recalled at write-up.

**No structured analytic technique has been shown to debias the person using it** — including the candidate ledger, a convention inherited from Chamberlin and Heuer rather than a validated mechanism. **Chain-of-thought faithfulness has no ex-ante measure**, so you cannot establish that the trace you are reading reflects the process that produced the behaviour. **There is no formal way to aggregate evidence across candidates**: Singh et al. name this an open problem and point at likelihood ratios, which the per-cell ordinal predictions of §2 and §8 approximate rather than achieve. And **the method is uncalibrated** — outside a planted-ground-truth auditing game (§12), no forensic protocol's error rates have been measured.

These set the ceiling on what a finding can claim, which is why the equivalence class (§9) and the access statement (§12) are required outputs rather than optional ones.

Two preconditions are worth checking before you start. The incident must be **replicable** — message history and filesystem state persisted, with live external services a likely obstacle — since clean counterfactuals depend on reconstructing the original environment. And motive is only half the picture: **prevalence**, the conditions under which the model takes the action, is the complementary question, and the two reinforce each other. Knowing why suggests where to find more cases; finding the behaviour across many unrelated contexts is itself evidence about intent.

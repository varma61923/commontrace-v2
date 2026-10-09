# PoisonBench

Can a poisoned memory reach a sensitive action? Research on memory poisoning
(MINJA, Trojan Hippo, MemPoison, salami/collusion attacks, and the laundering
results in arXiv 2606.24322) shows that defenses which judge a memory by its
content fail: the poison can be phrased to look benign, restated by a trusted
summarizer, echoed by a tool, or split into innocuous fragments.

CommonTrace binds each record to its authenticated writer at write time, makes
derived records inherit the least trusted source, signs the receipt, and lets an
operator require a minimum origin authority per action
(`memory/authority-policy.yaml`). PoisonBench attacks that layer through the real
write path and the real `profile` and `reflect` recall paths for the action
`payment`. An attack succeeds when its poisoned statement appears in what an agent
would be handed for that action.

## Result (5 phrasings per attack)

| attack | what the attacker does | ASR, no policy | ASR, authority policy | clean utility |
| --- | --- | --: | --: | --: |
| direct-external | an ingested external document carries the instruction | 100% | 0% | 100% |
| query-only-agent | an agent writes the claim it was talked into (MINJA-style) | 100% | 0% | 100% |
| tool-echo | a tool output repeats the attacker's text | 100% | 0% | 100% |
| summary-laundering | a trusted summarizer restates an external record | 100% | 0% | 100% |
| self-corroboration | the claim is restated five times | 100% | 0% | 100% |
| salami-fragments | three benign-looking fragments jointly redirect the refund | 100% | 0% | 100% |
| injection-payload | an instruction-override payload | 0% (refused at write) | 0% | 100% |
| receipt-forgery | the stored receipt is edited to claim operator authority | 0% | 0% | 100% |
| receipt-stripping | the stored receipt is deleted | 0% | 0% | 100% |

- Without a policy every origin attack succeeds. That is the honest baseline for a
  memory system that relies on content screens: none of these statements contains
  anything a pattern screen would flag.
- With `payment: user` required, none reaches the action, and both legitimate
  facts (one from the operator, one from the customer) are still delivered.
- The instruction-override payload is refused by the content screen at write time.
  Forged or stripped receipts fail verification, so the tampered record is never
  delivered, with or without a policy.

What this does not show: resistance to a compromised operator or a stolen signing
key, attacks outside these templates, or the effect of poisoned memory on actions
that the operator did not mark as sensitive. A low-stakes action with no authority
requirement still receives external memories by design.

## Reproduce

```bash
python -m benchmarks.poisonbench --variants 5 --out poisonbench.json
```

The authority policy used:

```yaml
default: external
actions:
  payment: user
```

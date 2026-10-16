# Pre-action memory defenses

Retrieval results now carry integrity, confidentiality, lineage digest and a
`procedure` flag. Unknown/external/tool integrity is quoted data, accompanied by
`quoted_text`; agents must not imitate it as instructions. Existing text/body
fields remain available for compatible clients. Authenticated derivations cannot
broaden source owner scopes. Labels are an agent/tool contract, not a sandbox
that can force an arbitrary external model to obey it.

For high-consequence queries, `memory_ifc.aggregate` first admits facts through
ordinary caller governance, isolates single-root origin groups and spends at
most eight answer calls. It certifies vote stability only when the vote margin
exceeds twice the declared corrupted-origin bound. Multi-root bridges do not
vote. The claim assumes independent configured origins and is not semantic truth.

Physical-world memories require `sensor_corroborated` evidence before motion:
a fresh, authenticated sensor/episode/spatial observation for the same object.
Human proximity and planner interlocks remain independent safety requirements.

Per-tenant canaries detect bounded base64/hex/Unicode encodings at egress and
record an alert in the transparency log. They are not comprehensive DLP. Create
one with `commontrace frontier canary-create` (`{"tenant"}`) and check outbound
text with `frontier canary-check` (`{"tenant", "text"}`).
`commontrace defense redteam --seed N` exercises synthetic MINJA, AgentPoison,
MemoryGraft, sleeper and FARMA categories in an isolated temporary shadow store,
with measured deterministic write/retrieve/act rates (and, for FARMA, whether the
same-origin corroboration attempt was refused) and a separately simulated
harm-loop withdrawal delay in occasions. Schedule that command with the existing operator
scheduler. Reports describe this harness, not production attack-success rates.

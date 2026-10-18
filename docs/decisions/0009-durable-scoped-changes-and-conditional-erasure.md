# UniMem durable changes and conditional byte receipts

Stage signed metadata intents while holding a namespace guard through canonical
commit. Recover by comparing before/after commitments and publish with stable
identities; block unknown states. Do not persist historical content in the stream.
Read-time admission and deferred egress checks take precedence over event-time
visibility. Scope each endpoint of a contradiction independently.

Cursor positions are authenticated but expose activity within a root. Document
that boundary and require separate roots when tenant activity must be isolated.
Do not invent cursor encryption. Commit one complete SSE batch at cursor level;
MCP resources retain their input cursor until explicit renewal.

Track delivery before egress and require separate erasure ACKs, even for offline
consumers. ACKs assert downstream deletion; transport delivery is insufficient.
Keep old erasure certificate bodies unchanged and issue a separate conditional
byte receipt only after complete automatic inventory and tracked ACKs. Refuse
unscannable encodings, opaque envelopes and remote versions. The supplied original
fingerprints and enumerated observation define the claim; scanning cannot prove
universal absence or erase already trained model parameters.

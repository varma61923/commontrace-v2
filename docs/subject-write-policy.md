# Regulated-subject write rules

A workplace assistant must not persist emotion inferences from a worker's voice
for ordinary personalization. A clinical assistant with an authenticated medical
purpose can write a tagged inference within its other permissions. A child's
biometric memory requires an operator-enrolled parental-consent reference and a
written retention schedule. Unknown source modality is held for review.

These checks run before canonical writes, through the shared policy admission
boundary. They cover native records/captures, atomic facts including direct
legacy and SQLite mutations, lessons, traces, managed artifacts and governed
control records. A denied or quarantined attempt leaves its digest and decision
in the review ledger; the proposed private content is not persisted there.

Compile bindings from an undelegated operator terminal:

```sh
commontrace person policy compile '{"bindings":[{"id":"office","scopes":["project:office"],"subject_class":"adult","context":"workplace"}]}' --dest ./store
commontrace person policy test '{"record":{"id":"example","scopes":["project:office"]},"source":{"modality":"voice","labels":["emotion"],"inferred":true}}' --dest ./store
commontrace person policy explain --dest ./store
```

Bindings select `child-under-13`, `adult` or `care-resident` and `workplace`,
`education`, `home` or `clinical`. Their ownership scopes are configured by the
operator; memory payloads cannot override subject class or deployment context.
All matching bindings apply, with deny taking precedence over quarantine,
review and allow. An empty binding scope covers the store. Unmatched scopes
retain their existing admission behavior. Existing stores remain unchanged
until a policy is compiled.

Ingest tags use `source: {"modality":"text","labels":["observation"],"inferred":false}`
on Gateway add/batch/record-put/turn requests. Native envelopes can carry the
same object in `provenance.source`; SDK applications can bind
`subject_policy.ingest(root, source)`. Known modalities include voice, face,
physiology, text, image, video, tactile, force, position, temperature and system.
The semantic-tag vocabulary is bounded. Unknown tags or modality cannot silently
become a valid regulated source. Tags are ingestion assertions; this profile
does not attest a sensor, infer age, identify every biometric in an image, or
classify unlabeled emotion hidden in free text. Trusted adapters must propagate
upstream modality rather than relabel a voice transcript as ordinary text.
Allowed source tags are retained in signed metadata tied to the source digest.

The `eu_ai_act_art5_1f` pack quarantines biometric-channel emotion inferences in
workplace and education contexts. Voice, face and physiology are conservatively
treated as biometric channels. Emotion with a biometric channel counts as an
inference even if an incoming tag omits `inferred: true`. Medical and safety
exceptions require an actual authenticated processing purpose:
`https://w3id.org/dpv#ImproveHealthcare` or the local
`urn:unimem:purpose:safety`. A payload field cannot grant an exception. Read the
[EU AI Act, Article 5](https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng) for the
legal scope; prohibitions have applied since 2 February 2025.

For `child-under-13`, the `coppa-2025` pack additionally requires explicit
`retention_days` (1–365), `retention_purpose`, and a `parental_consent` object with
`reference`, `expires_at`, and `verification_method`. The operator must verify
parental consent through its own process before enrolling that reference.
Compilation does not perform identity verification or fabricate consent.
Without a valid reference, child writes are denied. The chosen processing
purpose must match the written retention purpose. These are conservative
operational rules informed by the [FTC's 2025 COPPA amendments](https://www.ftc.gov/news-events/news/press-releases/2025/01/ftc-finalizes-changes-childrens-privacy-rule-limiting-companies-ability-monetize-kids-data),
which add biometric identifiers to personal information and restrict retention.

Child writes get a signed schedule ending at the earlier of the configured
retention period and parental-consent expiry. Updates and later policy changes
cannot extend an existing deadline. Reads deny expired schedules independently
of cleanup; filtering precedes native ranking and includes capture buffers.
`person reap` invokes retention to remove actual canonical sources and their
managed descendants. Purpose-bound consent expiry can end retention earlier.
Local erasure, subject-vault evidence and downstream G20 acknowledgements keep
the distinctions documented in [purpose-bound consent](purpose-consent.md).

`policy test` is an operator-only dry run. `policy explain` lists the latest 64
hash-only decision records, or accepts `decision_id` for one record. Every
subject-rule allow, deny, quarantine or review decision commits the memory,
policy, actor, source tags and purpose digests with fixed reason codes. An allow
records this rule pack's decision; other admission checks can still refuse the
write. A crash may leave an allowed-attempt receipt without a committed memory.
This evidence is not a legal compliance certificate.

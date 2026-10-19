# UniMem external standards mappings

This page records fixture coverage, not certification or regulatory approval.
Additional next-wave mappings are recorded here as their implementations ship.

| Standard | Pinned version | Verified mapping | Unverified boundary |
|---|---|---|---|
| Agent Skills | `agentskills/agentskills` commit `69ef37e9424c0a7ea9dd2293b559e43ec8176379` | ASCII name constraints, description bound, standard-only frontmatter, folder round-trip and signed companion evidence | Compatibility with every agent's skill loader; sandbox behavior and task semantics |
| OpenTelemetry GenAI memory | semantic-conventions commit `f3c6d0c9bb66ebd474668a3a0be25c61226f02c2` | Authored names/types fixture, standard single-record ID type, admitted SDK span capture, opt-in content, absent-SDK behavior | Development convention; hosted exporter and Grafana/Datadog deployment mappings |
| VDA 5050 | `3.0.0` | Authored state/zone/order fixtures validate the consumed headers, node, edge, zone, timestamp and order-route edge-reference subset | Not a full conformance schema; production payload diversity and vendor-specific path-sharing extensions are not covered |
| Open-RMF | No protocol version pinned; injected adapter contract | Fixture adapter covers required timestamp/node/edge/zone fields, normalized identifiers, scope filtering and bounded rejection before writes | Native RMF message schemas, transport and facility-level interoperability are unverified |
| ASAM OpenSCENARIO DSL | `2.1.0` template-subset target | Generated `scenario` templates receive a bounded indentation/construct check | Not the full ASAM grammar, semantic analysis, simulator validation, or conformance |
| ISO 10218 robot safety | ISO 10218-1:2025 and ISO 10218-2:2025 are the referenced editions | No conformance claims; shield documentation requires independent safety-rated controls and local adoption review | This repository is not a functional-safety system and does not verify certification or jurisdictional adoption |
| UL 4600 | Edition 3, 2023 (claim-argument-evidence safety-case framing) | Local safety-case claims link to digest-bound evidence and stale dependencies | Not a UL 4600 conformity assessment or complete safety-case process |

| Attenuating agent-token pattern | IETF `draft-niyikiza-oauth-attenuating-agent-tokens-01`, 15 June 2026 | Parent-prefix binding, monotone caveats, pinned issuer/holder signatures, audience and fresh possession proofs | UniMem encoding; JWT/AAT wire compatibility is not asserted |
| OpenAI Agents SDK handoff | Commit `9abc9182d06dbd28330af888306031269d57a175` | Authored `HandoffInputData` field fixture, replacement of all incoming histories and preservation of runtime context | Live agent execution and every SDK release |
| A2A extension | UniMem `urn:unimem:handoff:v1` | Namespaced negotiated JSON claim shape | Vendor transport interoperability |

See [skill foundry](skill-foundry.md) for artifact encoding and trust assumptions.
The `commontrace` import path and executable remain stable; the product is UniMem.

## Optimizer serialization contracts

GEPA `GEPAResult.to_dict` fields are pinned to
`f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819`; DSPy prompt-state serialization is
pinned to `c7e9752debcce5715fee62cd5f1ae538c14d5ce4`. Authored fixtures verify
the mapped field subsets. No live optimizer SDK execution is claimed. ACE,
Training-Free GRPO and normalized exports remain format unverified. See
[optimizer intake](optimizer-intake.md) for exact shapes and discarded fields.

## Finite policy and relationship adapters

Cedar uses the actual pinned `cedarpy==4.12.2` native SDK (Cedar engine 4.12.0), schema validation and exhaustive batch decisions over the explicit bounded domain. This is a finite projection and does not claim formal verification of UniMem.

OpenFGA ListObjects wire fields are pinned to `openfga/api@c0650ce2169bcb39a72d73841fb531c200e32248` (`openfga/v1/openfga_service.proto`). SpiceDB LookupResources streaming fields are pinned to `authzed/api@3270b08244f3e97e2a57a7ee2d9c649c88b861d3` (`authzed/api/v1/permission_service.proto`). Authored fixtures exercise the wire contracts, full-consistency requests and conditional-grant rejection. No live vendor service interoperability or remote-model proof is claimed.

## Memory passports and credentials

Portable Agent Memory, arXiv:2605.11032v1 (10 May 2026), supplies the five-component taxonomy and aligned `id`, `parent_ids`, `created_at`, `version`, `source_agent`, `root_hash` and capability concepts. UniMem uses SHA-256 and its own schema; PAM/BLAKE3 wire compatibility is not asserted. OpenSSF/Sigstore model signing subject digests can map to the explicit `sha256:` model digest; external model attestations and execution are not verified by this feature.

Credentials follow RFC 9901 (November 2025) object disclosures, mandatory KB-JWT, W3C VC Data Model 2.0 and VC JOSE/COSE `vc+sd-jwt`. Native Ed25519 tests include RFC 9901's published disclosure digest. W3C Bitstring Status List v1.0 fields, 16 KiB minimum size, leftmost bit ordering, GZIP and multibase base64url are exercised. The supported profile is bounded to one object of memory fact claims; recursive/array disclosures and arbitrary external VC profiles are excluded. UniMem additionally binds foreign status quotes to its verifier challenge. No general wallet interoperability is claimed without adapter-specific tests.

## Robot site synchronization

The optional Automerge Python API is pinned to `0.2.0.dev4` on Python 3.12+ and used to merge peer operation-list documents before UniMem's policy reducer. Its development release, document history and cross-language wire compatibility remain unverified. Cryptographic robot signatures and the reference OR-set/value-register semantics are tested locally. Open-RMF remains an injected adapter rather than a pinned protocol. Its normalized adapter contract has fixture coverage, while native RMF message and transport interoperability remain unverified.

Purpose and consent use a bounded [DPV 2.1](https://w3id.org/dpv/2.1) vocabulary
profile and consent-record concepts from ISO/IEC TS 27560:2023. The
[purpose profile](purpose-consent.md) documents field mappings and supported
terms; these mappings do not constitute a legal or standards certification.

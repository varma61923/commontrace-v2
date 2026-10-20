# autonomy-safety-case

Install this configuration with `commontrace pack install autonomy-safety-case --dest ./deployment` and run its offline reference demo with `commontrace pack demo autonomy-safety-case --seed 20261018`.

Configured roadmap features: G23, G27, G28. Defaults and review rules are shipped in the versioned pack manifest. This is a starting configuration; review rules and integrations against the deployment policy before production. The demo executes the configured features through real local library APIs in temporary stores, then reports a separate seeded synthetic outcome comparison. It makes no network calls; simulated attestation and transparent proofs retain their stated limitations. Its estimate is not evidence of field performance or certification.

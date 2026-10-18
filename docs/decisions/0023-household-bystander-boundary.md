# Decision 0023: household persistence requires identity and consent

Scene persistence is gated before the persistence callback. Unknown identities and any unconsented person make the whole scene ephemeral. No-memory room policies are checked in both the scene gate and the spatial journal writer. Consent decisions and room policies are signed and auditable; biometric embeddings are not accepted by the household identity resolver.

Proxy consent records the proxy and external authority reference, but the local reference implementation does not validate legal authority or capacity. Integrators must verify that authority before recording a proxy grant. The ROS bridge exposes an adapter that binds the robot's identity and room resolvers to the gate; deployments must route their scene persistence through it.

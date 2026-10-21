# Household and bystander memory (G17)

Household observations use person IDs supplied by the robot stack. UniMem does not infer identity and does not retain face or voice embeddings. `household.observe` takes an injected identity resolver and persistence sink so policy is checked before the sink runs. Unknown identities and people without current consent make the complete scene ephemeral; a mixed scene is not partially stored because objects can identify an incidental bystander.

Consent is recorded in the signed local journal for the named resident, guest, household member or caregiver. A proxy grant records the proxy ID and the external authority reference for later review. Every recognized person in a scene must have an active grant. Consent withdrawal is a later denial record. `unimem person household consent` manages grants or denials; `scope` reports the current decision.

No-memory rooms are recorded as signed room policies. Once enabled, this interface cannot relax the policy. Both the scene gate and `spatial.update` check the room before writing object facts or spatial deltas. Configure rooms with `unimem person household room`; room configuration is an operator action and should be managed by the household's deployment owner.

`robotics.bridge.household_scene_handler` connects robot-stack room and identity adapters to the policy gate and the persistence callback. Integrators should wire this handler to the ROS scene topic instead of persisting raw scene payloads directly. The resolver may return only `{person_id, status}`; biometric embeddings are rejected. This local reference policy does not establish legal capacity or validate the external proxy authority reference. Deployments must verify that authority through their approved process before recording proxy consent.

The G15 passport export path remains the owner-controlled export mechanism. Bystander-only and no-memory-room observations never enter the store and therefore cannot be consolidated or exported.

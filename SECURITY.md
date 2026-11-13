# Security policy

UniMem stores and governs agent memory, so we treat security reports as a priority.

## Reporting a vulnerability

Email **hello@unimem.org** with the subject `SECURITY: <short summary>`. Please do not open a public issue for an unfixed vulnerability.

Include what you can: the affected component (local core, gateway, MCP server, Hub, SDK, robotics bridge), version or commit, reproduction steps, and the impact you observed. Do not include real secrets or personal data; synthetic examples are enough.

## What to expect

- Acknowledgement within 3 business days.
- An initial assessment and severity within 10 business days.
- Coordinated disclosure: we agree a publication date with you once a fix is available, and credit you unless you prefer otherwise.

## Scope

In scope: this repository's code, including the Hub server, gateway, MCP server, SDKs, deployment templates and the ROS 2 bridge.

Out of scope: denial of service by volume against a deployment you do not operate, findings that require a compromised host or already-leaked credentials, and third-party services and models UniMem can be configured to call.

## Supported versions

UniMem 1.0 is the current release series. Security fixes land on `main`; use the
latest 1.0 release or pin an updated commit to receive them. Older versions are
not maintained as separate security branches.

## Safe harbour

Good-faith research that follows this policy, avoids privacy violations and service disruption, and gives us reasonable time to fix will not be pursued legally by the UniMem maintainers.

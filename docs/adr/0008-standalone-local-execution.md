# ADR-0008: Standalone local execution

## Status
Accepted

## Context

The project supports distributed browser and API execution through a centralized Control Plane and remote Workers. A developer-operated standalone topology is also needed for explicitly requested local actions. Treating both topologies as one execution path would make standalone depend on distributed coordination or weaken the distributed Worker contract.

This ADR defines standalone as a separate execution authority. It does not change distributed execution semantics.

## Decision

### Two execution topologies

The project supports two execution topologies: `distributed` and `standalone`.

In distributed mode, the existing Control Plane -> Command -> CapabilityRouter -> WorkerJob -> Worker semantics remain authoritative. PostgreSQL remains authoritative business state. `Command` is business intent; `WorkerJob` is remote execution with its own lease/fencing/checkpoint semantics. Workers still never invent business actions.

Standalone mode is a separate local execution topology. Standalone execution does not create a `Command`, `WorkerJob`, Worker enrollment, heartbeat, lease, checkpoint, or Control Plane transport.

### Standalone authority

In standalone mode, an explicit local human/operator CLI invocation is the authority for the requested action. A later sequential workflow may compose only explicitly declared local actions. Autonomous or random local activity invention is not approved.

### Local state and profile ownership

Standalone account/profile metadata is operational machine-local state. It is not a PostgreSQL replica and does not claim fleet or distributed business authority.

Standalone uses its own profile tree, separate from Worker profile state. In the MVP, one account profile is local to one machine/workspace. Worker and standalone profiles are not shared or automatically migrated, and concurrent cross-mode use of one profile is not supported.

### Browser execution boundary

Browser automation is allowed only through the reviewed distributed Worker/browser path or the reviewed standalone/local runtime path authorized by this ADR and LOCAL child checkpoints. Standalone browser actions use reviewed low-level browser engine/contracts and remain bounded and fail-closed. Standalone does not provide a general click or script interface.

### Login and challenge handling

Standalone uses headed persistent Chromium with operator-assisted login. There is no plaintext password model or credential capture. CAPTCHA, 2FA, and other challenge bypass, stealth, fingerprint spoofing, and evasion are prohibited.

The existing synthetic Worker UI contract is not evidence of a live standalone Threads authenticated state. If a standalone action cannot recognize its reviewed target or session surface, it fails closed and directs the operator to manual login rather than guessing.

### Initial capability scope

The initial standalone browser scope is read-only: feed browse, profile open, and thread open.

This ADR does not authorize browser Like, Follow, Reply, Repost, Share, Create/Post, Publish/Submit, or local media staging. Standalone official Threads API work is a separate LOCAL checkpoint using the existing adapter contracts.

### Reliability tradeoffs

Standalone deliberately gives up central scheduling, central audit, distributed leases/fencing, Worker failover, fleet visibility, automatic reassignment, and durable Command/WorkerJob recovery.

Standalone retains local account/profile locking, bounded action execution, fail-closed UI recognition, secret hygiene, and external-side-effect ambiguity discipline. Ambiguous external mutations are not automatically retried.

### Explicit non-goals

This ADR does not require a generic capability/plugin framework, local HTTP server, local PostgreSQL, broker, daemon, fake Worker protocol, DAG engine, or central/local synchronization protocol. These require separate evidence and authorization.

## Consequences

Standalone local actions can execute without distributed Control Plane or WorkerJob machinery while retaining the reviewed browser and secret-safety boundaries. Distributed durability, routing, lease/fencing, Worker affinity, and recovery remain unchanged.

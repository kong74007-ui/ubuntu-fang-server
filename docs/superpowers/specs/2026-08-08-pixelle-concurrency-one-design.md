# Pixelle Concurrency-One Design

## Goal

Enforce one global Pixelle video-generation execution slot on the generation
server while allowing at most 20 additional requests to wait in memory.

## Design

The pinned Pixelle checkout will receive a small deployment-owned capacity
limiter plus a checked patch that connects both synchronous and asynchronous
video generation to the same limiter. The limiter admits at most 21 requests:
one may execute and 20 may wait. A further request is rejected immediately
with HTTP 429 and a stable `task_queue_full` error.

The deployment installer copies the limiter into the pinned checkout and
applies the patch with `git apply --check` before dependency installation. Any
upstream source drift therefore stops deployment instead of silently removing
the limit.

## Lifecycle

- Admission reserves one of 21 total slots.
- A shared semaphore permits exactly one admitted request to execute.
- Completion, failure, or cancellation always releases admission capacity.
- Async task cancellation while waiting must not leak a queue slot.
- Service restart clears the in-memory queue, matching Pixelle's existing task
  registry behavior.

## Verification

Unit tests prove serialization, queue overflow, and cancellation cleanup. The
deployment contract test proves the installer checks and applies the pinned
patch and that both API routes use the shared limiter.
